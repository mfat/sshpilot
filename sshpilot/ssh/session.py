"""Qt-aware SSH session utilities.

This module introduces Qt-style timers and signals around SSH operations so
that connection events can be consumed by both GObject- and Qt-based UIs.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from sshpilot_qt.async_utils import QTimer
from sshpilot_qt.signals import ConnectionSignals


class SessionEventEmitter:
    """Dispatches SSH lifecycle updates through Qt-style signals."""

    def __init__(
        self,
        gobject_emitter: Optional[Callable[[str, object, bool], Any]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._gobject_emit = gobject_emitter
        self._loop = loop
        self.signals = ConnectionSignals()

        # Keep the Qt bridge in sync with legacy GTK signals when available
        if self._gobject_emit is not None:
            self.signals.relay_from_gobject(self._gobject_emit.__self__)

    def emit_status(self, connection: object, is_connected: bool) -> None:
        """Emit a connection status change using Qt timers for thread safety."""

        def _do_emit() -> None:
            if self._gobject_emit is not None:
                self._gobject_emit("connection-status-changed", connection, is_connected)
            self.signals.connection_status_changed.emit(connection, is_connected)

        QTimer.singleShot(0, _do_emit, loop=self._loop)

    def emit_added(self, connection: object) -> None:
        QTimer.singleShot(0, lambda: self.signals.connection_added.emit(connection), loop=self._loop)

    def emit_removed(self, connection: object) -> None:
        QTimer.singleShot(0, lambda: self.signals.connection_removed.emit(connection), loop=self._loop)

    def emit_updated(self, connection: object) -> None:
        QTimer.singleShot(0, lambda: self.signals.connection_updated.emit(connection), loop=self._loop)
