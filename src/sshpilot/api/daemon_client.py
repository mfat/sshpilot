"""Synchronous Protocol v1 client for the local sshPilot daemon."""

from __future__ import annotations

import os
import queue
import socket
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, NoReturn, Optional, Union

from sshpilot import __version__ as sshpilot_version

from .capabilities import Capabilities
from .errors import ErrorCode, SshPilotError, unsupported_capability
from .events import (
    CoreEventCallback,
    EventPublisher,
    EventType,
    Subscription,
)
from .in_process_client import UNSUPPORTED_CLIENT_METHOD_CAPABILITIES
from .models.common import ClientId, ConnectionId, RequestId
from .models.connections import (
    ConnectionDetails,
    ConnectionSummary,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    UpdateConnectionRequest,
)
from .models.interactions import InteractionResponse
from .models.sessions import (
    AttachSessionRequest,
    AttachSessionResult,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionSummary,
)
from .models.terminal import (
    ReplayRequest,
    ReplayResult,
    ResizeTerminalRequest,
    TerminalInput,
)
from .transport.codec import (
    capabilities_from_wire,
    connection_event_from_envelope,
    connection_details_from_wire,
    connection_summary_from_wire,
    decode_envelope,
    encode_envelope,
    error_from_wire,
    handshake_request_to_wire,
    handshake_result_from_wire,
)
from .transport.envelopes import (
    ErrorResponseEnvelope,
    EventEnvelope,
    HandshakeRequest,
    RequestEnvelope,
    SuccessResponseEnvelope,
)
from .transport.framing import FramingError, encode_frame, receive_frame
from .version import PROTOCOL_VERSION

DEFAULT_REQUEST_TIMEOUT = 5.0
DEFAULT_CLIENT_EVENT_DISPATCH_LIMIT = 256
_EVENT_STOP = object()


@dataclass
class _PendingRequest:
    completed: threading.Event
    response: Optional[Union[SuccessResponseEnvelope, ErrorResponseEnvelope]] = None
    error: Optional[SshPilotError] = None


@dataclass(frozen=True)
class _TransportFailureNotice:
    error: SshPilotError


