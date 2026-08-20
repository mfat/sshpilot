"""Global omni-search result collection, ranking, and GTK presentation."""

from __future__ import annotations

import difflib
import gettext
import math
import re
import shlex
import warnings
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("cairo", "1.0")
from gi.repository import Adw, Gdk, GLib, Gtk

# Cairo is only needed for the attention tracer's drawing callback; keep it
# optional so import stays possible on minimal CI images that stub gi.
try:
    from gi.repository import cairo as Gcairo
except Exception:  # pragma: no cover - only where gi cairo is absent
    Gcairo = None

from .cli_connect import validate_cli_tokens
from .shortcut_utils import DOUBLE_SHIFT_SHORTCUT

_ = gettext.gettext

_MAX_RESULTS = 8

# Transient "look here" tracer on the docked omni-search box when the Start tab
# becomes active: a short accent-colored segment travels clockwise once around
# the rounded border. Kept separate from :focus-within so the resting outline
# and the real focused/open state keep their distinctive meanings.
# The tracer's one full clockwise lap around the border. 700 ms read as
# fidgety; 3500 ms is a single slow, calm pass that still completes within a
# normal attention hold.
_ATTENTION_MS = 1500
# How many full clockwise laps the tracer makes before retiring. 1 = a single
# pass (the original behaviour); 0 = loop until the guard stops it (leaving
# Start or opening Omnisearch). Positive N = exactly N laps.
_ATTENTION_LAPS = 2
# Visible tracer length as a fraction of the border perimeter (15-25% reads as
# a short moving segment rather than a partial outline).
_ATTENTION_SEGMENT_FRACTION = 0.95
_ATTENTION_STROKE_WIDTH = 0.5
_ATTENTION_RADIUS = 8.0
# Settle delay before the boot-time tracer starts: the ``map`` signal can fire
# a frame before the window is actually usable. Only affects the initial
# mapped/startup presentation; normal Start transitions begin immediately.
_ATTENTION_DEBUT_DELAY_MS = 350
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


# --- attention tracer geometry (pure maths, unit-testable) ------------------


def _rounded_rect_perimeter(width: float, height: float, radius: float) -> float:
    """Perimeter of the inset rounded rectangle the tracer travels around.

    Four straight sections plus four quarter-circle corner arcs:
    ``2*(w-2r) + 2*(h-2r) + 2*pi*r``. The corner radius is clamped so it never
    exceeds half of the smaller dimension.
    """
    r = max(0.0, min(float(radius), min(float(width), float(height)) / 2.0))
    return 2.0 * (width - 2.0 * r) + 2.0 * (height - 2.0 * r) + 2.0 * math.pi * r


def _tracer_progress(elapsed_us: int, duration_ms: int, laps: int = 1) -> float:
    """Map elapsed frame-clock time (microseconds) into tracer progress.

    Single lap: clamps to ``1.0`` when the lap finishes. Multiple laps: the
    progress wraps to the current lap's phase (``0 <= p < 1``) and returns
    ``1.0`` once the final lap is done. ``laps <= 0`` loops forever, wrapping
    each lap, until the caller's guard retires the tracer.
    """
    if duration_ms <= 0:
        return 1.0
    t = elapsed_us / (1000.0 * duration_ms)
    if laps <= 0:
        return max(0.0, t % 1.0)
    if laps == 1:
        return max(0.0, min(1.0, t))
    if t >= laps:
        return 1.0
    return max(0.0, t % 1.0)


def _tracer_dash_offset(progress: float, perimeter: float) -> float:
    """Dash phase that moves the visible segment clockwise along the path.

    Cairo shifts the dash pattern backwards for positive offsets, so the
    negative offset sweeps the segment forward along the path direction (the
    continuous rounded-rect path below is traced clockwise). One exact lap by
    ``progress == 1.0`` returns the segment to its start.
    """
    return -progress * perimeter


