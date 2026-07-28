"""Selector-driven Unix-domain daemon server."""

from __future__ import annotations

import errno
import logging
import os
import selectors
import socket
import stat
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional, Tuple, Union

from sshpilot.api.client import SshPilotClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import CoreEvent, EventType, Subscription
from sshpilot.api.models.common import RequestId
from sshpilot.api.transport.codec import (
    connection_event_to_envelope,
    decode_envelope,
    encode_envelope,
    error_to_wire,
)
from sshpilot.api.transport.envelopes import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
)
from sshpilot.api.transport.framing import FrameDecoder, FramingError, encode_frame
from sshpilot.api.version import PROTOCOL_VERSION

from .dispatch import ClientProtocolState, RequestDispatcher
from .lifecycle import (
    DaemonAlreadyRunningError,
    SocketSecurityError,
    ensure_secure_socket_directory,
    prepare_socket_path,
    resolve_socket_path,
    unlink_owned_socket,
    verify_bound_socket,
)

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_EVENT_QUEUE_LIMIT = 256
DEFAULT_MAX_CLIENT_OUTBOUND_BYTES = 4 * 1024 * 1024
_FORWARDED_EVENT_TYPES = frozenset(
    {
        EventType.CONNECTION_CREATED,
        EventType.CONNECTION_UPDATED,
        EventType.CONNECTION_DELETED,
    }
)


@dataclass(frozen=True)
class _OutboundFrame:
    data: bytes
    is_event: bool = False


@dataclass
class _ClientConnection:
    sock: socket.socket
    decoder: FrameDecoder = field(default_factory=FrameDecoder)
    output: Deque[_OutboundFrame] = field(default_factory=deque)
    output_offset: int = 0
    queued_event_count: int = 0
    queued_outbound_bytes: int = 0
    protocol: ClientProtocolState = field(default_factory=ClientProtocolState)
    close_after_write: bool = False
    continuity_lost: bool = False
    closed: bool = False


