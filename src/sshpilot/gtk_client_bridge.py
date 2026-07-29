"""GTK-main-context handoff for synchronous frontend-neutral client calls."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from collections import deque
from typing import Callable, Deque, Dict, Optional, TypeVar

from gi.repository import GLib

logger = logging.getLogger(__name__)

T = TypeVar("T")
DEFAULT_GTK_TERMINAL_PENDING_BYTES = 1024 * 1024


class GtkClientRequest:
    """Cancelable delivery token for one submitted operation."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()

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
    ) -> None:
        if max_workers < 1:
            raise ValueError("GTK client bridge needs at least one worker")
        self._dispatcher = dispatcher
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="sshpilot-api",
        )
        self._interaction_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="sshpilot-interaction",
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

    def _submit_on(
        self,
        executor: ThreadPoolExecutor,
        operation: Callable[[], T],
        *,
        on_success: Callable[[T], None],
        on_error: Callable[[BaseException], None],
        on_discard: Optional[Callable[[T], None]],
    ) -> GtkClientRequest:
        request = GtkClientRequest()
        with self._lock:
            if self._closed:
                raise RuntimeError("GTK client bridge is closed")
            future = executor.submit(operation)
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
        for future, request in requests:
            future.cancel()
        for binding in terminal_bindings:
            binding.close()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        self._interaction_executor.shutdown(wait=wait, cancel_futures=True)
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
                    )
                ):
                    break
                time.sleep(0.01)


class GtkTerminalBinding:
    """One bounded, coalesced terminal-to-GLib handoff."""

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
        on_close,
    ) -> None:
        if (
            type(max_pending_bytes) is not int
            or max_pending_bytes < 1
        ):
            raise ValueError("GTK terminal pending byte limit must be positive")
        self._dispatcher = dispatcher
        self._on_output = on_output
        self._on_continuity_lost = on_continuity_lost
        self._on_eof = on_eof
        self._on_error = on_error
        self._max_pending_bytes = max_pending_bytes
        self._on_close = on_close
        self._lock = threading.Lock()
        self._pending: Deque = deque()
        self._pending_bytes = 0
        self._scheduled = False
        self._closed = False
        self._continuity_lost = None
        self._subscription = client.subscribe_terminal(
            session_id,
            self._receive_output,
            on_continuity_lost=self._receive_continuity,
            on_eof=self._receive_eof,
            on_error=self._receive_error,
        )

    def _receive_output(self, output) -> None:
        with self._lock:
            if self._closed:
                return
            if self._pending_bytes + len(output.data) > self._max_pending_bytes:
                self._pending.clear()
                self._pending_bytes = 0
                self._continuity_lost = (
                    output.session_id,
                    output.sequence,
                    output.next_sequence,
                )
            else:
                self._pending.append(("output", output))
                self._pending_bytes += len(output.data)
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
            self._continuity_lost = (
                session_id,
                expected_sequence,
                available_sequence,
            )
            self._schedule_locked()

    def _receive_eof(self, session_id, sequence) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending.append(("eof", session_id, sequence))
            self._schedule_locked()

    def _receive_error(self, error) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending.append(("error", error))
            self._schedule_locked()

    def _schedule_locked(self) -> None:
        if self._scheduled:
            return
        self._scheduled = True
        self._dispatcher(self._drain)

    def _drain(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            pending = tuple(self._pending)
            self._pending.clear()
            self._pending_bytes = 0
            continuity = self._continuity_lost
            self._continuity_lost = None
            self._scheduled = False
        if continuity is not None and callable(self._on_continuity_lost):
            self._on_continuity_lost(*continuity)
        for item in pending:
            if item[0] == "output":
                self._on_output(item[1])
            elif item[0] == "eof" and callable(self._on_eof):
                self._on_eof(item[1], item[2])
            elif item[0] == "error" and callable(self._on_error):
                self._on_error(item[1])
        return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._pending_bytes = 0
        self._subscription.close()
        self._on_close(self)
