"""Qt-aware helpers for running blocking SSH/Paramiko work off the UI thread."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from gi.repository import GLib

from .qt_compat import QtBinding, get_qt_binding, dispatch_to_ui

logger = logging.getLogger(__name__)


class QtWorkerSignals:
    """Lightweight signal container for Qt workers."""

    def __init__(self, binding: QtBinding) -> None:
        QtCore = binding.core

        class _SignalObject(QtCore.QObject):
            finished = QtCore.Signal(object)
            error = QtCore.Signal(Exception)
            progress = QtCore.Signal(object)

        self._signals = _SignalObject()

    @property
    def finished(self):  # pragma: no cover - thin Qt wrapper
        return self._signals.finished

    @property
    def error(self):  # pragma: no cover - thin Qt wrapper
        return self._signals.error

    @property
    def progress(self):  # pragma: no cover - thin Qt wrapper
        return self._signals.progress


class QtThreadWorker:
    """Run a callable inside a ``QThread`` with progress/error signals."""

    def __init__(self, func: Callable[[Callable[[object], None]], object]) -> None:
        self._binding = get_qt_binding()
        if self._binding is None:
            raise RuntimeError("Qt bindings are required for QtThreadWorker")

        self._func = func
        QtCore = self._binding.core
        self.signals = QtWorkerSignals(self._binding)
        self._thread = QtCore.QThread()
        self._thread.setObjectName("QtParamikoWorker")
        self._thread.started.connect(self._execute)
        self.signals.finished.connect(lambda *_: self._thread.quit())
        self.signals.error.connect(lambda *_: self._thread.quit())
        self._thread.finished.connect(self._thread.deleteLater)

    def _execute(self) -> None:
        try:
            result = self._func(self.signals.progress.emit)
        except Exception as exc:  # pragma: no cover - relies on Qt runtime
            self.signals.error.emit(exc)
        else:
            self.signals.finished.emit(result)

    def start(self) -> None:
        self._thread.start()

    def wait(self, msecs: int = -1) -> None:
        self._thread.wait(msecs)


class QtTaskRunner:
    """Submit blocking callables to QtConcurrent/QThread when available.

    The runner gracefully falls back to GLib-friendly execution so callers do
    not need to special-case toolkit differences. Success and error callbacks
    are always marshalled back to the UI thread via :func:`dispatch_to_ui`.
    """

    def __init__(self) -> None:
        self._binding: Optional[QtBinding] = get_qt_binding()

    @property
    def available(self) -> bool:
        return self._binding is not None

    def submit(
        self,
        func: Callable[[], object],
        *,
        on_success: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> object:
        if self._binding is None:
            logger.debug("Qt bindings unavailable; falling back to GLib executor")
            result = func()
            if on_success:
                dispatch_to_ui(on_success, (result,))
            return result

        QtConcurrent = self._binding.concurrent

        future = QtConcurrent.run(func)

        def _handle_finished() -> None:
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - relies on QtConcurrent
                logger.error("Qt task failed: %s", exc)
                if on_error:
                    dispatch_to_ui(on_error, (exc,))
            else:
                if on_success:
                    dispatch_to_ui(on_success, (result,))

        future.finished.connect(_handle_finished)
        return future


class QtSignalDispatcher:
    """Bridge QObject signals to the host application's dispatcher.

    ``AsyncSFTPManager`` and other Paramiko-heavy classes expect to call a
    dispatcher that schedules callbacks on the main thread. When running under
    Qt we use ``QTimer`` to bounce those callbacks through the event loop while
    preserving the existing ``GLib.idle_add`` semantics elsewhere.
    """

    def __init__(self) -> None:
        self._binding: Optional[QtBinding] = get_qt_binding()

    def dispatch(self, func: Callable, *args, **kwargs) -> None:
        if self._binding is None:
            GLib.idle_add(lambda: func(*args, **kwargs))
        else:
            dispatch_to_ui(func, args, kwargs)
