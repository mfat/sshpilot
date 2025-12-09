"""Qt shortcut registration mirroring the GTK accelerator map."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence

from .qt_compat import QtBinding, get_qt_binding


@dataclass
class ShortcutSet:
    mac: Sequence[str]
    default: Sequence[str]


DEFAULT_SHORTCUTS: Dict[str, ShortcutSet] = {
    "quit": ShortcutSet(["Meta+Shift+Q"], ["Ctrl+Shift+Q"]),
    "new-connection": ShortcutSet(["Meta+N"], ["Ctrl+N"]),
    "open-new-connection-tab": ShortcutSet(["Meta+Alt+N"], ["Ctrl+Alt+N"]),
    "toggle-list": ShortcutSet(["Meta+L"], ["Ctrl+L"]),
    "search": ShortcutSet(["Meta+F"], ["Ctrl+F"]),
    "terminal-search": ShortcutSet(["Meta+Shift+F"], ["Ctrl+Shift+F"]),
    "new-key": ShortcutSet(["Meta+Shift+K"], ["Ctrl+Shift+K"]),
    "edit-ssh-config": ShortcutSet(["Meta+Shift+E"], ["Ctrl+Shift+E"]),
    "manage-files": ShortcutSet(["Meta+Shift+O"], ["Ctrl+Shift+O"]),
    "local-terminal": ShortcutSet(["Meta+Shift+T"], ["Ctrl+Shift+T"]),
    "preferences": ShortcutSet(["Meta+,"], ["Ctrl+,"]),
    "tab-close": ShortcutSet(["Meta+F4"], ["Ctrl+F4"]),
    "broadcast-command": ShortcutSet(["Meta+Shift+B"], ["Ctrl+Shift+B"]),
    "help": ShortcutSet(["F1"], ["F1"]),
    "shortcuts": ShortcutSet(["Meta+Shift+/"], ["Ctrl+Shift+/"]),
    "tab-next": ShortcutSet(["Alt+Right"], ["Alt+Right"]),
    "tab-prev": ShortcutSet(["Alt+Left"], ["Alt+Left"]),
    "tab-overview": ShortcutSet(["Meta+Shift+Tab"], ["Ctrl+Shift+Tab"]),
    "quick-connect": ShortcutSet(["Meta+Alt+C"], ["Ctrl+Alt+C"]),
}


def get_shortcuts(mac: bool = False) -> Dict[str, List[str]]:
    """Return the default accelerator mapping for the current platform."""

    resolved: Dict[str, List[str]] = {}
    for action, mapping in DEFAULT_SHORTCUTS.items():
        chosen = mapping.mac if mac else mapping.default
        resolved[action] = list(chosen)
    return resolved


@dataclass
class ShortcutRegistrar:
    """Manage ``QShortcut`` registrations for a widget."""

    widget: object
    on_trigger: Callable[[str], None]
    mac: bool = False
    _binding: QtBinding | None = field(init=False, default=None)
    _shortcuts: List[object] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._binding = get_qt_binding()
        if self._binding is None:
            raise RuntimeError("Qt bindings are required to register shortcuts")

    def reinstall(self, mapping: Dict[str, Sequence[str]] | None = None) -> None:
        """Clear and re-register shortcuts for the widget."""

        self._clear()
        QtGui = self._binding.gui
        shortcuts = mapping or get_shortcuts(self.mac)

        for action, accelerators in shortcuts.items():
            for accel in accelerators:
                sequence = QtGui.QKeySequence(accel)
                shortcut = QtGui.QShortcut(sequence, self.widget)
                shortcut.setContext(QtGui.Qt.ShortcutContext.WindowShortcut)
                shortcut.activated.connect(lambda act=action: self.on_trigger(act))
                self._shortcuts.append(shortcut)

    def _clear(self) -> None:
        while self._shortcuts:
            shortcut = self._shortcuts.pop()
            shortcut.setEnabled(False)
            shortcut.setParent(None)
