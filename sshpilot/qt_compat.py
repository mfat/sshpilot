"""Qt interoperability helpers for background work and UI dispatching.

This module keeps Qt usage lazy and optional so the existing GTK entrypoints
remain functional. When Qt bindings are available we can safely marshal work
onto the Qt event loop without touching the GLib main context. When Qt is not
installed we transparently fall back to GLib/asyncio scheduling so callers can
use a single helper regardless of toolkit.
"""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from gi.repository import GLib


@dataclass(frozen=True)
class QtBinding:
    """Container for imported Qt modules."""

    core: object
    gui: object
    widgets: object
    concurrent: object


_BINDING_CACHE: Optional[QtBinding] = None


def _import_binding() -> Optional[QtBinding]:
    global _BINDING_CACHE
    if _BINDING_CACHE is not None:
        return _BINDING_CACHE

    if importlib.util.find_spec("PyQt6"):
        from PyQt6 import QtCore, QtGui, QtWidgets, QtConcurrent  # type: ignore

        _BINDING_CACHE = QtBinding(QtCore, QtGui, QtWidgets, QtConcurrent)
        return _BINDING_CACHE

    if importlib.util.find_spec("PySide6"):
        from PySide6 import QtCore, QtGui, QtWidgets, QtConcurrent  # type: ignore

        _BINDING_CACHE = QtBinding(QtCore, QtGui, QtWidgets, QtConcurrent)
        return _BINDING_CACHE

    return None


def get_qt_binding() -> Optional[QtBinding]:
    """Return the detected Qt binding if present.

    The function performs no imports when Qt is absent, allowing non-Qt
    runtimes to continue operating normally.
    """

    return _import_binding()


def dispatch_to_ui(
    callback: Callable,
    args: Sequence | tuple = (),
    kwargs: Optional[dict] = None,
) -> None:
    """Execute ``callback`` on the active UI thread.

    When Qt bindings are available the function uses ``QTimer.singleShot`` to
    queue the callback without assuming a GLib main context. Otherwise we fall
    back to ``GLib.idle_add`` or ``asyncio`` to keep semantics as close as
    possible while avoiding toolkit-specific code at call sites.
    """

    binding = get_qt_binding()
    payload_kwargs = kwargs or {}

    if binding is not None:
        QtCore = binding.core

        def _run() -> None:
            callback(*args, **payload_kwargs)

        QtCore.QTimer.singleShot(0, _run)
        return

    try:
        GLib.idle_add(lambda: callback(*args, **payload_kwargs))
        return
    except Exception:
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(callback, *args, **payload_kwargs)
