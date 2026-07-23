"""Global omni-search result collection, ranking, and GTK presentation."""

from __future__ import annotations

import difflib
import gettext
import re
import shlex
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gtk

from .cli_connect import validate_cli_tokens

_ = gettext.gettext

_MAX_RESULTS = 8
_TRANSFER_INTENTS = {
    "sftp": ("sftp", _("SFTP File Manager"), "folder-remote-symbolic"),
    "scp": ("scp", _("Transfer Files with SCP"), "folder-remote-symbolic"),
    "ssh-copy-id": (
        "ssh-copy-id",
        _("Copy Key to Server"),
        "dialog-password-symbolic",
    ),
}

# Keep the source strings as well as their translations searchable.
_ACTION_ALIASES = {
    "app.new-connection": (
        "add server", "add connection", "create host", "create server",
        "new host", "new machine",
    ),
    "app.local-terminal": (
        "shell", "terminal", "local shell", "command line",
    ),
    "app.preferences": (
        "settings", "options", "configure app", "configuration",
    ),
    "app.edit-ssh-config": (
        "edit ssh config", "ssh configuration", "configure hosts", "ssh config",
    ),
    "win.edit-known-hosts": (
        "known hosts", "host keys", "fingerprints", "server fingerprints",
    ),
    "win.manage-local-authorized-keys": (
        "authorized keys", "public keys", "access keys", "authorized_keys",
    ),
    "win.open-file-manager": (
        "sftp", "browse files", "remote files", "upload", "download",
    ),
    "app.new-key": (
        "ssh-copy-id", "copy key", "install key", "deploy public key",
    ),
}


@dataclass(frozen=True)
class CommandSpec:
    title: str
    action: str
    target: Any = None
    aliases: Tuple[str, ...] = ()
    icon_name: str = "application-x-executable-symbolic"


@dataclass(frozen=True)
class OmniResult:
    kind: str
    title: str
    subtitle: str
    icon_name: str
    score: int
    payload: Any = None
    enabled: bool = True


def _menu_string(model, index: int, attr: str) -> Optional[str]:
    value = model.get_item_attribute_value(index, attr, None)
    return value.get_string() if value is not None else None


def _walk_menu(model, out: List[Tuple[str, str, Any]]) -> None:
    for index in range(model.get_n_items()):
        label = _menu_string(model, index, "label")
        action = _menu_string(model, index, "action")
        if label and action:
            target = model.get_item_attribute_value(index, "target", None)
            out.append((label, action, target))
        links = model.iterate_item_links(index)
        while links.next():
            _walk_menu(links.get_value(), out)


def collect_commands(window) -> List[CommandSpec]:
    """Return the current main-menu actions, including plugin contributions."""
    pairs: List[Tuple[str, str, Any]] = []
    try:
        _walk_menu(window.create_menu(), pairs)
    except Exception:
        return []

    commands: List[CommandSpec] = []
    seen = set()
    for label, action, target in pairs:
        target_key = target.print_(False) if hasattr(target, "print_") else str(target)
        key = (action, target_key)
        if key in seen:
            continue
        seen.add(key)
        source_aliases = _ACTION_ALIASES.get(action, ())
        translated = tuple(_(alias) for alias in source_aliases)
        aliases = tuple(dict.fromkeys((*source_aliases, *translated)))
        commands.append(CommandSpec(
            title=label.replace("_", ""),
            action=action,
            target=target,
            aliases=aliases,
        ))
    return commands


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[\w@.-]+", (value or "").casefold()))


def _match_score(query: str, phrases: Iterable[str]) -> int:
    normalized_query = _normalize(query)
    if not normalized_query:
        return 0
    query_tokens = normalized_query.split()
    best = 0
    for phrase in phrases:
        normalized = _normalize(phrase)
        if not normalized:
            continue
        if normalized == normalized_query:
            best = max(best, 1000)
            continue
        if normalized.startswith(normalized_query):
            best = max(best, 820)
            continue
        if all(token in normalized for token in query_tokens):
            best = max(best, 650)
            continue
        if len(normalized_query) >= 4:
            ratio = difflib.SequenceMatcher(None, normalized_query, normalized).ratio()
            if ratio >= 0.72:
                best = max(best, 350 + int(ratio * 100))
    return best


