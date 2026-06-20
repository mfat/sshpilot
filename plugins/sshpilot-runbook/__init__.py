"""Runbook Snippets — per-connection command templates, one-click copy.

A non-protocol sshPilot plugin. Keep the commands you run on each host (e.g.
``journalctl -u {service}``, ``ssh {user}@{host}``) as labelled snippets and copy
the substituted command to the clipboard with one click.

**Copy only:** sshPilot's plugin API has no way to type into a terminal or run a
remote command, so this plugin copies to the clipboard — it never auto-runs.

Capabilities exercised (all from ``sshpilot.plugins.api``):
* a UI page (``ctx.ui.register_page``) + toasts (``ctx.ui.notify``)
* per-plugin structured settings (``ctx.settings`` holds a dict)
* enumerating saved hosts (``ctx.list_connections`` — needs app API >= 1.4)
* reacting to ``CONNECTION_DELETED`` (``ctx.events``) to prune stale snippets

Pure logic (``substitute`` / ``SnippetStore``) has no GTK import and is
unit-tested without a display; ``gi`` is imported lazily inside the page factory.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from sshpilot.plugins.api import Events, PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\{(\w+)\}")


# --- pure logic (no GTK) ----------------------------------------------------

def substitute(template: str, variables: Dict[str, Any]) -> str:
    """Replace ``{host} {user} {port} {nickname}`` (and any provided key) in
    ``template``. Unknown tokens are left untouched, so a snippet that legitimately
    contains braces isn't mangled."""
    def repl(match: "re.Match") -> str:
        key = match.group(1)
        if key in variables and variables[key] is not None:
            return str(variables[key])
        return match.group(0)
    return _TOKEN_RE.sub(repl, template or "")


def connection_vars(info: Any) -> Dict[str, Any]:
    """Build the substitution map from a ConnectionInfo-like object."""
    return {
        "nickname": getattr(info, "nickname", "") or "",
        "host": getattr(info, "host", "") or "",
        "user": getattr(info, "username", "") or "",
        "port": getattr(info, "port", "") or "",
    }


