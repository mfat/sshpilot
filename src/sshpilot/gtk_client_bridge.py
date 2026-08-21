"""GTK-main-context handoff for synchronous frontend-neutral client calls."""

from __future__ import annotations

import logging
import struct
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, TypeVar

from gi.repository import GLib

from .api.errors import ErrorCode, SshPilotError
from .api.models.terminal import TerminalOutput

logger = logging.getLogger(__name__)

T = TypeVar("T")
# Maximum raw payload handed to GTK in one idle slice.  This is a latency
# bound, not a permission to discard terminal data.
DEFAULT_GTK_TERMINAL_PENDING_BYTES = 64 * 1024
DEFAULT_GTK_TERMINAL_SPOOL_BYTES = 64 * 1024 * 1024
DEFAULT_PENDING_TERMINAL_INPUTS = 256

_TERMINAL_OUTPUT_RECORD = struct.Struct("!QQBQ")
_OUTPUT_REPLAY = 1
_OUTPUT_EOF = 2


class GtkClientRequest:
    """Cancelable delivery token for one submitted operation."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._slot = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()


class GtkClientBridge:
    """Run synchronous daemon calls on one worker and deliver on GTK's thread."""

    def __init__(
        self,
        *,
        dispatcher: Callable[..., object] = GLib.idle_add,
        max_workers: int = 1,
        max_pending_terminal_inputs: int = DEFAULT_PENDING_TERMINAL_INPUTS,
    ) -> None:
        if max_workers < 1:
            raise ValueError("GTK client bridge needs at least one worker")
        if max_pending_terminal_inputs < 1:
            raise ValueError("pending terminal input limit must be positive")
        self._dispatcher = dispatcher
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="sshpilot-api",
        )
        self._interaction_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="sshpilot-interaction",
        )
        self._terminal_input_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="sshpilot-terminal-input",
        )
        self._terminal_input_slots = threading.BoundedSemaphore(
            max_pending_terminal_inputs
        )
        self._lock = threading.RLock()
        self._closed = False
        self._requests: Dict[Future, GtkClientRequest] = {}
        self._active_requests = set()
        self._terminal_bindings = set()

    def bind_terminal(
        self,
        client,
        session_id,
        *,
        on_output,
        on_continuity_lost=None,
        on_eof=None,
        on_error=None,
        max_pending_bytes: int = DEFAULT_GTK_TERMINAL_PENDING_BYTES,
        max_spool_bytes: int = DEFAULT_GTK_TERMINAL_SPOOL_BYTES,
        start_paused: bool = False,
        recovery_sequence: Optional[int] = None,
    ):
        """Coalesce daemon terminal callbacks onto GTK's main context."""

        binding = GtkTerminalBinding(
            client,
            session_id,
            dispatcher=self._dispatcher,
            on_output=on_output,
            on_continuity_lost=on_continuity_lost,
            on_eof=on_eof,
            on_error=on_error,
            max_pending_bytes=max_pending_bytes,
            max_spool_bytes=max_spool_bytes,
            start_paused=start_paused,
            recovery_sequence=recovery_sequence,
            on_close=lambda item: self._discard_terminal_binding(item),
        )
        with self._lock:
            if self._closed:
                binding.close()
                raise RuntimeError("GTK client bridge is closed")
            self._terminal_bindings.add(binding)
        return binding

    def _discard_terminal_binding(self, binding) -> None:
        with self._lock:
            self._terminal_bindings.discard(binding)

    def submit(
        self,
        operation: Callable[[], T],
        *,
        on_success: Callable[[T], None],
        on_error: Callable[[BaseException], None],
        on_discard: Optional[Callable[[T], None]] = None,
    ) -> GtkClientRequest:
        return self._submit_on(
            self._executor,
            operation,
            on_success=on_success,
            on_error=on_error,
            on_discard=on_discard,
        )

    def submit_interaction(
        self,
        operation: Callable[[], T],
        *,
        on_success: Callable[[T], None],
        on_error: Callable[[BaseException], None],
        on_discard: Optional[Callable[[T], None]] = None,
    ) -> GtkClientRequest:
        """Run interaction control independently of terminal streaming work."""

        return self._submit_on(
            self._interaction_executor,
            operation,
            on_success=on_success,
            on_error=on_error,
            on_discard=on_discard,
        )

    def submit_terminal_input(
        self,
        operation: Callable[[], T],
        *,
        on_success: Callable[[T], None],
        on_error: Callable[[BaseException], None],
        on_discard: Optional[Callable[[T], None]] = None,
    ) -> GtkClientRequest:
        """Serialize input independently from resize/control RPCs."""

        return self._submit_on(
            self._terminal_input_executor,
            operation,
            on_success=on_success,
            on_error=on_error,
            on_discard=on_discard,
            slot=self._terminal_input_slots,
        )

    def _submit_on(
        self,
        executor: ThreadPoolExecutor,
        operation: Callable[[], T],
        *,
        on_success: Callable[[T], None],
        on_error: Callable[[BaseException], None],
        on_discard: Optional[Callable[[T], None]],
        slot: Optional[threading.BoundedSemaphore] = None,
    ) -> GtkClientRequest:
        request = GtkClientRequest()
        if slot is not None and not slot.acquire(blocking=False):
            raise SshPilotError(
                ErrorCode.TERMINAL_INPUT_BACKPRESSURE,
                "The frontend terminal input queue is full",
                retryable=True,
            )
        request._slot = slot
        with self._lock:
            if self._closed:
                if slot is not None:
                    slot.release()
                raise RuntimeError("GTK client bridge is closed")
            try:
                future = executor.submit(operation)
            except BaseException:
                if slot is not None:
                    slot.release()
                raise
            self._requests[future] = request
            self._active_requests.add(request)
        future.add_done_callback(
            lambda completed: self._schedule_delivery(
                completed,
                request,
                on_success,
                on_error,
                on_discard,
            )
        )
        return request

    def _schedule_delivery(
        self,
        future: Future,
        request: GtkClientRequest,
        on_success: Callable[[T], None],
        on_error: Callable[[BaseException], None],
        on_discard: Optional[Callable[[T], None]],
    ) -> None:
        with self._lock:
            self._requests.pop(future, None)
            closed = self._closed

        try:
            result = future.result()
        except BaseException as error:
            if not closed and not request.cancelled:
                self._dispatcher(self._deliver_error, request, on_error, error)
            else:
                self._finish_request(request)
            return

        if closed or request.cancelled:
            self._finish_request(request)
            if on_discard is not None:
                on_discard(result)
            return
        self._dispatcher(
            self._deliver_success,
            request,
            on_success,
            on_discard,
            result,
        )

    def _deliver_success(
        self,
        request: GtkClientRequest,
        callback: Callable[[T], None],
        on_discard: Optional[Callable[[T], None]],
        result: T,
    ) -> bool:
        self._finish_request(request)
        if request.cancelled:
            if on_discard is not None:
                on_discard(result)
            return False
        callback(result)
        return False

    def _deliver_error(
        self,
        request: GtkClientRequest,
        callback: Callable[[BaseException], None],
        error: BaseException,
    ) -> bool:
        self._finish_request(request)
        if not request.cancelled:
            callback(error)
        return False

    def _finish_request(self, request: GtkClientRequest) -> None:
        with self._lock:
            self._active_requests.discard(request)
            slot = request._slot
            request._slot = None
        if slot is not None:
            slot.release()

    def shutdown(self, *, wait: bool = False, wait_timeout: float = 1.0) -> None:
        """Suppress callbacks and stop accepting work.

        ``wait=False`` (default) matches historical non-blocking teardown used
        by the live app. Tests may pass ``wait=True`` so worker threads exit
        before the next case starts.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            requests = list(self._requests.items())
            self._requests.clear()
            active_requests = list(self._active_requests)
            self._active_requests.clear()
            terminal_bindings = tuple(self._terminal_bindings)
            self._terminal_bindings.clear()
        for request in active_requests:
            request.cancel()
            self._finish_request(request)
        for future, request in requests:
            future.cancel()
        for binding in terminal_bindings:
            binding.close()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        self._interaction_executor.shutdown(wait=wait, cancel_futures=True)
        self._terminal_input_executor.shutdown(wait=wait, cancel_futures=True)
        if wait and wait_timeout > 0:
            # ThreadPoolExecutor has no join timeout; a short sleep gives
            # cancelled workers a bounded chance to exit without hanging tests.
            import time

            deadline = time.monotonic() + wait_timeout
            while time.monotonic() < deadline:
                if not any(
                    getattr(t, "is_alive", lambda: False)()
                    for t in (
                        *getattr(self._executor, "_threads", ()),
                        *getattr(self._interaction_executor, "_threads", ()),
                        *getattr(self._terminal_input_executor, "_threads", ()),
                    )
                ):
                    break
                time.sleep(0.01)


class GtkTerminalBinding:
    """One ordered, bounded terminal-to-GLib handoff.

    Output is durably spooled outside the GTK heap and drained in bounded
    slices.  Reaching the spool's hard bound is terminal for this binding: its
    coherent prefix is delivered, continuity loss is reported, and later
    bytes are suppressed.  In particular, bytes after a gap are never fed to
    the existing terminal-parser state.
    """

    def __init__(
        self,
        client,
        session_id,
        *,
        dispatcher,
        on_output,
        on_continuity_lost,
        on_eof,
        on_error,
        max_pending_bytes,
        max_spool_bytes=DEFAULT_GTK_TERMINAL_SPOOL_BYTES,
        start_paused=False,
        recovery_sequence=None,
        on_close,
    ) -> None:
        if (
            type(max_pending_bytes) is not int
            or max_pending_bytes < 1
        ):
            raise ValueError("GTK terminal pending byte limit must be positive")
        if type(max_spool_bytes) is not int or max_spool_bytes < 1:
            raise ValueError("GTK terminal spool byte limit must be positive")
        if type(start_paused) is not bool:
            raise TypeError("GTK terminal paused state must be a boolean")
        if recovery_sequence is not None and (
            type(recovery_sequence) is not int or recovery_sequence < 0
        ):
            raise ValueError("terminal recovery sequence must be non-negative")
        self._dispatcher = dispatcher
        self._on_output = on_output
        self._on_continuity_lost = on_continuity_lost
        self._on_eof = on_eof
        self._on_error = on_error
        self._max_pending_bytes = max_pending_bytes
        self._max_spool_bytes = max_spool_bytes
        self._on_close = on_close
        self._lock = threading.Lock()
        self._spool = tempfile.TemporaryFile(mode="w+b", buffering=0)
        self._spool.truncate(max_spool_bytes)
        self._spool_read_offset = 0
        self._spool_write_offset = 0
        self._spool_records = 0
        self._spool_bytes = 0
        self._pending_bytes = 0
        self._high_water_mark = 0
        self._scheduled = False
        self._paused = start_paused
        self._recovery_sequence = recovery_sequence
        self._recovery_end = None
        self._recovery_live_enabled = recovery_sequence is None
        self._closed = False
        self._terminal_loss = None
        self._loss_reported = False
        self._pending_eof = None
        self._pending_error = None
        self._last_received_sequence = None
        self._last_delivered_sequence = None
        self._session_id = session_id
        self._subscription = client.subscribe_terminal(
            session_id,
            self._receive_output,
            on_continuity_lost=self._receive_continuity,
            on_eof=self._receive_eof,
            on_error=self._receive_error,
        )

    @property
    def pending_bytes(self) -> int:
        with self._lock:
            return self._pending_bytes

    @property
    def high_water_mark(self) -> int:
        with self._lock:
            return self._high_water_mark

    @property
    def last_received_sequence(self):
        with self._lock:
            return self._last_received_sequence

    @property
    def last_delivered_sequence(self):
        with self._lock:
            return self._last_delivered_sequence

    @property
    def continuity_lost(self) -> bool:
        with self._lock:
            return self._terminal_loss is not None

    def _receive_output(self, output) -> None:
        with self._lock:
            if self._closed or self._terminal_loss is not None:
                return
            if self._recovery_sequence is not None:
                progress = (
                    self._last_received_sequence
                    if self._last_received_sequence is not None
                    else self._recovery_sequence
                )
                if output.replay:
                    if output.sequence != progress:
                        self._terminal_loss = (
                            output.session_id,
                            progress,
                            output.sequence,
                        )
                        self._schedule_locked()
                        return
                elif (
                    not self._recovery_live_enabled
                    or self._recovery_end is None
                    or progress < self._recovery_end
                ):
                    # Live output queued before the replay response/boundary
                    # cannot be mixed into the reconstruction stream.
                    return
            if (
                self._last_received_sequence is not None
                and output.sequence != self._last_received_sequence
            ):
                self._terminal_loss = (
                    output.session_id,
                    self._last_received_sequence,
                    output.sequence,
                )
                self._schedule_locked()
                return
            created_us = int(output.created_at.timestamp() * 1_000_000)
            flags = (_OUTPUT_REPLAY if output.replay else 0) | (
                _OUTPUT_EOF if output.eof else 0
            )
            header = _TERMINAL_OUTPUT_RECORD.pack(
                output.sequence,
                len(output.data),
                flags,
                created_us,
            )
            record_size = len(header) + len(output.data)
            if self._spool_bytes + record_size > self._max_spool_bytes:
                self._terminal_loss = (
                    output.session_id,
                    output.sequence,
                    output.next_sequence,
                )
                self._schedule_locked()
                return
            try:
                self._write_spool_locked(self._spool_write_offset, header)
                data_offset = (
                    self._spool_write_offset + len(header)
                ) % self._max_spool_bytes
                self._write_spool_locked(data_offset, output.data)
            except OSError:
                self._terminal_loss = (
                    output.session_id,
                    output.sequence,
                    output.next_sequence,
                )
                self._schedule_locked()
                return
            self._spool_write_offset = (
                self._spool_write_offset + record_size
            ) % self._max_spool_bytes
            self._spool_records += 1
            self._spool_bytes += record_size
            self._pending_bytes += len(output.data)
            self._high_water_mark = max(
                self._high_water_mark,
                self._pending_bytes,
            )
            self._last_received_sequence = output.next_sequence
            self._schedule_locked()

    def _receive_continuity(
        self,
        session_id,
        expected_sequence,
        available_sequence,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            if self._terminal_loss is not None:
                return
            self._terminal_loss = (
                session_id,
                expected_sequence,
                available_sequence,
            )
            self._schedule_locked()

    def _receive_eof(self, session_id, sequence) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending_eof = (session_id, sequence)
            self._schedule_locked()

    def _receive_error(self, error) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending_error = error
            self._schedule_locked()

    def _schedule_locked(self) -> None:
        if self._scheduled or self._paused:
            return
        self._scheduled = True
        self._dispatcher(self._drain)

    def resume(
        self,
        replay_end: Optional[int] = None,
        *,
        allow_live: bool = True,
    ) -> None:
        """Advance a validated recovery chunk and allow GTK delivery."""

        with self._lock:
            if self._closed:
                return
            if self._recovery_sequence is not None:
                if replay_end is None or replay_end < self._recovery_sequence:
                    raise ValueError("terminal replay end precedes recovery start")
                self._recovery_end = replay_end
                self._recovery_live_enabled = allow_live
            if not self._paused:
                return
            self._paused = False
            if (
                self._spool_records
                or self._terminal_loss is not None
                or self._pending_eof is not None
                or self._pending_error is not None
            ):
                self._schedule_locked()

    def _drain(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            pending = self._read_slice_locked()
            more_output = self._spool_records > 0
            continuity = None
            eof = None
            error = None
            if not more_output:
                if self._terminal_loss is not None and not self._loss_reported:
                    continuity = self._terminal_loss
                    self._loss_reported = True
                eof = self._pending_eof
                self._pending_eof = None
                error = self._pending_error
                self._pending_error = None
                self._scheduled = False
                self._reset_empty_spool_locked()

        for output in pending:
            with self._lock:
                if self._closed:
                    return False
            self._on_output(output)
            with self._lock:
                self._last_delivered_sequence = output.next_sequence
        if continuity is not None and callable(self._on_continuity_lost):
            self._on_continuity_lost(*continuity)
        if eof is not None and callable(self._on_eof):
            self._on_eof(*eof)
        if error is not None and callable(self._on_error):
            self._on_error(error)
        return more_output

    def _read_slice_locked(self):
        pending = []
        payload_bytes = 0
        while self._spool_records:
            raw_header = self._read_spool_locked(
                self._spool_read_offset,
                _TERMINAL_OUTPUT_RECORD.size,
            )
            if len(raw_header) != _TERMINAL_OUTPUT_RECORD.size:
                self._terminal_loss = (
                    self._session_id,
                    self._last_delivered_sequence or 0,
                    self._last_received_sequence or 0,
                )
                self._spool_records = 0
                self._spool_bytes = 0
                self._pending_bytes = 0
                break
            sequence, length, flags, created_us = _TERMINAL_OUTPUT_RECORD.unpack(
                raw_header
            )
            if pending and payload_bytes + length > self._max_pending_bytes:
                break
            data_offset = (
                self._spool_read_offset + _TERMINAL_OUTPUT_RECORD.size
            ) % self._max_spool_bytes
            data = self._read_spool_locked(data_offset, length)
            if len(data) != length:
                self._terminal_loss = (
                    self._session_id,
                    self._last_delivered_sequence or 0,
                    self._last_received_sequence or 0,
                )
                self._spool_records = 0
                self._spool_bytes = 0
                self._pending_bytes = 0
                break
            record_size = _TERMINAL_OUTPUT_RECORD.size + length
            self._spool_read_offset = (
                self._spool_read_offset + record_size
            ) % self._max_spool_bytes
            self._spool_records -= 1
            self._spool_bytes -= record_size
            self._pending_bytes -= length
            payload_bytes += length
            pending.append(
                TerminalOutput(
                    session_id=self._session_id,
                    sequence=sequence,
                    data=data,
                    created_at=datetime.fromtimestamp(
                        created_us / 1_000_000,
                        tz=timezone.utc,
                    ),
                    replay=bool(flags & _OUTPUT_REPLAY),
                    eof=bool(flags & _OUTPUT_EOF),
                )
            )
        return pending

    def _write_spool_locked(self, offset: int, data: bytes) -> None:
        first_length = min(len(data), self._max_spool_bytes - offset)
        self._spool.seek(offset)
        self._spool.write(data[:first_length])
        if first_length < len(data):
            self._spool.seek(0)
            self._spool.write(data[first_length:])

    def _read_spool_locked(self, offset: int, length: int) -> bytes:
        first_length = min(length, self._max_spool_bytes - offset)
        self._spool.seek(offset)
        first = self._spool.read(first_length)
        if len(first) != first_length or first_length == length:
            return first
        self._spool.seek(0)
        return first + self._spool.read(length - first_length)

    def _reset_empty_spool_locked(self) -> None:
        if self._spool_records:
            return
        self._spool_read_offset = 0
        self._spool_write_offset = 0
        self._spool_bytes = 0
        self._pending_bytes = 0

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            spool = self._spool
            self._pending_bytes = 0
            self._spool_records = 0
            self._spool_bytes = 0
        spool.close()
        self._subscription.close()
        self._on_close(self)