class DaemonServer:
    """Own one core client and serve all local clients on its owner thread."""

    def __init__(
        self,
        core_factory: Callable[[], SshPilotClient],
        *,
        socket_path: Optional[os.PathLike] = None,
        client_event_queue_limit: int = DEFAULT_CLIENT_EVENT_QUEUE_LIMIT,
        max_client_outbound_bytes: int = DEFAULT_MAX_CLIENT_OUTBOUND_BYTES,
    ) -> None:
        if (
            type(client_event_queue_limit) is not int
            or client_event_queue_limit < 1
        ):
            raise ValueError("client event queue limit must be positive")
        if (
            type(max_client_outbound_bytes) is not int
            or max_client_outbound_bytes < 1
        ):
            raise ValueError("maximum client outbound bytes must be positive")
        self.socket_path = resolve_socket_path(socket_path)
        self.client_event_queue_limit = client_event_queue_limit
        self.max_client_outbound_bytes = max_client_outbound_bytes
        self._core_factory = core_factory
        self._selector: Optional[selectors.BaseSelector] = None
        self._listener: Optional[socket.socket] = None
        self._wakeup_read: Optional[socket.socket] = None
        self._wakeup_write: Optional[socket.socket] = None
        self._core_client: Optional[SshPilotClient] = None
        self._dispatcher: Optional[RequestDispatcher] = None
        self._clients: Dict[int, _ClientConnection] = {}
        self._socket_identity: Optional[Tuple[int, int]] = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._thread: Optional[threading.Thread] = None
        self._event_lock = threading.Lock()
        self._accepting_core_events = False
        self._core_subscription: Optional[Subscription] = None
        self._next_event_sequence = 0

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._startup_error is None

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def start_in_thread(self, *, timeout: float = 5.0) -> threading.Thread:
        """Start the production server loop in one ownership thread."""

        if self._thread is not None:
            raise RuntimeError("daemon server has already been started")
        thread = threading.Thread(
            target=self.serve_forever,
            name="sshpilot-daemon",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("daemon server did not become ready")
        if self._startup_error is not None:
            raise self._startup_error
        return thread

    def serve_forever(self) -> None:
        """Construct the core, bind securely, and dispatch until shutdown."""

        try:
            self._setup()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            self._cleanup()
            self._stopped.set()
            return
        self._ready.set()
        try:
            while not self._stopping.is_set():
                selector = self._selector
                if selector is None:
                    break
                for key, mask in selector.select():
                    if key.data == "listener":
                        self._accept_client()
                    elif key.data == "wakeup":
                        self._drain_wakeup()
                    else:
                        state = key.data
                        if mask & selectors.EVENT_READ:
                            self._read_client(state)
                        if (
                            state.sock.fileno() >= 0
                            and mask & selectors.EVENT_WRITE
                        ):
                            self._write_client(state)
        finally:
            self._stop_core_events()
            if self._dispatcher is not None:
                self._dispatcher.begin_shutdown()
            self._cleanup()
            self._stopped.set()

    def shutdown(self) -> None:
        """Request shutdown without blocking the caller."""

        if self._stopping.is_set():
            return
        self._stop_core_events()
        if self._dispatcher is not None:
            self._dispatcher.begin_shutdown()
        self._stopping.set()
        wakeup = self._wakeup_write
        if wakeup is not None:
            try:
                wakeup.send(b"x")
            except OSError:
                pass

    def wait_stopped(self, *, timeout: float = 5.0) -> bool:
        return self._stopped.wait(timeout)

    def _setup(self) -> None:
        prepare_socket_path(self.socket_path)
        selector = selectors.DefaultSelector()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old_umask = os.umask(0o177)
            try:
                try:
                    listener.bind(str(self.socket_path))
                except OSError as exc:
                    if exc.errno == errno.EADDRINUSE:
                        raise DaemonAlreadyRunningError(
                            "The daemon socket is already in use"
                        ) from None
                    raise
            finally:
                os.umask(old_umask)
            initial = self.socket_path.lstat()
            if (
                stat.S_ISSOCK(initial.st_mode)
                and not stat.S_ISLNK(initial.st_mode)
                and (
                    not hasattr(os, "getuid")
                    or initial.st_uid == os.getuid()
                )
            ):
                self._socket_identity = (initial.st_dev, initial.st_ino)
            else:
                raise SocketSecurityError("The daemon path changed during bind")
            ensure_secure_socket_directory(self.socket_path)
            os.chmod(self.socket_path, 0o600, follow_symlinks=False)
            verified_identity = verify_bound_socket(self.socket_path)
            if verified_identity != self._socket_identity:
                raise SocketSecurityError("The daemon socket changed during bind")
            listener.listen()
            listener.setblocking(False)
            # Own the single-instance socket before loading/migrating the core.
            # Readiness remains false until construction and migration finish.
            self._core_client = self._core_factory()
            self._dispatcher = RequestDispatcher(self._core_client)
            wakeup_read, wakeup_write = socket.socketpair()
            wakeup_read.setblocking(False)
            wakeup_write.setblocking(False)
            selector.register(listener, selectors.EVENT_READ, "listener")
            selector.register(wakeup_read, selectors.EVENT_READ, "wakeup")
        except Exception:
            listener.close()
            selector.close()
            raise
        self._selector = selector
        self._listener = listener
        self._wakeup_read = wakeup_read
        self._wakeup_write = wakeup_write
        subscribe_events = getattr(self._core_client, "subscribe_events", None)
        if callable(subscribe_events):
            self._core_subscription = subscribe_events(self._on_core_event)
        with self._event_lock:
            self._accepting_core_events = True

    def _accept_client(self) -> None:
        listener = self._listener
        selector = self._selector
        if listener is None or selector is None or self._stopping.is_set():
            return
        try:
            client_socket, _address = listener.accept()
        except BlockingIOError:
            return
        client_socket.setblocking(False)
        state = _ClientConnection(client_socket)
        with self._event_lock:
            self._clients[client_socket.fileno()] = state
        selector.register(client_socket, selectors.EVENT_READ, state)

    def _read_client(self, state: _ClientConnection) -> None:
        try:
            chunk = state.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self._close_client(state)
            return
        if not chunk:
            try:
                state.decoder.finish()
            except FramingError:
                logger.debug("Client closed during a partial daemon frame")
            self._close_client(state)
            return
        try:
            messages = state.decoder.feed(chunk)
        except FramingError as exc:
            self._queue_protocol_error(state, exc.code, exc.message)
            return
        for message in messages:
            self._handle_message(state, message)

    def _handle_message(self, state: _ClientConnection, message: dict) -> None:
        request_id = message.get("request_id")
        if type(request_id) is not str or not request_id.strip():
            request_id = RequestId("protocol")
        else:
            request_id = RequestId(request_id)
        try:
            envelope = decode_envelope(message)
            if not isinstance(envelope, RequestEnvelope):
                raise ValueError("clients may send request envelopes only")
            dispatcher = self._dispatcher
            if dispatcher is None:
                raise SshPilotError(
                    ErrorCode.DAEMON_SHUTTING_DOWN,
                    "The daemon is shutting down",
                    retryable=True,
                    request_id=envelope.request_id,
                )
            result = dispatcher.dispatch(envelope, state.protocol)
            response = SuccessResponseEnvelope(
                protocol_version=(
                    state.protocol.selected_protocol_version or PROTOCOL_VERSION
                ),
                request_id=envelope.request_id,
                result=result,
            )
        except SshPilotError as exc:
            response = self._error_response(request_id, exc)
        except (TypeError, ValueError):
            response = self._error_response(
                request_id,
                SshPilotError(
                    ErrorCode.PROTOCOL_ERROR,
                    "The transport envelope is malformed",
                    request_id=request_id,
                ),
            )
        self._queue_response(state, response)

    def _queue_protocol_error(
        self,
        state: _ClientConnection,
        code: ErrorCode,
        message: str,
    ) -> None:
        self._queue_response(
            state,
            self._error_response(
                RequestId("protocol"),
                SshPilotError(code, message, request_id=RequestId("protocol")),
            ),
        )
        state.close_after_write = True

    @staticmethod
    def _error_response(
        request_id: RequestId,
        error: SshPilotError,
    ) -> ErrorResponseEnvelope:
        safe_error = error
        if error.request_id != request_id:
            safe_error = SshPilotError(
                error.code,
                error.message,
                details=error.details,
                retryable=error.retryable,
                request_id=request_id,
                connection_id=error.connection_id,
                session_id=error.session_id,
            )
        return ErrorResponseEnvelope(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            error=error_to_wire(safe_error),
        )

    def _queue_response(
        self,
        state: _ClientConnection,
        response: Union[SuccessResponseEnvelope, ErrorResponseEnvelope],
    ) -> None:
        try:
            frame = _OutboundFrame(
                encode_frame(encode_envelope(response)),
                is_event=False,
            )
        except FramingError:
            logger.exception("Daemon generated an invalid response frame")
            self._close_client(state)
            return
        with self._event_lock:
            if state.closed or state.continuity_lost:
                return
            overflow = not self._enqueue_frame_locked(state, frame)
        if overflow:
            logger.warning("Disconnecting daemon peer after outbound queue overflow")
            self._close_client(state)
            return
        selector = self._selector
        if selector is not None and state.sock.fileno() >= 0:
            self._refresh_client_interest(state)

    def _write_client(self, state: _ClientConnection) -> None:
        with self._event_lock:
            if state.closed or state.continuity_lost:
                return
            if not state.output:
                frame = None
                offset = 0
            else:
                frame = state.output[0]
                offset = state.output_offset
        if frame is None:
            self._finish_write(state)
            return
        try:
            sent = state.sock.send(frame.data[offset:])
        except BlockingIOError:
            return
        except OSError:
            self._close_client(state)
            return
        with self._event_lock:
            if (
                state.closed
                or state.continuity_lost
                or not state.output
                or state.output[0] is not frame
            ):
                return
            state.output_offset += sent
            state.queued_outbound_bytes = max(
                0,
                state.queued_outbound_bytes - sent,
            )
            if state.output_offset >= len(frame.data):
                completed = state.output.popleft()
                state.output_offset = 0
                if completed.is_event:
                    state.queued_event_count -= 1
            empty = not state.output
        if empty:
            self._finish_write(state)

    def _finish_write(self, state: _ClientConnection) -> None:
        if state.close_after_write:
            self._close_client(state)
            return
        self._refresh_client_interest(state)

    def _drain_wakeup(self) -> None:
        wakeup = self._wakeup_read
        if wakeup is not None:
            try:
                while wakeup.recv(1024):
                    pass
            except BlockingIOError:
                pass
            except OSError:
                pass
        with self._event_lock:
            states = tuple(self._clients.values())
        for state in states:
            if state.continuity_lost:
                logger.warning("Disconnecting daemon peer after event queue overflow")
                self._close_client(state)
            else:
                self._refresh_client_interest(state)

    def _close_client(self, state: _ClientConnection) -> None:
        file_descriptor = state.sock.fileno()
        with self._event_lock:
            if state.closed:
                return
            state.closed = True
            state.output.clear()
            state.output_offset = 0
            state.queued_event_count = 0
            state.queued_outbound_bytes = 0
            self._clients.pop(file_descriptor, None)
        selector = self._selector
        if selector is not None and file_descriptor >= 0:
            try:
                selector.unregister(state.sock)
            except (KeyError, ValueError):
                pass
        try:
            state.sock.close()
        except OSError:
            pass

    def _cleanup(self) -> None:
        self._stop_core_events()
        with self._event_lock:
            states = tuple(self._clients.values())
        for state in states:
            self._close_client(state)
        selector = self._selector
        for sock in (self._listener, self._wakeup_read, self._wakeup_write):
            if sock is None:
                continue
            if selector is not None:
                try:
                    selector.unregister(sock)
                except (KeyError, ValueError):
                    pass
            sock.close()
        if selector is not None:
            selector.close()
        self._selector = None
        self._listener = None
        self._wakeup_read = None
        self._wakeup_write = None
        core_client = self._core_client
        self._core_client = None
        try:
            if core_client is not None:
                core_client.close()
        except Exception as error:
            logger.error(
                "Daemon core cleanup failed (%s)",
                type(error).__name__,
            )
        finally:
            unlink_owned_socket(self.socket_path, self._socket_identity)
            self._socket_identity = None

    def _on_core_event(self, event: CoreEvent) -> None:
        """Encode once and enqueue without performing socket I/O."""

        if event.type not in _FORWARDED_EVENT_TYPES:
            return
        with self._event_lock:
            if not self._accepting_core_events or self._stopping.is_set():
                return
            sequence = self._next_event_sequence
            try:
                envelope = connection_event_to_envelope(
                    event,
                    sequence=sequence,
                    protocol_version=PROTOCOL_VERSION,
                )
                frame = encode_frame(encode_envelope(envelope))
            except (FramingError, TypeError, ValueError):
                logger.error(
                    "Daemon rejected invalid core event type=%s",
                    event.type.value,
                )
                return
            self._next_event_sequence += 1
            for state in self._clients.values():
                if (
                    state.closed
                    or state.continuity_lost
                    or not state.protocol.handshake_completed
                ):
                    continue
                if state.queued_event_count >= self.client_event_queue_limit:
                    state.continuity_lost = True
                    state.output.clear()
                    state.output_offset = 0
                    state.queued_event_count = 0
                    state.queued_outbound_bytes = 0
                    continue
                if not self._enqueue_frame_locked(
                    state,
                    _OutboundFrame(frame, is_event=True),
                ):
                    continue
        self._wake_selector()

    def _enqueue_frame_locked(
        self,
        state: _ClientConnection,
        frame: _OutboundFrame,
    ) -> bool:
        """Queue one complete frame while enforcing total per-peer memory."""

        if (
            state.queued_outbound_bytes + len(frame.data)
            > self.max_client_outbound_bytes
        ):
            state.continuity_lost = True
            state.output.clear()
            state.output_offset = 0
            state.queued_event_count = 0
            state.queued_outbound_bytes = 0
            return False
        state.output.append(frame)
        state.queued_outbound_bytes += len(frame.data)
        if frame.is_event:
            state.queued_event_count += 1
        return True

    def _refresh_client_interest(self, state: _ClientConnection) -> None:
        selector = self._selector
        if selector is None or state.closed or state.sock.fileno() < 0:
            return
        with self._event_lock:
            has_output = bool(state.output)
        events = selectors.EVENT_READ
        if has_output:
            events |= selectors.EVENT_WRITE
        try:
            selector.modify(state.sock, events, state)
        except (KeyError, ValueError, OSError):
            self._close_client(state)

    def _wake_selector(self) -> None:
        wakeup = self._wakeup_write
        if wakeup is None:
            return
        try:
            wakeup.send(b"e")
        except (BlockingIOError, OSError):
            pass

    def _stop_core_events(self) -> None:
        with self._event_lock:
            self._accepting_core_events = False
            subscription = self._core_subscription
            self._core_subscription = None
        if subscription is not None:
            subscription.unsubscribe()
