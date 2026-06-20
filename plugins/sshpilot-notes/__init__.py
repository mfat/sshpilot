"""Connection Notes — keep a freeform note per saved connection.

A non-protocol sshPilot plugin. Pick a connection, jot anything (credentials
reminders, runbook steps, "don't reboot on Fridays"), and it's saved per host.
Notes for deleted connections are pruned automatically.

Capabilities exercised (all from ``sshpilot.plugins.api``):
* a UI page (``ctx.ui.register_page``) with a connection picker + editor
* enumerating saved hosts (``ctx.list_connections`` — needs app API >= 1.4)
* per-plugin persisted, structured settings (``ctx.settings`` holds a dict)
* reacting to ``CONNECTION_DELETED`` (``ctx.events``) to prune stale notes

Known limitation: the ``CONNECTION_UPDATED`` event carries only the *current*
nickname, so a note cannot follow a rename automatically — it stays under the
old nickname until pruned. See README.

Pure logic (``NoteStore``) has no GTK import and is unit-tested without a
display; ``gi`` is imported lazily inside the page factory.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

from sshpilot.plugins.api import Events, PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)


# --- pure logic (no GTK) ----------------------------------------------------

class NoteStore:
    """A nickname → note-text map. Empty notes are not stored; round-trips
    through JSON (settings), so it tolerates a non-dict starting value."""

    def __init__(self, data: Any = None):
        self._data: Dict[str, str] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(key, str) and isinstance(value, str) and value:
                    self._data[key] = value

    def as_dict(self) -> Dict[str, str]:
        return dict(self._data)

    def get(self, nickname: str) -> str:
        return self._data.get(nickname, "")

    def set(self, nickname: str, text: str) -> bool:
        """Store (or, for empty text, clear) a note. Returns True if anything
        changed."""
        text = (text or "").strip("\n")
        if text:
            changed = self._data.get(nickname) != text
            self._data[nickname] = text
            return changed
        return self._data.pop(nickname, None) is not None

    def prune(self, nickname: str) -> bool:
        return self._data.pop(nickname, None) is not None

    def prune_missing(self, valid_nicknames: Iterable[str]) -> int:
        """Drop notes whose connection no longer exists. Returns the count
        removed."""
        valid = set(valid_nicknames)
        stale = [nick for nick in self._data if nick not in valid]
        for nick in stale:
            del self._data[nick]
        return len(stale)


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._notes = NoteStore(ctx.settings.get("notes", {}))
        self._nicknames: list = []
        self._current: Optional[str] = None
        self._buffer = None
        self._dropdown = None
        self._status_label = None

        ctx.ui.register_page(
            "notes", "Notes", "text-editor-symbolic", self._build_page)
        ctx.events.subscribe(Events.CONNECTION_DELETED, self._on_connection_deleted)

    def deactivate(self) -> None:
        logger.info("notes: deactivate")

    # --- persistence ------------------------------------------------------
    def _persist(self) -> None:
        self.ctx.settings.set("notes", self._notes.as_dict())

    # --- event handler (main thread) -------------------------------------
    def _on_connection_deleted(self, info) -> None:
        if self._notes.prune(info.nickname):
            self._persist()

    # --- UI (gi imported lazily; only runs inside the app) ----------------
    def _build_page(self):
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        self._Gtk = Gtk

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for fn in (box.set_margin_top, box.set_margin_bottom,
                   box.set_margin_start, box.set_margin_end):
            fn(18)

        title = Gtk.Label(label="Connection Notes")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        # Drop stale notes the moment the page is opened, then build the picker.
        self._nicknames = [c.nickname for c in self.ctx.list_connections()]
        if self._notes.prune_missing(self._nicknames):
            self._persist()

        picker_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        picker_row.append(Gtk.Label(label="Connection:"))
        self._dropdown = Gtk.DropDown.new_from_strings(
            self._nicknames or ["(no connections)"])
        self._dropdown.set_hexpand(True)
        self._dropdown.connect("notify::selected", self._on_selection_changed)
        picker_row.append(self._dropdown)
        box.append(picker_row)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        textview = Gtk.TextView()
        textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        textview.set_monospace(False)
        self._buffer = textview.get_buffer()
        scroller.set_child(textview)
        box.append(scroller)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        save_btn = Gtk.Button(label="Save note")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_clicked)
        actions.append(save_btn)
        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("dim-label")
        actions.append(self._status_label)
        box.append(actions)

        if self._nicknames:
            self._load_into_editor(self._nicknames[0])
        else:
            textview.set_sensitive(False)
            save_btn.set_sensitive(False)

        return box

    def _selected_nickname(self) -> Optional[str]:
        if not self._nicknames:
            return None
        index = self._dropdown.get_selected()
        if 0 <= index < len(self._nicknames):
            return self._nicknames[index]
        return None

    def _load_into_editor(self, nickname: str) -> None:
        self._current = nickname
        self._buffer.set_text(self._notes.get(nickname))

    def _on_selection_changed(self, _dropdown, _param) -> None:
        # Save the note we're leaving, then load the newly selected one.
        self._save_current()
        nickname = self._selected_nickname()
        if nickname is not None:
            self._load_into_editor(nickname)

    def _buffer_text(self) -> str:
        start, end = self._buffer.get_bounds()
        return self._buffer.get_text(start, end, True)

    def _save_current(self) -> bool:
        if self._current is None:
            return False
        changed = self._notes.set(self._current, self._buffer_text())
        if changed:
            self._persist()
        return changed

    def _on_save_clicked(self, _btn) -> None:
        if self._current is None:
            return
        self._save_current()
        self._set_status(f"Saved note for {self._current}")
        self.ctx.ui.notify(f"Note saved for {self._current}")

    def _set_status(self, text: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(text)
