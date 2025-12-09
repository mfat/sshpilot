"""Qt-oriented UI helpers for the sshPilot Qt migration.

These classes provide small, composable building blocks that parallel
common GTK constructs with Qt-ready equivalents. They avoid the need for
UI definition files and focus on lightweight, code-driven configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from .async_utils import QTimer
from .signals import QObject, Signal


@dataclass
class ToolbarAction:
    label: str
    callback: Callable[[], None]
    tooltip: str = ""


class HeaderToolBar(QObject):
    """Replacement for GTK header bars using a Qt-style toolbar model."""

    def __init__(self, title: str = "", parent: Optional[object] = None):
        super().__init__(parent)
        self.title = title
        self.actions: List[ToolbarAction] = []
        self.stylesheet: str = ""

    def add_action(self, label: str, callback: Callable[[], None], tooltip: str = "") -> None:
        self.actions.append(ToolbarAction(label=label, callback=callback, tooltip=tooltip))

    def set_stylesheet(self, stylesheet: str) -> None:
        self.stylesheet = stylesheet

    def trigger(self, label: str) -> None:
        for action in self.actions:
            if action.label == label:
                action.callback()
                break


class ToastManager(QObject):
    """Simple toast presenter backed by Qt-style status tips."""

    def __init__(self, parent: Optional[object] = None):
        super().__init__(parent)
        self.toast_shown: Signal = Signal()
        self.toast_hidden: Signal = Signal()
        self.active_messages: List[str] = []
        self.stylesheet: str = ""

    def show_toast(self, message: str, duration_ms: int = 2000) -> None:
        self.active_messages.append(message)
        self.toast_shown.emit(message)
        QTimer.singleShot(duration_ms, lambda: self._hide_toast(message))

    def _hide_toast(self, message: str) -> None:
        if message in self.active_messages:
            self.active_messages.remove(message)
        self.toast_hidden.emit(message)

    def set_stylesheet(self, stylesheet: str) -> None:
        self.stylesheet = stylesheet


@dataclass
class PopoverAction:
    label: str
    callback: Callable[[], None]
    status_tip: str = ""


class PopoverMenu(QObject):
    """Lightweight stand-in for ``QMenu`` popovers."""

    def __init__(self, parent: Optional[object] = None):
        super().__init__(parent)
        self.actions: List[PopoverAction] = []
        self.stylesheet: str = ""
        self.opened: Signal = Signal()
        self.closed: Signal = Signal()

    def add_action(self, label: str, callback: Callable[[], None], status_tip: str = "") -> None:
        self.actions.append(PopoverAction(label=label, callback=callback, status_tip=status_tip))

    def set_stylesheet(self, stylesheet: str) -> None:
        self.stylesheet = stylesheet

    def open(self) -> None:
        self.opened.emit()

    def close(self) -> None:
        self.closed.emit()

    def trigger(self, label: str) -> None:
        for action in self.actions:
            if action.label == label:
                action.callback()
                break
