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
    """The port a ``[user@]host[:port]`` hop names; empty when none."""
    hop = hop.split("://", 1)[-1].rpartition("@")[2]
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
) -> List[Stop]:
    """The stops after this computer, from the first jump host to the
    destination. *inherited*: the route rows (``proxy_jump_row``...) whose
    value is OpenSSH's rather than the block's. *saved*: hop host ->
    ``(hostname, port)`` of a saved connection of that name."""
    saved = saved or {}
    stops: List[Stop] = []
    hops = fields.jump_hops(jump)
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
        self.route_strip = RouteStrip()
        inner.append(self.route_strip)
        card.append(inner)
        group.add(card)
        self._route_update_pending = 0
        for name in _ROUTE_ROWS:
            row = getattr(self, name, None)
            if row is not None:
                row.connect("changed", lambda *_a: self._schedule_route_update())
        self._update_route()
        return group

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
                return False
            inherited = set(getattr(self, "_inherited_row_handlers", None) or ())
            strip.show_route(route_stops(
                host=host,
                hostname=hostname,
                port=self._row_text("port_row"),
                jump=self._row_text("proxy_jump_row"),
                command=self._row_text("proxy_command_row"),
                inherited=inherited,
                saved=self._saved_hops(),
                source=inherited_note_text(),
            ))
        except Exception:
            logger.debug("Could not draw the route", exc_info=True)
        return False
