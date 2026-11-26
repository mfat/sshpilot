"""The connection editor's Routing page: the route the connection takes,
drawn as Advanced Configuration draws it (:mod:`sshpilot.route_strip`).

The picture reads the page's own rows (jump hosts, proxy command) and the
Connection page's host, so it follows every keystroke. A row the Host
block does not set holds the value OpenSSH resolves for it, dimmed (see
``_load_inherited_values_async``); such a stop is dimmed too. A jump host
is a saved connection when one has its name: its address is shown. The
picture is not a way into Advanced Configuration: the dialog is modal and
the two windows are not linked.
"""
from __future__ import annotations

import logging
from gettext import gettext as _
from typing import List, Optional

from . import advanced_config_fields as fields
from .connection_dialog_overrides import inherited_note_text

logger = logging.getLogger(__name__)

try:
    from gi.repository import Adw, GLib, Gtk

    from .route_strip import COMMAND, JUMP, TARGET, RouteStrip, Stop
except Exception:  # pragma: no cover - GTK-less import (headless tests)
    Adw = GLib = Gtk = None
    RouteStrip = None
    from dataclasses import dataclass

    JUMP, COMMAND, TARGET = "jump", "command", "target"

    @dataclass(frozen=True)
    class Stop:  # type: ignore[no-redef]
        name: str
        endpoint: str = ""
        kind: str = JUMP
        inherited: bool = False
        source: str = ""


#: Rows the route is drawn from.
_ROUTE_ROWS = (
    "nickname_row", "hostname_row", "port_row", "proxy_jump_row", "proxy_command_row",
)


def _endpoint(host: str, port: str) -> str:
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"                  # an IPv6 address
    return f"{host}:{port or '22'}"


def _hop_port(hop: str) -> str:
    """The port a ``[user@]host[:port]`` or ``ssh://[user@]host[:port][/]``
    hop names; empty when none."""
    if "://" in hop:
        hop = hop.split("://", 1)[1].rstrip("/")
    hop = hop.rpartition("@")[2]
    if hop.startswith("["):
        return hop.partition("]")[2].lstrip(":")
    return hop.partition(":")[2]


def route_stops(
    *,
    host: str,
    hostname: str,
    port: str,
    jump: str,
    command: str,
    inherited=frozenset(),
    saved=None,
    source: str = "",
    command_first: bool = False,
) -> List[Stop]:
    """The stops after this computer, from the first jump host to the
    destination. *inherited*: the route rows (``proxy_jump_row``...) whose
    value is OpenSSH's rather than the block's. *saved*: hop host ->
    ``(hostname, port)`` of a saved connection of that name.
    *command_first*: OpenSSH obtains the proxy command before the jump
    hosts (see :func:`unused_setting`)."""
    saved = saved or {}
    stops: List[Stop] = []
    hops = fields.jump_hops(jump)
    if unused_setting(jump, command, command_first) == "jump":
        hops = []
    if hops:
        from_block = "proxy_jump_row" in inherited
        for hop in hops:
            name = fields.hop_host(hop)
            known = saved.get(name)
            explicit = _hop_port(hop)
            # The address of a saved connection of that name; a hop the
            # app does not know says no more than its name already does.
            where = (
                _endpoint(known[0] or name, explicit or str(known[1] or ""))
                if known is not None else ""
            )
            stops.append(Stop(hop, where, JUMP, from_block, source if from_block else ""))
    elif command.strip() and command.strip().lower() != "none":
        from_block = "proxy_command_row" in inherited
        stops.append(Stop(
            _("Proxy command"), command.strip(), COMMAND,
            from_block, source if from_block else "",
        ))
    stops.append(Stop(host or hostname, _endpoint(hostname or host, port), TARGET))
    return stops


def _set(value: str) -> bool:
    return bool(value.strip()) and value.strip().lower() != "none"


def unused_setting(jump: str, command: str, command_first: bool) -> Optional[str]:
    """Which of the two settings OpenSSH ignores when both are set:
    ``"jump"``, ``"command"``, or None. They compete for one slot and the
    first OpenSSH obtains wins; a new block writes the jump hosts first."""
    if not (fields.jump_hops(jump) and _set(command)):
        return None
    return "jump" if command_first else "command"


def command_wins(resolved) -> bool:
    """Whether OpenSSH's answer (``ssh -G``, keyword -> value) uses a proxy
    command rather than jump hosts: it reports only the one in force."""
    resolved = resolved or {}
    return _set(str(resolved.get("proxycommand") or "")) and not _set(
        str(resolved.get("proxyjump") or "")
    )


def reorder(hops: List[str], index: int, position: int) -> Optional[List[str]]:
    """*hops* with the one at *index* moved to *position* (counted before
    it moves); None when that changes nothing or is out of range."""
    if not 0 <= index < len(hops) or not 0 <= position <= len(hops):
        return None
    if position in (index, index + 1):
        return None
    moved = list(hops)
    hop = moved.pop(index)
    moved.insert(position - 1 if position > index else position, hop)
    return moved