def _connection_phrases(connection) -> Tuple[str, ...]:
    nickname = str(getattr(connection, "nickname", "") or "")
    display_name = str(getattr(connection, "display_name", "") or "")
    host = str(
        getattr(connection, "hostname", "")
        or getattr(connection, "host", "")
        or ""
    )
    user = str(getattr(connection, "username", "") or "")
    tags = tuple(str(tag) for tag in (getattr(connection, "tags", None) or ()))
    target = f"{user}@{host}" if user and host else host
    return tuple(value for value in (nickname, display_name, host, target, *tags) if value)


def _connection_result(connection, score: int) -> OmniResult:
    phrases = _connection_phrases(connection)
    title = str(getattr(connection, "display_name", "") or phrases[0])
    host = str(
        getattr(connection, "hostname", "")
        or getattr(connection, "host", "")
        or ""
    )
    user = str(getattr(connection, "username", "") or "")
    subtitle = f"{user}@{host}" if user and host else host
    return OmniResult(
        "connection", title, subtitle, "network-server-symbolic",
        score, connection,
    )


def _find_saved_alias(connections: Sequence[Any], token: str):
    folded = token.casefold()
    for connection in connections:
        if str(getattr(connection, "nickname", "")).casefold() == folded:
            return connection
    return None


def _parse_tokens(query: str) -> Tuple[Optional[List[str]], Optional[str]]:
    try:
        return shlex.split(query), None
    except ValueError as exc:
        return None, str(exc)


def _looks_like_ssh(query: str, tokens: Sequence[str]) -> bool:
    if not tokens:
        return False
    first = tokens[0].casefold()
    if first == "ssh":
        return True
    if first.startswith("-") or "@" in first:
        return True
    return bool(re.fullmatch(
        r"(?:\d{1,3}\.){3}\d{1,3}|(?:[a-z0-9-]+\.)+[a-z0-9-]+",
        first,
        re.IGNORECASE,
    ))


def _intent_result(query: str, connections: Sequence[Any]) -> Optional[OmniResult]:
    tokens, _error = _parse_tokens(query)
    if not tokens:
        return None
    command = tokens[0].casefold()
    intent = _TRANSFER_INTENTS.get(command)
    if intent is None:
        return None
    connection = _find_saved_alias(connections, tokens[1]) if len(tokens) == 2 else None
    subtitle = (
        _("Use saved connection {name}").format(
            name=getattr(connection, "nickname", ""),
        )
        if connection is not None
        else _("Choose a connection")
    )
    return OmniResult(
        "transfer", intent[1], subtitle, intent[2], 1400,
        (intent[0], connection),
    )


def _ssh_result(query: str) -> Optional[OmniResult]:
    tokens, parse_error = _parse_tokens(query)
    if tokens is None:
        if query.strip().casefold().startswith("ssh"):
            return OmniResult(
                "validation", _("Invalid SSH command"), parse_error or "",
                "dialog-warning-symbolic", 1300, enabled=False,
            )
        return None
    if not _looks_like_ssh(query, tokens):
        return None
    error = validate_cli_tokens(tokens)
    if error:
        return OmniResult(
            "validation", _("Invalid SSH command"), _(error),
            "dialog-warning-symbolic", 1300, enabled=False,
        )
    display = shlex.join(tokens)
    return OmniResult(
        "ssh", _("Connect using SSH"), display,
        "utilities-terminal-symbolic", 1350, tuple(tokens),
    )


def _recent_and_pinned(window, connections: Sequence[Any]) -> List[Any]:
    by_name = {
        str(getattr(connection, "nickname", "")): connection
        for connection in connections
    }
    ordered: List[Any] = []
    try:
        for nickname in window.config.get_pinned_nicknames():
            if nickname in by_name and by_name[nickname] not in ordered:
                ordered.append(by_name[nickname])
    except Exception:
        pass

    def last_used(connection):
        try:
            return window.config.get_connection_meta(
                connection.nickname,
            ).get("last_used", 0) or 0
        except Exception:
            return 0

    for connection in sorted(connections, key=last_used, reverse=True):
        if last_used(connection) and connection not in ordered:
            ordered.append(connection)
    return ordered