class DaemonClient:
    """Synchronous requests with one persistent socket reader and event handoff."""

    def __init__(
        self,
        *,
        socket_path: Optional[os.PathLike] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        client_name: str = "daemon-client",
        client_version: str = sshpilot_version,
        client_id: Optional[str] = None,
        frontend_type: Optional[str] = None,
        event_dispatch_limit: int = DEFAULT_CLIENT_EVENT_DISPATCH_LIMIT,
    ) -> None:
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("daemon request timeout must be positive")
        if type(event_dispatch_limit) is not int or event_dispatch_limit < 1:
            raise ValueError("event dispatch limit must be positive")
        self._timeout = float(timeout)
        self._socket_path = (
            Path(socket_path) if socket_path is not None else self.default_socket_path()
        )
        self._client_id = ClientId(client_id or f"client:{uuid.uuid4().hex}")
        self._client_name = client_name
        self._client_version = client_version
        self._frontend_type = frontend_type
        self._request_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._publisher = EventPublisher()
        self._pending_requests: Dict[RequestId, _PendingRequest] = {}
        self._event_queue: queue.Queue = queue.Queue(maxsize=event_dispatch_limit)
        self._last_event_sequence: Optional[int] = None
        self._closed = False
        self._close_complete = False
        self._transport_failed = False
        self._socket: Optional[socket.socket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._event_thread: Optional[threading.Thread] = None
        self._capabilities: Optional[Capabilities] = None
        self._selected_protocol_version: Optional[str] = None
        self._server_instance_id: Optional[str] = None
        try:
            self._connect_and_handshake()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def default_socket_path() -> Path:
        # Lazy import avoids making socket-path policy part of envelope modules.
        from sshpilot.daemon.lifecycle import resolve_socket_path

        return resolve_socket_path()

    @property
    def server_instance_id(self) -> str:
        if self._server_instance_id is None:
            raise SshPilotError(
                ErrorCode.PROTOCOL_ERROR,
                "The daemon handshake is incomplete",
            )
        return self._server_instance_id

    def get_capabilities(self) -> Capabilities:
        capabilities = self._capabilities
        if capabilities is None:
            raise self._closed_error()
        return capabilities

    def list_connections(self) -> List[ConnectionSummary]:
        result = self._request("connections.list", {})
        if type(result) is not list:
            self._fail_protocol("The daemon returned an invalid connection list")
        try:
            return [connection_summary_from_wire(item) for item in result]
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid connection list")

    def get_connection(self, connection_id: ConnectionId) -> ConnectionDetails:
        result = self._request(
            "connections.get",
            {"connection_id": connection_id},
        )
        try:
            return connection_details_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid connection details")

    def create_connection(self, request: CreateConnectionRequest) -> ConnectionDetails:
        del request
        raise self._unsupported("create_connection")

    def update_connection(
        self,
        connection_id: ConnectionId,
        request: UpdateConnectionRequest,
    ) -> ConnectionDetails:
        del connection_id, request
        raise self._unsupported("update_connection")

    def delete_connection(self, request: DeleteConnectionRequest) -> DeleteConnectionResult:
        del request
        raise self._unsupported("delete_connection")

    def open_session(self, request: OpenSessionRequest) -> SessionSummary:
        del request
        raise self._unsupported("open_session")

    def attach_session(self, request: AttachSessionRequest) -> AttachSessionResult:
        del request
        raise self._unsupported("attach_session")

    def detach_session(self, request: DetachSessionRequest) -> None:
        del request
        raise self._unsupported("detach_session")

    def close_session(self, request: CloseSessionRequest) -> None:
        del request
        raise self._unsupported("close_session")

    def send_terminal_input(self, request: TerminalInput) -> None:
        del request
        raise self._unsupported("send_terminal_input")

    def resize_terminal(self, request: ResizeTerminalRequest) -> None:
        del request
        raise self._unsupported("resize_terminal")

    def replay_terminal(self, request: ReplayRequest) -> ReplayResult:
        del request
        raise self._unsupported("replay_terminal")

    def respond_to_interaction(self, response: InteractionResponse) -> None:
        del response
        raise self._unsupported("respond_to_interaction")

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        with self._state_lock:
            if self._closed:
                raise self._closed_error()
        try:
            return self._publisher.subscribe(callback)
        except RuntimeError:
            raise self._closed_error() from None

    def close(self) -> None:
        with self._state_lock:
            if self._close_complete:
                return
            self._closed = True
            transport = self._socket
            self._socket = None
            pending = tuple(self._pending_requests.items())
            self._pending_requests.clear()
        self._close_transport(transport)
        for request_id, request in pending:
            request.error = SshPilotError(
                ErrorCode.TRANSPORT_CLOSED,
                "The daemon client was closed",
                request_id=request_id,
            )
            request.completed.set()
        self._stop_event_dispatch()
        self._publisher.close()
        self._join_thread(self._reader_thread)
        self._join_thread(self._event_thread)
        with self._state_lock:
            self._close_complete = True

    def _connect_and_handshake(self) -> None:
        transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        transport.settimeout(self._timeout)
        try:
            transport.connect(str(self._socket_path))
        except (OSError, socket.timeout):
            transport.close()
            raise SshPilotError(
                ErrorCode.DAEMON_UNAVAILABLE,
                "The local sshPilot daemon is unavailable",
                retryable=True,
            ) from None
        transport.settimeout(None)
        self._socket = transport
        self._start_background_threads()
        request = HandshakeRequest(
            client_name=self._client_name,
            client_version=self._client_version,
            supported_protocol_versions=(PROTOCOL_VERSION,),
            client_capabilities=frozenset(),
            frontend_type=self._frontend_type,
        )
        result = self._request(
            "system.handshake",
            handshake_request_to_wire(request),
            protocol_version=PROTOCOL_VERSION,
        )
        try:
            handshake = handshake_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid handshake")
        if (
            handshake.selected_protocol_version != PROTOCOL_VERSION
            or handshake.compatibility_status != "compatible"
        ):
            self._fail_protocol("The daemon selected an incompatible protocol")
        self._selected_protocol_version = handshake.selected_protocol_version
        self._server_instance_id = handshake.server_instance_id
        capabilities = self._request("system.get_capabilities", {})
        try:
            self._capabilities = capabilities_from_wire(capabilities)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid capabilities")

    def _request(
        self,
        method: str,
        params: dict,
        *,
        protocol_version: Optional[str] = None,
    ):
        with self._request_lock:
            with self._state_lock:
                if self._closed:
                    raise self._closed_error()
                transport = self._socket
                if transport is None:
                    raise self._closed_error()
                request_id = RequestId(uuid.uuid4().hex)
            request = RequestEnvelope(
                protocol_version=(
                    protocol_version
                    or self._selected_protocol_version
                    or PROTOCOL_VERSION
                ),
                request_id=request_id,
                method=method,
                params=params,
                client_id=self._client_id,
            )
            pending = _PendingRequest(completed=threading.Event())
            with self._state_lock:
                if self._closed or self._socket is not transport:
                    raise self._closed_error()
                self._pending_requests[request_id] = pending
            try:
                frame = encode_frame(encode_envelope(request))
                with self._send_lock:
                    transport.sendall(frame)
            except (FramingError, TypeError, ValueError):
                self._fail_transport(
                    SshPilotError(
                        ErrorCode.PROTOCOL_ERROR,
                        "The daemon request could not be encoded",
                        request_id=request_id,
                    )
                )
            except OSError:
                self._fail_transport(
                    SshPilotError(
                        ErrorCode.TRANSPORT_CLOSED,
                        "The daemon transport failed",
                        retryable=True,
                        request_id=request_id,
                    )
                )

            if not pending.completed.wait(self._timeout):
                with self._state_lock:
                    response_arrived = (
                        pending.response is not None or pending.error is not None
                    )
                if not response_arrived:
                    self._fail_transport(
                        SshPilotError(
                            ErrorCode.TRANSPORT_TIMEOUT,
                            "The daemon request timed out",
                            retryable=True,
                            request_id=request_id,
                        )
                    )
                    pending.completed.wait()

            if pending.error is not None:
                raise pending.error
            response = pending.response
            if response is None:
                raise SshPilotError(
                    ErrorCode.TRANSPORT_CLOSED,
                    "The daemon transport closed unexpectedly",
                    retryable=True,
                    request_id=request_id,
                )
            if isinstance(response, ErrorResponseEnvelope):
                raise error_from_wire(response.error)
            return response.result

    def _reader_main(self) -> None:
        while True:
            with self._state_lock:
                if self._closed:
                    return
                transport = self._socket
            if transport is None:
                return
            try:
                envelope = decode_envelope(receive_frame(transport))
            except EOFError:
                with self._state_lock:
                    closing = self._closed and not self._transport_failed
                if not closing:
                    self._fail_transport(
                        SshPilotError(
                            ErrorCode.TRANSPORT_CLOSED,
                            "The daemon transport closed unexpectedly",
                            retryable=True,
                        )
                    )
                return
            except FramingError:
                self._fail_transport(
                    SshPilotError(
                        ErrorCode.PROTOCOL_ERROR,
                        "The daemon sent an invalid transport frame",
                    )
                )
                return
            except (TypeError, ValueError):
                self._fail_transport(
                    SshPilotError(
                        ErrorCode.PROTOCOL_ERROR,
                        "The daemon sent an invalid protocol envelope",
                    )
                )
                return
            except OSError:
                with self._state_lock:
                    closing = self._closed and not self._transport_failed
                if not closing:
                    self._fail_transport(
                        SshPilotError(
                            ErrorCode.TRANSPORT_CLOSED,
                            "The daemon transport failed",
                            retryable=True,
                        )
                    )
                return

            if isinstance(envelope, EventEnvelope):
                if not self._receive_event(envelope):
                    return
                continue
            if not isinstance(
                envelope,
                (SuccessResponseEnvelope, ErrorResponseEnvelope),
            ):
                self._fail_protocol_from_reader(
                    "The daemon sent an unexpected envelope type"
                )
                return
            expected_version = self._selected_protocol_version or PROTOCOL_VERSION
            if envelope.protocol_version != expected_version:
                self._fail_protocol_from_reader(
                    "The daemon response uses an unexpected protocol"
                )
                return
            with self._state_lock:
                pending = self._pending_requests.pop(
                    envelope.request_id,
                    None,
                )
            if pending is None:
                self._fail_protocol_from_reader(
                    "The daemon response has an unknown request ID"
                )
                return
            pending.response = envelope
            pending.completed.set()

    def _receive_event(self, envelope: EventEnvelope) -> bool:
        expected_version = self._selected_protocol_version or PROTOCOL_VERSION
        if envelope.protocol_version != expected_version:
            self._fail_protocol_from_reader(
                "The daemon event uses an unexpected protocol"
            )
            return False
        try:
            event = connection_event_from_envelope(envelope)
        except (TypeError, ValueError):
            self._fail_protocol_from_reader(
                "The daemon sent an invalid connection event"
            )
            return False
        with self._state_lock:
            previous = self._last_event_sequence
            if previous is not None and event.sequence != previous + 1:
                invalid_sequence = True
            else:
                invalid_sequence = False
                self._last_event_sequence = event.sequence
        if invalid_sequence:
            self._fail_protocol_from_reader(
                "The daemon event sequence lost continuity"
            )
            return False
        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            self._fail_protocol_from_reader(
                "The daemon client event queue lost continuity"
            )
            return False
        return True

    def _event_dispatch_main(self) -> None:
        while True:
            item = self._event_queue.get()
            if item is _EVENT_STOP:
                return
            if isinstance(item, _TransportFailureNotice):
                try:
                    self._publisher.publish(
                        EventType.ERROR_OCCURRED,
                        item.error.to_dict(),
                    )
                except RuntimeError:
                    pass
                self._publisher.close()
                return
            try:
                self._publisher._publish_existing(item)
            except RuntimeError:
                return

    def _fail_protocol_from_reader(self, message: str) -> None:
        self._fail_transport(SshPilotError(ErrorCode.PROTOCOL_ERROR, message))

    def _fail_transport(self, error: SshPilotError) -> None:
        with self._state_lock:
            if self._transport_failed or self._close_complete:
                return
            self._transport_failed = True
            self._closed = True
            transport = self._socket
            self._socket = None
            pending = tuple(self._pending_requests.items())
            self._pending_requests.clear()
        self._close_transport(transport)
        for request_id, request in pending:
            request.error = SshPilotError(
                error.code,
                error.message,
                details=error.details,
                retryable=error.retryable,
                request_id=request_id,
            )
            request.completed.set()
        self._replace_event_queue_with_failure(error)

    def _replace_event_queue_with_failure(self, error: SshPilotError) -> None:
        while True:
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._event_queue.put_nowait(_TransportFailureNotice(error))
        except queue.Full:
            pass

    def _start_background_threads(self) -> None:
        self._event_thread = threading.Thread(
            target=self._event_dispatch_main,
            name="sshpilot-daemon-events",
            daemon=True,
        )
        self._reader_thread = threading.Thread(
            target=self._reader_main,
            name="sshpilot-daemon-reader",
            daemon=True,
        )
        self._event_thread.start()
        self._reader_thread.start()

    def _stop_event_dispatch(self) -> None:
        while True:
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._event_queue.put_nowait(_EVENT_STOP)
        except queue.Full:
            pass

    @staticmethod
    def _join_thread(thread: Optional[threading.Thread]) -> None:
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    @staticmethod
    def _close_transport(transport: Optional[socket.socket]) -> None:
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            transport.close()

    def _fail_protocol(self, message: str) -> NoReturn:
        self._fail_transport(SshPilotError(ErrorCode.PROTOCOL_ERROR, message))
        raise SshPilotError(ErrorCode.PROTOCOL_ERROR, message)

    def _closed_error(self) -> SshPilotError:
        if self._transport_failed:
            return SshPilotError(
                ErrorCode.TRANSPORT_CLOSED,
                "The daemon transport is closed",
                retryable=True,
            )
        return SshPilotError(ErrorCode.INVALID_REQUEST, "The client is closed")

    @staticmethod
    def _unsupported(method_name: str) -> SshPilotError:
        return unsupported_capability(
            UNSUPPORTED_CLIENT_METHOD_CAPABILITIES[method_name]
        )