class SnippetStore:
    """A ``{nickname: [{"label","command"}, …]}`` map persisted as one settings
    value. Tolerant of junk read back from JSON."""

    def __init__(self, data: Any = None):
        self._data: Dict[str, List[Dict[str, str]]] = {}
        if isinstance(data, dict):
            for nick, items in data.items():
                if not isinstance(nick, str) or not isinstance(items, list):
                    continue
                clean = [
                    {"label": str(it.get("label", "")).strip(),
                     "command": str(it.get("command", ""))}
                    for it in items
                    if isinstance(it, dict) and str(it.get("command", "")).strip()
                ]
                if clean:
                    self._data[nick] = clean

    def as_dict(self) -> Dict[str, List[Dict[str, str]]]:
        return {nick: list(items) for nick, items in self._data.items()}

    def get(self, nickname: str) -> List[Dict[str, str]]:
        return list(self._data.get(nickname, []))

    def add(self, nickname: str, label: str, command: str) -> bool:
        command = (command or "").strip()
        if not command:
            return False
        self._data.setdefault(nickname, []).append(
            {"label": (label or "").strip() or command, "command": command})
        return True

    def remove(self, nickname: str, index: int) -> bool:
        items = self._data.get(nickname)
        if not items or not (0 <= index < len(items)):
            return False
        items.pop(index)
        if not items:
            del self._data[nickname]
        return True

    def prune(self, nickname: str) -> bool:
        return self._data.pop(nickname, None) is not None

    def prune_missing(self, valid_nicknames: Any) -> int:
        valid = set(valid_nicknames)
        stale = [n for n in self._data if n not in valid]
        for n in stale:
            del self._data[n]
        return len(stale)


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._store = SnippetStore(ctx.settings.get("snippets", {}))
        self._nicknames: List[str] = []
        self._infos: Dict[str, Any] = {}
        self._current: Optional[str] = None
        self._dropdown = None
        self._snippet_box = None
        self._label_entry = None
        self._command_entry = None
        self._status_label = None
        self._reloading = False

        ctx.ui.register_page(
            "runbook", "Runbook", "utilities-terminal-symbolic", self._build_page)
        ctx.events.subscribe(Events.CONNECTION_DELETED, self._on_connection_deleted)
        ctx.events.subscribe(Events.CONNECTION_CREATED, self._on_connection_created)

    def deactivate(self) -> None:
        logger.info("runbook: deactivate")

    def _persist(self) -> None:
        self.ctx.settings.set("snippets", self._store.as_dict())

    # --- event handler ----------------------------------------------------
    def _on_connection_deleted(self, info) -> None:
        if self._store.prune(info.nickname):
            self._persist()
        self._reload_connections()

    def _on_connection_created(self, info) -> None:
        self._reload_connections()

    def _reload_connections(self) -> None:
        if self._dropdown is None:
            return  # page not built yet
        Gtk = self._Gtk
        self._infos = {c.nickname: c for c in self.ctx.list_connections()}
        self._nicknames = list(self._infos.keys())
        self._reloading = True
        self._dropdown.set_model(
            Gtk.StringList.new(self._nicknames or ["(no connections)"]))
        if self._current in self._nicknames:
            self._dropdown.set_selected(self._nicknames.index(self._current))
        self._reloading = False
        if self._current is None and self._nicknames:
            self._current = self._nicknames[0]
        self._rebuild_snippets()

    # --- UI (gi imported lazily) ------------------------------------------
    def _build_page(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        self._Gtk = Gtk
        self._Adw = Adw

        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for fn in (box.set_margin_top, box.set_margin_bottom,
                   box.set_margin_start, box.set_margin_end):
            fn(18)
        outer.set_child(box)

        title = Gtk.Label(label="Runbook Snippets")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        subtitle = Gtk.Label(
            label="Per-connection command templates. {host} {user} {port} "
                  "{nickname} are filled in; Copy puts the result on the "
                  "clipboard (it never runs anything).")
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_wrap(True)
        subtitle.set_xalign(0)
        box.append(subtitle)

        # Connection picker (prune stale snippets first).
        self._infos = {c.nickname: c for c in self.ctx.list_connections()}
        self._nicknames = list(self._infos.keys())
        if self._store.prune_missing(self._nicknames):
            self._persist()

        picker = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        picker.append(Gtk.Label(label="Connection:"))
        self._dropdown = Gtk.DropDown.new_from_strings(
            self._nicknames or ["(no connections)"])
        self._dropdown.set_hexpand(True)
        self._dropdown.connect("notify::selected", self._on_selection_changed)
        picker.append(self._dropdown)
        box.append(picker)

        self._snippet_group = Adw.PreferencesGroup(title="Snippets")
        box.append(self._snippet_group)

        add_group = Adw.PreferencesGroup(title="Add a snippet")
        self._label_entry = Adw.EntryRow(title="Label")
        self._command_entry = Adw.EntryRow(title="Command (use {host}, {user}, …)")
        add_group.add(self._label_entry)
        add_group.add(self._command_entry)
        add_btn = Gtk.Button(label="Add snippet")
        add_btn.add_css_class("suggested-action")
        add_btn.set_halign(Gtk.Align.START)
        add_btn.connect("clicked", self._on_add_clicked)
        add_group.add(add_btn)
        box.append(add_group)

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("dim-label")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        if self._nicknames:
            self._current = self._nicknames[0]
        self._rebuild_snippets()
        return outer

    def _selected_nickname(self) -> Optional[str]:
        if not self._nicknames:
            return None
        index = self._dropdown.get_selected()
        if 0 <= index < len(self._nicknames):
            return self._nicknames[index]
        return None

    def _on_selection_changed(self, _dropdown, _param) -> None:
        if self._reloading:
            return  # model swap during a connection-list refresh, not a user pick
        self._current = self._selected_nickname()
        self._rebuild_snippets()

    def _rebuild_snippets(self) -> None:
        Adw, Gtk = self._Adw, self._Gtk
        group = self._snippet_group
        for row in getattr(self, "_snippet_rows", []):
            group.remove(row)
        self._snippet_rows = []

        if not self._current:
            empty = Adw.ActionRow(title="No connection selected")
            group.add(empty)
            self._snippet_rows.append(empty)
            return

        snippets = self._store.get(self._current)
        if not snippets:
            empty = Adw.ActionRow(title="No snippets yet")
            empty.set_subtitle("Add one below.")
            group.add(empty)
            self._snippet_rows.append(empty)
            return

        info = self._infos.get(self._current)
        variables = connection_vars(info) if info is not None else {}
        for index, snip in enumerate(snippets):
            rendered = substitute(snip["command"], variables)
            row = Adw.ActionRow(title=snip["label"])
            row.set_subtitle(rendered)
            row.set_subtitle_lines(1)
            copy = Gtk.Button(icon_name="edit-copy-symbolic")
            copy.add_css_class("flat")
            copy.set_valign(Gtk.Align.CENTER)
            copy.set_tooltip_text("Copy command")
            copy.connect("clicked", self._on_copy_clicked, rendered)
            row.add_suffix(copy)
            delete = Gtk.Button(icon_name="user-trash-symbolic")
            delete.add_css_class("flat")
            delete.set_valign(Gtk.Align.CENTER)
            delete.set_tooltip_text("Delete snippet")
            delete.connect("clicked", self._on_delete_clicked, index)
            row.add_suffix(delete)
            group.add(row)
            self._snippet_rows.append(row)

    def _on_add_clicked(self, _btn) -> None:
        if not self._current:
            self._set_status("Select a connection first.")
            return
        label = self._label_entry.get_text().strip()
        command = self._command_entry.get_text().strip()
        if not command:
            self._set_status("A command is required.")
            return
        self._store.add(self._current, label, command)
        self._persist()
        self._label_entry.set_text("")
        self._command_entry.set_text("")
        self._rebuild_snippets()
        self._set_status("Snippet added.")

    def _on_delete_clicked(self, _btn, index: int) -> None:
        if self._current and self._store.remove(self._current, index):
            self._persist()
            self._rebuild_snippets()
            self._set_status("Snippet removed.")

    def _on_copy_clicked(self, button, rendered: str) -> None:
        try:
            button.get_clipboard().set(rendered)
        except Exception:
            logger.debug("clipboard set failed", exc_info=True)
            self._set_status("Could not copy.")
            return
        self._set_status("Copied to clipboard.")
        self.ctx.ui.notify("Command copied to clipboard")

    def _set_status(self, text: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(text)