def search_omni(window, query: str, limit: int = _MAX_RESULTS) -> List[OmniResult]:
    """Return ranked, deduplicated omni-search results."""
    connections = list(window.connection_manager.get_connections())
    commands = collect_commands(window)
    query = query.strip()

    if not query:
        suggestions: List[OmniResult] = []
        for index, connection in enumerate(_recent_and_pinned(window, connections)):
            suggestions.append(_connection_result(connection, 900 - index))
        common = (
            "app.new-connection",
            "app.local-terminal",
            "app.preferences",
            "app.edit-ssh-config",
        )
        by_action = {command.action: command for command in commands}
        for index, action in enumerate(common):
            command = by_action.get(action)
            if command is not None:
                suggestions.append(OmniResult(
                    "command", command.title, _("Command"), command.icon_name,
                    700 - index, command,
                ))
        return suggestions[:limit]

    results: List[OmniResult] = []
    intent = _intent_result(query, connections)
    if intent is not None:
        results.append(intent)

    ssh = _ssh_result(query)
    if ssh is not None:
        results.append(ssh)

    for connection in connections:
        score = _match_score(query, _connection_phrases(connection))
        if score:
            results.append(_connection_result(connection, score + 50))

    for command in commands:
        score = _match_score(query, (command.title, *command.aliases))
        if score:
            results.append(OmniResult(
                "command", command.title, _("Command"), command.icon_name,
                score, command,
            ))

    results.sort(key=lambda result: (-result.score, result.title.casefold()))
    deduped: List[OmniResult] = []
    seen = set()
    for result in results:
        if result.kind == "command":
            spec = result.payload
            key = ("command", spec.action, str(spec.target))
        elif result.kind == "connection":
            key = ("connection", getattr(result.payload, "nickname", result.title))
        else:
            key = (result.kind, result.title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
        if len(deduped) >= limit:
            break
    return deduped


class OmniSearchController:
    """Own the shared welcome/overlay omni-search widget."""

    def __init__(self, window, overlay: Gtk.Overlay, home: Adw.Bin):
        self.window = window
        self.home = home
        self._results: List[OmniResult] = []
        self._previous_focus = None
        self._anchored = False

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content.add_css_class("omni-search")

        self.entry = Gtk.SearchEntry()
        self.entry.set_can_focus(True)
        self.entry.set_placeholder_text(
            _("Search connections, tools, or type a SSH command")
        )
        self.entry.add_css_class("omni-search-entry")
        self.entry.connect("search-changed", self._on_search_changed)
        self.entry.connect("activate", lambda *_args: self.activate_selected())
        self.entry.connect("stop-search", lambda *_args: self.dismiss())
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(key)
        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_args: self._request_show())
        self.entry.add_controller(click)
        self.content.append(self.entry)

        self.results_scroller = Gtk.ScrolledWindow()
        self.results_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC,
        )
        self.results_scroller.set_max_content_height(420)
        self.results_scroller.set_propagate_natural_height(True)
        self.results_scroller.add_css_class("omni-search-results")
        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.results.set_can_focus(False)
        self.results.add_css_class("navigation-sidebar")
        self.results.connect("row-activated", self._on_row_activated)
        self.results_scroller.set_child(self.results)
        self.content.append(self.results_scroller)

        # The real entry lives on the welcome page; showing the popup detaches
        # the whole content box into the floating panel and hiding re-docks it.
        home.set_child(self.content)
        from .search_popup import SearchPopup
        self.popup = SearchPopup(
            overlay,
            home,
            self.content,
            lambda: max(320, min(620, overlay.get_width() - 32)),
            on_shown=self._on_popup_shown,
            on_hidden=self._on_popup_hidden,
            on_dismiss=self.dismiss,
            focus_func=self._entry_focus_widget,
        )
        self.popup.set_anchor(home)
        self._set_results_visible(False)

    def _set_results_visible(self, visible: bool) -> None:
        self.results_scroller.set_visible(visible)
        if visible:
            self.content.add_css_class("omni-search-open")
        else:
            self.content.remove_css_class("omni-search-open")

    def _entry_focus_widget(self):
        delegate = self.entry.get_delegate()
        return delegate if delegate is not None else self.entry

    def _start_is_visible(self) -> bool:
        if not self.window.is_start_tab_selected():
            return False
        nav = getattr(self.window, "nav_view", None)
        work = getattr(self.window, "_work_page", None)
        if nav is None:
            return True
        try:
            return nav.get_visible_page() is work
        except Exception:
            return True

    def _apply_presentation(self) -> None:
        self._anchored = self._start_is_visible()
        if self._anchored:
            self.popup.apply_preset("anchored")
        else:
            self.popup.apply_preset("omni")

    def show(self, select_all: bool = True) -> None:
        if self.popup.visible:
            self._apply_presentation()
            if select_all and self.entry.get_text():
                self.entry.select_region(0, -1)
            self._entry_focus_widget().grab_focus()
            return
        try:
            self._previous_focus = self.window.get_focus()
        except Exception:
            self._previous_focus = None
        self._apply_presentation()
        self._rebuild()
        self.popup.show()
        self._entry_focus_widget().grab_focus()
        if select_all and self.entry.get_text():
            self.entry.select_region(0, -1)

    def dismiss(self, clear: bool = False) -> None:
        if clear:
            self.entry.set_text("")
        self.popup.hide()

    def _request_show(self) -> None:
        # Deferred: showing reparents the entry, which must not happen while
        # the click/key event that triggered it is still being dispatched.
        if not self.popup.visible:
            GLib.idle_add(lambda: (self.show(False), GLib.SOURCE_REMOVE)[1])

    def _on_popup_shown(self) -> None:
        self._set_results_visible(True)
        self._rebuild()

    def _on_popup_hidden(self) -> None:
        self._set_results_visible(False)
        if self._anchored:
            self._entry_focus_widget().grab_focus()
        elif self._previous_focus is not None:
            try:
                self._previous_focus.grab_focus()
            except Exception:
                pass
        self._previous_focus = None

    def _clear_rows(self) -> None:
        child = self.results.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.results.remove(child)
            child = following

    def _make_row(self, result: OmniResult) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.omni_result = result
        row.set_can_focus(False)
        row.set_activatable(result.enabled)
        row.set_selectable(result.enabled)
        row.add_css_class("omni-result")
        if not result.enabled:
            row.set_sensitive(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(9)
        box.set_margin_bottom(9)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.append(Gtk.Image.new_from_icon_name(result.icon_name))

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        labels.set_hexpand(True)
        title = Gtk.Label(label=result.title, xalign=0)
        title.set_ellipsize(3)
        labels.append(title)
        if result.subtitle:
            subtitle = Gtk.Label(label=result.subtitle, xalign=0)
            subtitle.add_css_class("caption")
            subtitle.add_css_class("dim-label")
            subtitle.set_ellipsize(3)
            labels.append(subtitle)
        box.append(labels)
        row.set_child(box)
        return row

    def _rebuild(self) -> None:
        self._results = search_omni(self.window, self.entry.get_text())
        self._clear_rows()
        for result in self._results:
            self.results.append(self._make_row(result))
        first = self._first_enabled_row()
        if first is not None:
            self.results.select_row(first)

    def _first_enabled_row(self):
        index = 0
        while True:
            row = self.results.get_row_at_index(index)
            if row is None:
                return None
            if getattr(row, "omni_result", None) and row.omni_result.enabled:
                return row
            index += 1

    def _on_search_changed(self, *_args) -> None:
        if self.popup.visible:
            self._rebuild()
        elif self.entry.get_text():
            self._request_show()

    def _on_entry_key(self, _controller, keyval, _keycode, _state):
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            row = self.results.get_selected_row() or self._first_enabled_row()
            if row is None:
                return True
            step = 1 if keyval == Gdk.KEY_Down else -1
            index = row.get_index()
            while True:
                index += step
                candidate = self.results.get_row_at_index(index)
                if candidate is None:
                    break
                result = getattr(candidate, "omni_result", None)
                if result is not None and result.enabled:
                    self.results.select_row(candidate)
                    break
            return True
        return False

    def _on_row_activated(self, _list_box, row) -> None:
        self.activate_result(getattr(row, "omni_result", None))

    def activate_selected(self) -> None:
        if not self.popup.visible:
            self._request_show()
            return
        row = self.results.get_selected_row() or self._first_enabled_row()
        self.activate_result(getattr(row, "omni_result", None) if row else None)

    def activate_result(self, result: Optional[OmniResult]) -> None:
        if result is None or not result.enabled:
            return
        self.dismiss(clear=True)
        if result.kind == "connection":
            self.window._return_to_tab_view_if_welcome()
            self.window._cycle_connection_tabs_or_open(result.payload)
        elif result.kind == "command":
            spec = result.payload
            Gtk.Widget.activate_action(self.window, spec.action, spec.target)
        elif result.kind == "ssh":
            self.window.open_cli_connect(list(result.payload))
        elif result.kind == "transfer":
            intent, connection = result.payload
            self.window.open_omni_transfer_intent(intent, connection)