def _draw_attention_tracer(
    cr, width: float, height: float, progress: float, color=None
) -> None:
    """Stroke one accent segment of the Omnisearch border, if the geometry and
    cairo are usable; no-op otherwise. Reads the live ``width``/``height`` on
    every call so a mid-animation resize just re-derives the path."""
    if Gcairo is None or width <= 0.0 or height <= 0.0 or progress is None:
        return
    inset = _ATTENTION_STROKE_WIDTH / 2.0
    r = max(0.0, min(_ATTENTION_RADIUS, min(width, height) / 2.0 - inset))
    w = width - 2.0 * inset
    h = height - 2.0 * inset
    x = inset
    y = inset
    cr.save()
    try:
        # Single continuous path, clockwise (right-hand turns), starting on the
        # top edge just past the upper-left corner.
        cr.move_to(x + r, y)
        cr.line_to(x + w - r, y)
        cr.arc(x + w - r, y + r, r, -math.pi / 2.0, 0.0)
        cr.line_to(x + w, y + h - r)
        cr.arc(x + w - r, y + h - r, r, 0.0, math.pi / 2.0)
        cr.line_to(x + r, y + h)
        cr.arc(x + r, y + h - r, r, math.pi / 2.0, math.pi)
        cr.line_to(x, y + r)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()
        perimeter = _rounded_rect_perimeter(w, h, r)
        if perimeter <= 0.0:
            return
        segment = perimeter * _ATTENTION_SEGMENT_FRACTION
        cr.set_dash(
            [segment, max(0.0, perimeter - segment)],
            _tracer_dash_offset(progress, perimeter),
        )
        cr.set_line_width(_ATTENTION_STROKE_WIDTH)
        cr.set_line_cap(Gcairo.LineCap.ROUND)
        if color is not None:
            cr.set_source_rgba(float(color.red), float(color.green),
                               float(color.blue), float(color.alpha))
        cr.stroke()
    finally:
        cr.restore()


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


def _transfer_result(intent, connection, score: int) -> OmniResult:
    title = str(
        getattr(connection, "display_name", "")
        or getattr(connection, "nickname", "")
    )
    return OmniResult(
        "transfer", title, intent[1], intent[2], score,
        (intent[0], connection),
    )


