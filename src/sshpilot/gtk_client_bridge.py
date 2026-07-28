"""GTK-main-context handoff for synchronous frontend-neutral client calls."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, Optional, TypeVar

from gi.repository import GLib

logger = logging.getLogger(__name__)

T = TypeVar("T")


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
        self._lock = threading.Lock()
        self._closed = False
        self._requests: Dict[Future, GtkClientRequest] = {}
        self._active_requests = set()

    def submit(
        self,
        operation: Callable[[], T],
        *,
        on_success: Callable[[T], None],
        on_error: Callable[[BaseException], None],
        on_discard: Optional[Callable[[T], None]] = None,
    ) -> GtkClientRequest:
        request = GtkClientRequest()
        with self._lock:
            if self._closed:
                raise RuntimeError("GTK client bridge is closed")
            future = self._executor.submit(operation)
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

    def shutdown(self) -> None:
        """Suppress callbacks and stop accepting work without an unbounded wait."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            requests = list(self._requests.items())
            self._requests.clear()
            active_requests = list(self._active_requests)
            self._active_requests.clear()
        for request in active_requests:
            request.cancel()
        for future, request in requests:
            future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
