"""Tailscale Sync — turn your tailnet peers into sshPilot connections.

A non-protocol sshPilot plugin. Reads ``tailscale status --json`` and lists your
tailnet peers (name, IP, online, tags); add any peer — or a filtered batch — as
an SSH connection without copying IPs by hand.

Capabilities exercised (all from ``sshpilot.plugins.api``):
* running the ``tailscale`` CLI (process; Flatpak host-spawn aware)
* a UI page (``ctx.ui.register_page``) + toasts (``ctx.ui.notify``)
* creating connections and de-duping against existing ones
  (``ctx.add_connection`` / ``ctx.list_connections`` — needs app API >= 1.4)
* a default SSH username persisted in ``ctx.settings``

Pure logic (status parsing / dedup) has no GTK import and is unit-tested without
a display or Tailscale; ``gi`` is imported lazily inside the page factory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Dict, List, Optional

from sshpilot.plugins.api import PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)


# --- Flatpak helpers --------------------------------------------------------

def _is_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or os.path.exists("/.flatpak-info")


def tailscale_status_argv() -> Optional[List[str]]:
    """Argv for ``tailscale status --json`` (host-spawned under Flatpak when the
    binary isn't in the sandbox). None if tailscale can't be found."""
    binary = shutil.which("tailscale")
    if binary:
        return [binary, "status", "--json"]
    if _is_flatpak() and shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host", "tailscale", "status", "--json"]
    return None


# --- pure logic (no GTK) ----------------------------------------------------

def _safe_name(raw: str) -> str:
    """A short, stable nickname from a (possibly FQDN) tailscale name."""
    name = (raw or "").strip().rstrip(".")
    if not name:
        return ""
    # MagicDNS names look like host.tailnet.ts.net — keep the leading label.
    short = name.split(".")[0]
    return re.sub(r"[^A-Za-z0-9_.-]", "-", short) or short


def _unique_nickname(base: str, used: set) -> str:
    """Disambiguate colliding short names (web.prod / web.staging both -> web)
    by appending -2, -3, … so each connection gets a distinct nickname."""
    nick = base or "host"
    if nick not in used:
        used.add(nick)
        return nick
    index = 2
    while f"{nick}-{index}" in used:
        index += 1
    candidate = f"{nick}-{index}"
    used.add(candidate)
    return candidate


def parse_tailscale_status(status: Any, *, include_self: bool = False) -> List[Dict[str, Any]]:
    """Parse the dict from ``tailscale status --json`` into peer rows.

    Each row: ``{name, dns_name, ip, online, tags, os}``. Defensive — tolerates
    missing keys/old schemas and returns [] for junk input."""
    if not isinstance(status, dict):
        return []
    rows: List[Dict[str, Any]] = []
    used: set = set()

    def add(node: Any) -> None:
        if not isinstance(node, dict):
            return
        ips = node.get("TailscaleIPs") or []
        ip = ""
        for candidate in ips:
            if isinstance(candidate, str) and ":" not in candidate:  # prefer IPv4
                ip = candidate
                break
        if not ip and ips and isinstance(ips[0], str):
            ip = ips[0]
        dns_name = (node.get("DNSName") or "").rstrip(".")
        host_name = node.get("HostName") or ""
        name = _safe_name(dns_name or host_name)
        if not name and not ip:
            return
        name = _unique_nickname(name or ip, used)
        rows.append({
            "name": name,
            "dns_name": dns_name,
            "ip": ip,
            "online": bool(node.get("Online")),
            "tags": [t for t in (node.get("Tags") or []) if isinstance(t, str)],
            "os": node.get("OS") or "",
        })

    if include_self:
        add(status.get("Self"))
    peers = status.get("Peer")
    if isinstance(peers, dict):
        for node in peers.values():
            add(node)
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def matches_filter(row: Dict[str, Any], query: str) -> bool:
    """Filter by substring across name/dns/ip, or ``tag:foo`` against tags."""
    query = (query or "").strip().lower()
    if not query:
        return True
    if query.startswith("tag:"):
        want = query[4:]
        return any(want in (t.lower()) for t in row.get("tags", []))
    haystack = " ".join([
        row.get("name", ""), row.get("dns_name", ""), row.get("ip", "")]).lower()
    return query in haystack


def peer_connection_data(row: Dict[str, Any], default_user: str = "") -> Dict[str, Any]:
    """Connection payload for a peer. Prefers the MagicDNS name as host, else IP."""
    host = row.get("dns_name") or row.get("ip") or ""
    data: Dict[str, Any] = {
        "protocol": "ssh",
        "nickname": row.get("name") or host,
        "host": host,
        "hostname": host,
        "port": 22,
    }
    user = (default_user or "").strip()
    if user:
        data["username"] = user
    return data


def dedup_new(rows: List[Dict[str, Any]], existing: Any) -> List[Dict[str, Any]]:
    """Rows whose nickname or host isn't already a saved connection."""
    nicks = set()
    hosts = set()
    for conn in existing or []:
        nicks.add((getattr(conn, "nickname", "") or "").lower())
        hosts.add((getattr(conn, "host", "") or "").lower())
    out = []
    for row in rows:
        nick = (row.get("name") or "").lower()
        host = (row.get("dns_name") or row.get("ip") or "").lower()
        if nick in nicks or (host and host in hosts):
            continue
        out.append(row)
    return out


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._default_user = ctx.settings.get("default_user", "")
        self._include_self = bool(ctx.settings.get("include_self", False))
        self._stop = threading.Event()
        self._rows: List[Dict[str, Any]] = []
        self._error = ""
        self._list_box = None
        self._filter_entry = None
        self._user_entry = None
        self._self_row = None
        self._status_label = None

        ctx.ui.register_page(
            "tailscale", "Tailscale", "network-wireless-symbolic", self._build_page)

    def deactivate(self) -> None:
        self._stop.set()
        logger.info("tailscale: deactivate")

    # --- status fetch (subprocess; impure) --------------------------------
    def _fetch_status(self) -> Any:
        argv = tailscale_status_argv()
        if argv is None:
            raise RuntimeError("The 'tailscale' command was not found.")
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=15, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f"tailscale status failed: {exc}") from exc
        if result.returncode != 0:
            msg = (result.stderr or "").strip() or "non-zero exit"
            raise RuntimeError(f"tailscale status failed: {msg}")
        try:
            return json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise RuntimeError("Could not parse tailscale status JSON.") from exc

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

        title = Gtk.Label(label="Tailscale Peers")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        opts = Adw.PreferencesGroup()
        self._user_entry = Adw.EntryRow(title="Default SSH username")
        self._user_entry.set_text(self._default_user)
        self._user_entry.connect("changed", self._on_user_changed)
        opts.add(self._user_entry)
        self._filter_entry = Adw.EntryRow(title="Filter (name/ip, or tag:foo)")
        self._filter_entry.set_text(self.ctx.settings.get("filter", "") or "")
        self._filter_entry.connect("changed", self._on_filter_changed)
        opts.add(self._filter_entry)
        self._self_row = Adw.SwitchRow(
            title="Include this machine",
            subtitle="Show your own node in the peer list")
        self._self_row.set_active(self._include_self)
        self._self_row.connect("notify::active", self._on_self_toggled)
        opts.add(self._self_row)
        box.append(opts)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _b: self._refresh())
        actions.append(refresh)
        bulk = Gtk.Button(label="Add all shown (new)")
        bulk.add_css_class("suggested-action")
        bulk.connect("clicked", self._on_bulk_add)
        actions.append(bulk)
        box.append(actions)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        box.append(self._list_box)

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("dim-label")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        self._refresh()
        return outer

    def _on_user_changed(self, entry) -> None:
        self._default_user = entry.get_text().strip()
        self.ctx.settings.set("default_user", self._default_user)

    def _on_filter_changed(self, entry) -> None:
        self.ctx.settings.set("filter", entry.get_text().strip())
        self._repopulate()

    def _on_self_toggled(self, row, _param) -> None:
        self._include_self = row.get_active()
        self.ctx.settings.set("include_self", self._include_self)
        self._refresh()

    def _refresh(self) -> None:
        self._set_status("Querying tailscale…")
        include_self = self._include_self

        def worker():
            try:
                status = self._fetch_status()
                rows = parse_tailscale_status(status, include_self=include_self)
                error = ""
            except Exception as exc:  # surfaced to the user
                rows, error = [], str(exc)
            if not self._stop.is_set():
                self.ctx.run_on_ui_thread(self._on_fetched, rows, error)
        threading.Thread(target=worker, daemon=True).start()

    def _on_fetched(self, rows: List[Dict[str, Any]], error: str) -> None:
        self._error = error
        self._rows = [] if error else rows
        self._repopulate()

    def _shown_rows(self) -> List[Dict[str, Any]]:
        query = self._filter_entry.get_text() if self._filter_entry else ""
        return [r for r in self._rows if matches_filter(r, query)]

    def _existing(self) -> Any:
        if hasattr(self.ctx, "list_connections"):
            return self.ctx.list_connections()
        return []

    def _repopulate(self) -> None:
        Gtk = self._Gtk
        while child := self._list_box.get_first_child():
            self._list_box.remove(child)

        shown = self._shown_rows()
        existing = self._existing()
        new_rows = dedup_new(shown, existing)
        new_keys = {r["name"] for r in new_rows}

        if not shown:
            row = Gtk.ListBoxRow()
            row.set_child(Gtk.Label(label="No peers match.",
                                    margin_top=8, margin_bottom=8))
            self._list_box.append(row)
        for peer in shown:
            self._list_box.append(self._peer_row(peer, peer["name"] in new_keys))

        if self._error:
            self._set_status(self._error)
        else:
            self._set_status(f"{len(shown)} peer(s) shown, {len(new_rows)} new.")

    def _peer_row(self, peer: Dict[str, Any], is_new: bool):
        Gtk = self._Gtk
        row = Gtk.ListBoxRow()
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for fn in (line.set_margin_top, line.set_margin_bottom,
                   line.set_margin_start, line.set_margin_end):
            fn(8)
        dot = "🟢" if peer["online"] else "⚪"
        name = Gtk.Label(label=f"{dot} {peer['name']}", xalign=0)
        name.set_hexpand(True)
        line.append(name)
        addr = Gtk.Label(label=peer.get("ip") or peer.get("dns_name") or "", xalign=0)
        addr.add_css_class("dim-label")
        line.append(addr)
        if peer.get("tags"):
            tags = Gtk.Label(label=",".join(peer["tags"]), xalign=0)
            tags.add_css_class("dim-label")
            tags.add_css_class("caption")
            line.append(tags)
        btn = Gtk.Button(label="Add" if is_new else "Added")
        btn.set_sensitive(is_new)
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", self._on_add_one, peer)
        line.append(btn)
        row.set_child(line)
        return row

    def _on_add_one(self, _btn, peer: Dict[str, Any]) -> None:
        data = peer_connection_data(peer, self._default_user)
        try:
            self.ctx.add_connection(data)
        except ValueError as exc:
            self._set_status(f"{peer['name']}: {exc}")
            return
        self.ctx.ui.notify(f"Added {peer['name']}")
        self._repopulate()

    def _on_bulk_add(self, _btn) -> None:
        new_rows = dedup_new(self._shown_rows(), self._existing())
        added = 0
        for peer in new_rows:
            try:
                self.ctx.add_connection(peer_connection_data(peer, self._default_user))
                added += 1
            except ValueError:
                pass
        self._set_status(f"Added {added} connection(s).")
        self.ctx.ui.notify(f"Added {added} Tailscale peer(s)")
        self._repopulate()

    def _set_status(self, text: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(text)