def _intent_results(
    window, query: str, connections: Sequence[Any]
) -> List[OmniResult]:
    tokens, _error = _parse_tokens(query)
    if not tokens:
        return []
    intent = _TRANSFER_INTENTS.get(tokens[0].casefold())
    if intent is None:
        return []

    chooser = OmniResult(
        "transfer", intent[1], _("Choose a connection"), intent[2], 1400,
        (intent[0], None),
    )

    if len(tokens) == 1:
        # Bare tool: offer the chooser plus recent/pinned hosts to run it on.
        results = [chooser]
        for index, connection in enumerate(
            _recent_and_pinned(window, connections)[:5]
        ):
            results.append(_transfer_result(intent, connection, 1390 - index))
        return results

    if len(tokens) == 2:
        # "sftp <partial>": fuzzy-match hosts, exact alias first.
        matches = []
        for connection in connections:
            score = _match_score(tokens[1], _connection_phrases(connection))
            if score:
                matches.append((score, connection))
        matches.sort(key=lambda item: -item[0])
        if not matches:
            return [chooser]
        return [
            _transfer_result(intent, connection, 900 + score // 2)
            for score, connection in matches[:5]
        ]

    return [chooser]


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


def _ssh_host_suggestions(
    window, query: str, connections: Sequence[Any]
) -> List[OmniResult]:
    """Saved connections matching the destination of an ssh-like query.

    "ssh g" or "root@g" scores connections against just "g", so saved hosts
    surface next to the ad-hoc "Connect using SSH" row; a bare "ssh" offers
    recent/pinned. Callers gate on the query already looking like ssh.
    """
    tokens, _error = _parse_tokens(query)
    if not tokens:
        return []
    rest = tokens[1:] if tokens[0].casefold() == "ssh" else tokens
    if not rest:
        return [
            _connection_result(connection, 1390 - index)
            for index, connection in enumerate(
                _recent_and_pinned(window, connections)[:5]
            )
        ]
    dest = rest[-1]
    if dest.startswith("-"):
        return []
    dest = dest.rsplit("@", 1)[-1]
    if not dest:
        return []
    matches = []
    for connection in connections:
        score = _match_score(dest, _connection_phrases(connection))
        if score:
            matches.append((score, connection))
    matches.sort(key=lambda item: -item[0])
    return [
        _connection_result(connection, 900 + score // 2)
        for score, connection in matches[:5]
    ]


def _recent_and_pinned(window, connections: Sequence[Any]) -> List[Any]:
    by_name = {
        str(getattr(connection, "nickname", "")): connection
        for connection in connections
    }
    ordered: List[Any] = []
    try:
        for nickname in (
            item.connection_id
            for item in window.connection_manager.metadata
            if item.values.get("pinned")
        ):
            if nickname in by_name and by_name[nickname] not in ordered:
                ordered.append(by_name[nickname])
    except Exception:
        pass

    def last_used(connection):
        try:
            return window.connection_manager.get_metadata(connection.nickname).get(
                "last_used", 0
            ) or 0
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
    results.extend(_intent_results(window, query, connections))

    ssh = _ssh_result(query)
    if ssh is not None:
        results.append(ssh)
        results.extend(_ssh_host_suggestions(window, query, connections))

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

        self.content = Gtk.Overlay()
        self.content.add_css_class("omni-search")
        self._content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.content.set_child(self._content_box)

        # Decorative attention-tracer layer: draws the traveling accent
        # segment while Start attention is active. Never takes focus or
        # pointer input (can_target(False) lets clicks reach the entry below).
        self._attention_area = Gtk.DrawingArea()
        self._attention_area.set_can_focus(False)
        self._attention_area.set_can_target(False)
        self._attention_area.set_hexpand(True)
        self._attention_area.set_vexpand(True)
        self._attention_area.set_draw_func(self._on_attention_draw, None)
        self.content.add_overlay(self._attention_area)

        self._attention_active = False
        self._attention_progress = 0.0
        self._attention_start_us = None
        self._attention_color = None
        self._attention_tick_id = None
        self._attention_start_source_id = None
        self._attention_map_handler_id = None
        self.content.connect("destroy", self._on_attention_owner_destroyed)

        self.entry = Gtk.SearchEntry()
        self.entry.set_can_focus(True)
        self._update_placeholder()
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
        self._content_box.append(self.entry)

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
        self._content_box.append(self.results_scroller)

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

    def _update_placeholder(self) -> None:
        """Placeholder mentions the current omnisearch shortcut, if any."""
        label = ""
        try:
            app = self.window.get_application()
            accels = app.get_effective_shortcuts("omnisearch") or []
            if accels:
                if accels[0] == DOUBLE_SHIFT_SHORTCUT:
                    self.entry.set_placeholder_text(
                        _("Press Shift twice to search connections, tools or type ssh commands")
                    )
                    return
                ok, keyval, mods = Gtk.accelerator_parse(accels[0])
                if ok and (keyval or mods):
                    label = Gtk.accelerator_get_label(keyval, mods)
        except Exception:
            pass
        base = _("Search connections, tools or type ssh commands")
        self.entry.set_placeholder_text(f"{base} · {label}" if label else base)

    def _examples_placeholder(self) -> str:
        """Example queries shown while the box is open: a connection name, an
        ssh command and a tool, built from the user's own hosts when possible."""
        try:
            connections = list(self.window.connection_manager.get_connections())
        except Exception:
            connections = []
        ordered = _recent_and_pinned(self.window, connections) or connections
        if ordered:
            first = ordered[0]
            nick = str(getattr(first, "nickname", "") or "")
            host = str(
                getattr(first, "hostname", "") or getattr(first, "host", "") or ""
            )
            user = str(getattr(first, "username", "") or "")
            target = f"{user}@{host}" if user and host else host or nick
            second = ordered[1] if len(ordered) > 1 else first
            nick2 = str(getattr(second, "nickname", "") or "") or nick
            if nick and target:
                return _("Try: “{name}”  or  “ssh {target}”  or  “sftp {name2}”").format(
                    name=nick, target=target, name2=nick2,
                )
        return _("Try: “ssh root@203.0.113.7”  or  “sftp”  or  “preferences”")

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

    # -- transient Start attention -----------------------------------------

    @property
    def attention_active(self) -> bool:
        """True while the border tracer is animating (read-only observation)."""
        return self._attention_active

    def request_attention(self) -> None:
        """Brief, focus-safe attention tracer on the docked omni-search box.

        Called from the canonical Start-transition path when the Start tab
        becomes current. A short accent-colored segment travels clockwise once
        around the rounded border (~700 ms). No-op unless Start is genuinely
        visible and Omnisearch is neither open nor focused, so it never
        competes with the real ``:focus-within`` ring. Repeats restart the
        tracer instead of stacking (each call replaces any pending animation),
        which keeps duplicate activation requests deterministic and
        single-visual. Never touches keyboard focus.

        When the window has not been presented yet (initial Start
        presentation), the tracer is deferred until the search box is mapped,
        so it is actually visible instead of finishing before the first paint.
        """
        if self.popup.visible:
            return
        if not self._start_is_visible():
            return
        try:
            if self._entry_focus_widget().has_focus():
                return
        except Exception:
            pass
        self._cancel_attention()
        if not self._attention_owner_is_mapped():
            # The window has not been presented yet (initial Start
            # presentation happens mid-construction); an animation started now
            # would run against a 0x0/unmapped widget. Defer until the widget
            # is mapped, then let the settle delay pace the tracer's start.
            self._defer_attention_until_map()
            return
        self._start_attention()

    def _attention_owner_is_mapped(self) -> bool:
        try:
            return bool(self.content.get_mapped())
        except Exception:
            return True

    def _defer_attention_until_map(self) -> None:
        # One-shot: replace any earlier pending map hook so repeated requests
        # keep the deterministic single-tracer semantics.
        handler_id = self._attention_map_handler_id
        if handler_id is not None:
            try:
                self.content.disconnect(handler_id)
            except Exception:
                pass
            self._attention_map_handler_id = None
        try:
            self._attention_map_handler_id = self.content.connect(
                "map", self._on_attention_map
            )
        except Exception:
            pass

    def _on_attention_map(self, *_args) -> None:
        # One-shot: the content is (presumably) on screen now; disconnect so a
        # later unmap/map cycle cannot stack another tracer. The tracer itself
        # waits a short settle beat so it starts once the window is usable.
        handler_id = self._attention_map_handler_id
        self._attention_map_handler_id = None
        if handler_id is not None:
            try:
                self.content.disconnect(handler_id)
            except Exception:
                pass
        try:
            self._attention_start_source_id = GLib.timeout_add(
                _ATTENTION_DEBUT_DELAY_MS, self._on_attention_start_delay
            )
        except Exception:
            self._attention_start_source_id = None

    def _on_attention_start_delay(self) -> None:
        # Re-run the full guarded request against the now-mapped widget; if
        # Start was left during the settle delay, the guards decline silently.
        self._attention_start_source_id = None
        self.request_attention()
        return GLib.SOURCE_REMOVE

    def _animations_disabled(self) -> bool:
        """True when the theme asks for reduced motion (GTK setting)."""
        try:
            settings = Gtk.Settings.get_default()
            return not bool(settings.get_property("gtk-enable-animations"))
        except Exception:
            return False

    def _accent_color(self) -> Optional[Gdk.RGBA]:
        """Resolve the current theme accent color for the tracer stroke.

        Uses ``Gtk.StyleContext.lookup_color`` against the search widget's own
        context so light/dark mode and custom accent colours apply. The API is
        deprecated in newer GTK but remains the only portable way to resolve
        ``@name``-style palette colours across the supported 4.6-4.2x range;
        PyGObject returns ``(ok, Gdk.RGBA)`` on recent versions and fills an
        out-param on older ones. Returns ``None`` on any failure so the tracer
        degrades to nothing instead of crashing Omnisearch.
        """
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                context = self._attention_area.get_style_context()
                try:
                    result = context.lookup_color("accent_bg_color")
                except TypeError:  # older PyGObject: out-param form
                    rgba = Gdk.RGBA()
                    if context.lookup_color("accent_bg_color", rgba):
                        return rgba
                    return None
            if isinstance(result, tuple) and len(result) == 2:
                ok, rgba = result
                return rgba if ok else None
        except Exception:
            pass
        return None

    def _start_attention(self) -> None:
        if self._animations_disabled():
            return
        try:
            self._attention_color = self._accent_color()
        except Exception:
            self._attention_color = None
        if self._attention_color is None:
            return
        self._attention_active = True
        self._attention_progress = 0.0
        self._attention_start_us = None  # stamped by the first tick
        try:
            self._attention_tick_id = self._attention_area.add_tick_callback(
                self._on_attention_tick
            )
        except Exception:
            self._attention_tick_id = None
            self._attention_active = False
            return
        self._attention_area.queue_draw()

    def _on_attention_tick(self, _widget, frame_clock) -> bool:
        # Leaving Start or opening Omnisearch mid-lap stops the tracer
        # immediately rather than letting it consume frames for the rest of
        # the lap.
        if self.popup.visible or not self._start_is_visible():
            self._attention_active = False
            self._attention_tick_id = None
            self._attention_area.queue_draw()
            return False
        now = frame_clock.get_frame_time()
        if now < 0:
            self._attention_area.queue_draw()
            return True
        if self._attention_start_us is None:
            self._attention_start_us = now
            self._attention_area.queue_draw()
            return True
        self._attention_progress = _tracer_progress(
            now - self._attention_start_us, _ATTENTION_MS, _ATTENTION_LAPS
        )
        if self._attention_progress >= 1.0:
            self._attention_active = False
            self._attention_tick_id = None
            self._attention_area.queue_draw()
            return False
        self._attention_area.queue_draw()
        return True

    def _on_attention_draw(
        self, _area, cr, width: float, height: float, _user_data
    ) -> None:
        # Decorative layer: any failure must never take Omnisearch down.
        if not self._attention_active or self._attention_color is None:
            return
        try:
            _draw_attention_tracer(
                cr, width, height, self._attention_progress, self._attention_color
            )
        except Exception:
            pass

    def _cancel_attention(self) -> None:
        """Retire any pending attention tracer idempotently: the animation
        (tick callback + drawing state), the startup delay source and any
        pending map hook. Calling it twice is harmless."""
        tick_id = self._attention_tick_id
        self._attention_tick_id = None
        if tick_id is not None:
            try:
                self._attention_area.remove_tick_callback(tick_id)
            except Exception:
                pass
        self._attention_active = False
        self._attention_progress = 0.0
        self._attention_start_us = None
        start_source_id = self._attention_start_source_id
        self._attention_start_source_id = None
        if start_source_id is not None:
            try:
                GLib.source_remove(start_source_id)
            except Exception:
                pass
        handler_id = self._attention_map_handler_id
        self._attention_map_handler_id = None
        if handler_id is not None:
            try:
                self.content.disconnect(handler_id)
            except Exception:
                pass
        try:
            self._attention_area.queue_draw()
        except Exception:
            pass

    def _on_attention_owner_destroyed(self, *_args) -> None:
        # The content can be destroyed while a tracer is pending (window
        # teardown); retire everything so no callback fires into a dead widget.
        self._cancel_attention()

    def show(self, select_all: bool = True) -> None:
        # Opening Omnisearch is its own strong visual state (the focused
        # ring), so retire any pending attention tracer rather than letting it
        # linger under the popup.
        self._cancel_attention()
        # While open, the placeholder shows example queries instead of the
        # shortcut hint; _on_popup_hidden switches it back.
        self.entry.set_placeholder_text(self._examples_placeholder())
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
        self._rebuild()

    def _on_popup_hidden(self) -> None:
        self._set_results_visible(False)
        # Back to the rest-state hint (also picks up shortcut reassignments).
        self._update_placeholder()
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
        query = self.entry.get_text().strip()
        self._results = search_omni(self.window, query) if query else []
        self._clear_rows()
        for result in self._results:
            self.results.append(self._make_row(result))
        first = self._first_enabled_row()
        if first is not None:
            self.results.select_row(first)
        self._set_results_visible(self.popup.visible and bool(self._results))

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
