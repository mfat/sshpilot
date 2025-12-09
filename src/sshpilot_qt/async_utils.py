"""Qt-like timer helpers for coordinating background work.

These helpers mimic ``QTimer.singleShot`` and ``QTimer`` without requiring an
active Qt event loop. They rely on ``threading.Timer`` to schedule callbacks
and optionally marshal results back onto an asyncio event loop to keep SSH
operations thread-safe.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Optional


class QTimer:
    """Lightweight replacement for Qt's ``QTimer`` class."""

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        self._loop = loop
        self._interval_ms: Optional[int] = None
        self._single_shot = False
        self._callbacks: list[Callable[[], Any]] = []
        self._timer: Optional[threading.Timer] = None

    @staticmethod
    def singleShot(
        msec: int, callback: Callable[[], Any], loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> threading.Timer:
        """Schedule a one-shot callback using a Qt-style API."""

        timer = threading.Timer(msec / 1000.0, _invoke_callback, args=(callback, loop))
        timer.daemon = True
        timer.start()
        return timer

    def setSingleShot(self, single_shot: bool) -> None:
        self._single_shot = single_shot

    def timeout_connect(self, callback: Callable[[], Any]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def start(self, msec: int) -> None:
        self._interval_ms = msec
        self.stop()
        self._timer = threading.Timer(msec / 1000.0, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire(self) -> None:
        for callback in list(self._callbacks):
            _invoke_callback(callback, self._loop)
        if not self._single_shot and self._interval_ms is not None:
            self.start(self._interval_ms)


def _invoke_callback(callback: Callable[[], Any], loop: Optional[asyncio.AbstractEventLoop]) -> None:
    if loop is None:
        callback()
        return

    try:
        loop.call_soon_threadsafe(callback)
    except RuntimeError:
        # Loop is closed; fall back to direct invocation
        callback()