class ConnectionDialogRouteMixin:
    """The Routing page's picture of the route."""

    def _build_route_group(self):
        group = Adw.PreferencesGroup()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_top(12)
        inner.set_margin_bottom(14)
        inner.set_margin_start(14)
        inner.set_margin_end(14)
        caption = Gtk.Label(label=_("Route"), xalign=0)
        caption.add_css_class("caption-heading")
        caption.add_css_class("dim-label")
        inner.append(caption)
        self.route_strip = RouteStrip(
            on_reorder=self._reorder_jump_hosts, on_remove=self._remove_jump_host,
        )
        inner.append(self.route_strip)
        # When both settings are set, the one OpenSSH ignores.
        self.route_note = Gtk.Label(xalign=0, wrap=True)
        self.route_note.add_css_class("caption")
        self.route_note.add_css_class("warning")
        self.route_note.set_visible(False)
        inner.append(self.route_note)
        card.append(inner)
        group.add(card)
        self._route_update_pending = 0
        for name in _ROUTE_ROWS:
            row = getattr(self, name, None)
            if row is not None:
                row.connect("changed", lambda *_a: self._schedule_route_update())
        self._update_route()
        return group

    def _show_route_note(self, unused: Optional[str]) -> None:
        note = getattr(self, "route_note", None)
        if note is None:
            return
        if unused == "jump":
            text = _("The jump hosts have no effect: the proxy command comes first, and OpenSSH uses only one of the two.")
        elif unused == "command":
            text = _("The proxy command has no effect: the jump hosts come first, and OpenSSH uses only one of the two.")
        else:
            text = ""
        note.set_label(text)
        note.set_visible(bool(text))

    def _reorder_jump_hosts(self, index: int, position: int) -> None:
        """A jump host card dropped elsewhere: the Jump hosts row is
        written in the new order (an inherited chain becomes this host's
        own, as any edit of the row makes it)."""
        row = getattr(self, "proxy_jump_row", None)
        if row is None:
            return
        hops = reorder(fields.jump_hops(row.get_text()), index, position)
        if hops is not None:
            self._write_jump_hosts(hops)

    def _remove_jump_host(self, index: int) -> None:
        """A jump host card's remove button: the hop leaves the Jump hosts
        row. The last hop of a chain this host inherits leaves ``none``:
        an empty row would inherit the chain again."""
        row = getattr(self, "proxy_jump_row", None)
        if row is None:
            return
        inherited = "proxy_jump_row" in (getattr(self, "_inherited_row_handlers", None) or {})
        hops = fields.jump_hops(row.get_text())
        if 0 <= index < len(hops):
            del hops[index]
            self._write_jump_hosts(hops or (["none"] if inherited else []))

    def _write_jump_hosts(self, hops: List[str]) -> None:
        row = self.proxy_jump_row
        text = ",".join(hops)
        setter = getattr(self, "_set_text_without_completion", None)
        if callable(setter):
            setter(row, text)
        else:
            row.set_text(text)

    def _schedule_route_update(self) -> None:
        # Several rows change at once when a connection is loaded or an
        # inherited value arrives: draw once, when they are done.
        if GLib is None or getattr(self, "_route_update_pending", 0):
            return
        self._route_update_pending = GLib.idle_add(self._update_route)

    def _row_text(self, name: str) -> str:
        row = getattr(self, name, None)
        try:
            return row.get_text().strip() if row is not None else ""
        except Exception:
            return ""

    def _saved_hops(self) -> dict:
        manager = getattr(self, "connection_manager", None)
        found = {}
        for connection in getattr(manager, "connections", None) or ():
            nickname = str(getattr(connection, "nickname", "") or "")
            if nickname and nickname not in found:
                found[nickname] = (
                    str(getattr(connection, "hostname", "") or "")
                    or str(getattr(connection, "host", "") or ""),
                    getattr(connection, "port", "") or "",
                )
        return found

    def _update_route(self) -> bool:
        self._route_update_pending = 0
        strip = getattr(self, "route_strip", None)
        if strip is None:
            return False
        try:
            typed = getattr(self, "_typed_primary_alias", None)
            host = typed() if callable(typed) else self._row_text("nickname_row")
            hostname = self._row_text("hostname_row")
            if not host and not hostname:
                strip.show_route((), _("Type a host name to see its route."))
                self._show_route_note(None)
                return False
            inherited = set(getattr(self, "_inherited_row_handlers", None) or ())
            jump = self._row_text("proxy_jump_row")
            command = self._row_text("proxy_command_row")
            # Which one OpenSSH used when the dialog asked it: the order of
            # the two lines is kept when the block is saved.
            first = command_wins(getattr(self, "_effective_resolved", None))
            strip.show_route(route_stops(
                host=host,
                hostname=hostname,
                port=self._row_text("port_row"),
                jump=jump,
                command=command,
                inherited=inherited,
                saved=self._saved_hops(),
                source=inherited_note_text(),
                command_first=first,
            ))
            self._show_route_note(unused_setting(jump, command, first))
        except Exception:
            logger.debug("Could not draw the route", exc_info=True)
        return False
