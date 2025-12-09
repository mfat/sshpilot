"""Qt-style signal helpers used to mirror GLib/GObject events.

The helpers in this module intentionally mimic the Qt signal/slot API while
remaining lightweight enough to run in environments where the full Qt
stack is unavailable. They provide a bridge between existing GObject signal
emitters and Qt-inspired consumers by exposing familiar ``connect`` and
``emit`` methods. Signal relays are kept in a shared module so they can be
imported by both UI and SSH subsystems without circular dependencies.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Optional


class QObject:
    """Minimal QObject stand-in for organising signal emitters."""

    def __init__(self, parent: Optional[object] = None):
        self.parent = parent


class Signal:
    """Simple signal that mirrors the Qt ``connect``/``emit`` pattern."""

    def __init__(self) -> None:
        self._subscribers: List[Callable[..., Any]] = []

    def connect(self, callback: Callable[..., Any]) -> None:
        """Register a slot to be invoked when the signal fires."""

        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def disconnect(self, callback: Callable[..., Any]) -> None:
        """Remove a previously registered slot if present."""

        try:
            self._subscribers.remove(callback)
        except ValueError:
            return

    def emit(self, *args: Any, **kwargs: Any) -> None:
        """Invoke all connected slots with the provided arguments."""

        for callback in list(self._subscribers):
            callback(*args, **kwargs)


class ConnectionSignals(QObject):
    """Centralised signal collection for connection lifecycle events."""

    def __init__(self, parent: Optional[object] = None):
        super().__init__(parent=parent)
        self.connection_added: Signal = Signal()
        self.connection_removed: Signal = Signal()
        self.connection_updated: Signal = Signal()
        self.connection_status_changed: Signal = Signal()

    def relay_from_gobject(self, emitter: Any) -> None:
        """Bridge standard GObject signals into the Qt-style collection.

        The relay keeps both ecosystems in sync so that GTK and Qt widgets
        can respond to the same events without duplicating business logic.
        """

        if hasattr(emitter, "connect"):
            emitter.connect(
                "connection-added",
                lambda _obj, connection: self.connection_added.emit(connection),
            )
            emitter.connect(
                "connection-removed",
                lambda _obj, connection: self.connection_removed.emit(connection),
            )
            emitter.connect(
                "connection-updated",
                lambda _obj, connection: self.connection_updated.emit(connection),
            )
            emitter.connect(
                "connection-status-changed",
                lambda _obj, connection, status: self.connection_status_changed.emit(
                    connection, status
                ),
            )

    def emit_all(
        self,
        added: Iterable[Any] = (),
        removed: Iterable[Any] = (),
        updated: Iterable[Any] = (),
    ) -> None:
        """Convenience helper for bulk emitting events."""

        for item in added:
            self.connection_added.emit(item)
        for item in removed:
            self.connection_removed.emit(item)
        for item in updated:
            self.connection_updated.emit(item)
