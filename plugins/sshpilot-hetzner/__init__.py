"""Hetzner Cloud — browse your servers and add them as SSH connections.

A non-protocol sshPilot plugin. Paste a Hetzner Cloud API token, list your
servers (name, status, public IPv4, labels), and add any of them — or a filtered
batch — as SSH connections.

This is a **browser of existing servers**, not a provisioner: it lists what's
already in your project and creates connections pointing at each server's public
IP. It does not create/destroy servers and does not store server passwords
(Hetzner's API doesn't expose them); auth to the host is whatever you've set up
(an SSH key, usually).

Capabilities exercised (all from ``sshpilot.plugins.api``):
* the Hetzner Cloud REST API over HTTPS (network, stdlib ``urllib`` only)
* the API token stored in the OS keyring (``ctx.secrets``)
* a UI page (``ctx.ui.register_page``) + toasts; background fetch
  (``ctx.run_on_ui_thread``)
* creating connections + de-dup (``ctx.add_connection`` / ``ctx.list_connections``
  — needs app API >= 1.4) and optional grouping (``ctx.add_connection_group``)

Pure logic (response parsing / dedup) has no GTK import and is unit-tested
without the network; ``gi`` is imported lazily inside the page factory.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from sshpilot.plugins.api import PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)

API_BASE = "https://api.hetzner.cloud/v1"
HTTP_TIMEOUT = 30
USER_AGENT = "sshpilot-hetzner-plugin/1.0"


class HetznerError(RuntimeError):
    pass


# --- HTTP (stdlib only) -----------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(path: str, token: str, *, timeout: int = HTTP_TIMEOUT) -> Dict[str, Any]:
    url = API_BASE + path
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token or ''}")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if exc.code in (401, 403):
            raise HetznerError("Authentication failed — check your API token.") from exc
        raise HetznerError(f"GET {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise HetznerError(f"GET {path} failed: {exc.reason}") from exc
    try:
        return json.loads(raw) if raw else {}
    except ValueError as exc:
        raise HetznerError("Could not parse Hetzner API response.") from exc


def fetch_servers(token: str) -> List[Dict[str, Any]]:
    """Fetch all servers, following pagination."""
    servers: List[Dict[str, Any]] = []
    page = 1
    while True:
        payload = _get(f"/servers?page={page}&per_page=50", token)
        batch = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(batch, list):
            break
        servers.extend(batch)
        pagination = ((payload.get("meta") or {}).get("pagination") or {}) \
            if isinstance(payload, dict) else {}
        nxt = pagination.get("next_page")
        if not nxt:
            break
        page = nxt
    return servers


# --- pure logic (no GTK) ----------------------------------------------------

def servers_from_response(servers: Any) -> List[Dict[str, Any]]:
    """Normalize raw Hetzner server objects into rows
    ``{name, status, ip, labels, has_ip}``. Servers without a public IPv4 are
    kept but flagged (has_ip=False) so the UI can disable Add for them."""
    rows: List[Dict[str, Any]] = []
    if not isinstance(servers, list):
        return rows
    for srv in servers:
        if not isinstance(srv, dict):
            continue
        name = (srv.get("name") or "").strip()
        public = (srv.get("public_net") or {}).get("ipv4") or {}
        ip = (public.get("ip") or "").strip()
        labels = srv.get("labels") if isinstance(srv.get("labels"), dict) else {}
        rows.append({
            "name": name or (str(srv.get("id")) if srv.get("id") else ""),
            "status": srv.get("status") or "unknown",
            "ip": ip,
            "labels": labels,
            "has_ip": bool(ip),
        })
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def server_connection_data(row: Dict[str, Any], default_user: str = "root") -> Dict[str, Any]:
    user = (default_user or "root").strip() or "root"
    return {
        "protocol": "ssh",
        "nickname": row.get("name") or row.get("ip"),
        "host": row.get("ip"),
        "hostname": row.get("ip"),
        "username": user,
        "port": 22,
    }


def dedup_new(rows: List[Dict[str, Any]], existing: Any) -> List[Dict[str, Any]]:
    nicks, hosts = set(), set()
    for conn in existing or []:
        nicks.add((getattr(conn, "nickname", "") or "").lower())
        hosts.add((getattr(conn, "host", "") or "").lower())
    out = []
    for row in rows:
        if not row.get("has_ip"):
            continue
        if (row["name"].lower() in nicks) or (row["ip"].lower() in hosts):
            continue
        out.append(row)
    return out


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._default_user = ctx.settings.get("default_user", "root")
        self._group_name = ctx.settings.get("group_name", "")
        self._stop = threading.Event()
        self._rows: List[Dict[str, Any]] = []
        self._error = ""
        self._stack = None
        self._token_entry = None
        self._user_entry = None
        self._group_entry = None
        self._list_box = None
        self._status_label = None
        self._gate_status = None

        ctx.ui.register_page(
            "hetzner", "Hetzner", "network-server-symbolic", self._build_page)

    def deactivate(self) -> None:
        self._stop.set()
        logger.info("hetzner: deactivate")

    def _token(self) -> str:
        return self.ctx.secrets.get("api_token") or ""

    # --- UI (gi imported lazily) ------------------------------------------
    def _build_page(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        self._Gtk = Gtk
        self._Adw = Adw

        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_gate(), "gate")
        self._stack.add_named(self._build_dashboard(), "dashboard")
        self._stack.set_visible_child_name("dashboard" if self._token() else "gate")
        if self._token():
            self._refresh()
        return self._stack

    def _build_gate(self):
        Adw, Gtk = self._Adw, self._Gtk
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for fn in (box.set_margin_top, box.set_margin_bottom,
                   box.set_margin_start, box.set_margin_end):
            fn(18)
        title = Gtk.Label(label="Connect Hetzner Cloud")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)
        hint = Gtk.Label(
            label="Paste a Hetzner Cloud API token (Project ▸ Security ▸ API "
                  "tokens, read access is enough). It's stored in your keyring.")
        hint.add_css_class("dim-label")
        hint.set_wrap(True)
        hint.set_xalign(0)
        box.append(hint)
        group = Adw.PreferencesGroup()
        self._token_entry = Adw.PasswordEntryRow(title="API token")
        group.add(self._token_entry)
        box.append(group)
        self._save_btn = Gtk.Button(label="Save token & connect")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.set_halign(Gtk.Align.START)
        self._save_btn.connect("clicked", self._on_save_token)
        box.append(self._save_btn)
        self._gate_status = Gtk.Label(label="")
        self._gate_status.add_css_class("dim-label")
        self._gate_status.set_halign(Gtk.Align.START)
        self._gate_status.set_wrap(True)
        self._gate_status.set_xalign(0)
        box.append(self._gate_status)
        return box

    def _build_dashboard(self):
        Adw, Gtk = self._Adw, self._Gtk
        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for fn in (box.set_margin_top, box.set_margin_bottom,
                   box.set_margin_start, box.set_margin_end):
            fn(18)
        outer.set_child(box)

        title = Gtk.Label(label="Hetzner Servers")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        opts = Adw.PreferencesGroup()
        self._user_entry = Adw.EntryRow(title="Default SSH username")
        self._user_entry.set_text(self._default_user)
        self._user_entry.connect("changed", self._on_user_changed)
        opts.add(self._user_entry)
        self._group_entry = Adw.EntryRow(title="Add to group (optional)")
        self._group_entry.set_text(self._group_name)
        self._group_entry.connect("changed", self._on_group_changed)
        opts.add(self._group_entry)
        box.append(opts)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _b: self._refresh())
        actions.append(refresh)
        bulk = Gtk.Button(label="Add all new")
        bulk.add_css_class("suggested-action")
        bulk.connect("clicked", self._on_bulk_add)
        actions.append(bulk)
        forget = Gtk.Button(label="Sign out")
        forget.connect("clicked", self._on_sign_out)
        actions.append(forget)
        box.append(actions)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        box.append(self._list_box)

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("dim-label")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)
        return outer

    # --- token / prefs ----------------------------------------------------
    def _on_save_token(self, _btn) -> None:
        token = self._token_entry.get_text().strip()
        if not token:
            return
        # Validate against the API before committing the token, so a bad token
        # is reported here instead of silently failing on the dashboard.
        self._save_btn.set_sensitive(False)
        self._set_gate_status("Validating token…")

        def worker():
            try:
                rows = servers_from_response(fetch_servers(token))
                error = ""
            except Exception as exc:
                rows, error = [], str(exc)
            if not self._stop.is_set():
                self.ctx.run_on_ui_thread(self._on_token_validated, token, rows, error)
        threading.Thread(target=worker, daemon=True).start()

    def _on_token_validated(self, token, rows, error) -> None:
        self._save_btn.set_sensitive(True)
        if error:
            self._set_gate_status(f"Token rejected: {error}")
            return
        try:
            self.ctx.secrets.set("api_token", token)
        except Exception as exc:
            self._set_gate_status(f"Could not store token: {exc}")
            return
        self._set_gate_status("")
        self._stack.set_visible_child_name("dashboard")
        self._on_fetched(rows, "")

    def _on_sign_out(self, _btn) -> None:
        try:
            self.ctx.secrets.delete("api_token")
        except Exception:
            pass
        self._rows = []
        self._stack.set_visible_child_name("gate")

    def _on_user_changed(self, entry) -> None:
        self._default_user = entry.get_text().strip() or "root"
        self.ctx.settings.set("default_user", self._default_user)

    def _on_group_changed(self, entry) -> None:
        self._group_name = entry.get_text().strip()
        self.ctx.settings.set("group_name", self._group_name)

    # --- fetch ------------------------------------------------------------
    def _refresh(self) -> None:
        token = self._token()
        if not token:
            self._stack.set_visible_child_name("gate")
            return
        self._set_status("Fetching servers…")

        def worker():
            try:
                rows = servers_from_response(fetch_servers(token))
                error = ""
            except Exception as exc:
                rows, error = [], str(exc)
            if not self._stop.is_set():
                self.ctx.run_on_ui_thread(self._on_fetched, rows, error)
        threading.Thread(target=worker, daemon=True).start()

    def _set_gate_status(self, text: str) -> None:
        if self._gate_status is not None:
            self._gate_status.set_text(text)

    def _on_fetched(self, rows: List[Dict[str, Any]], error: str) -> None:
        self._error = error
        self._rows = [] if error else rows
        self._repopulate()

    def _existing(self) -> Any:
        if hasattr(self.ctx, "list_connections"):
            return self.ctx.list_connections()
        return []

    def _repopulate(self) -> None:
        Gtk = self._Gtk
        while child := self._list_box.get_first_child():
            self._list_box.remove(child)

        new_rows = dedup_new(self._rows, self._existing())
        new_names = {r["name"] for r in new_rows}
        for row in self._rows:
            self._list_box.append(self._server_row(row, row["name"] in new_names))

        if self._error:
            self._set_status(self._error)
        elif not self._rows:
            self._set_status("No servers in this project.")
        else:
            self._set_status(f"{len(self._rows)} server(s), {len(new_rows)} new.")

    def _server_row(self, row: Dict[str, Any], is_new: bool):
        Gtk = self._Gtk
        lb_row = Gtk.ListBoxRow()
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for fn in (line.set_margin_top, line.set_margin_bottom,
                   line.set_margin_start, line.set_margin_end):
            fn(8)
        dot = "🟢" if row["status"] == "running" else "⚪"
        name = Gtk.Label(label=f"{dot} {row['name']}", xalign=0)
        name.set_hexpand(True)
        line.append(name)
        addr = Gtk.Label(label=row.get("ip") or "(no public IP)", xalign=0)
        addr.add_css_class("dim-label")
        line.append(addr)
        btn = Gtk.Button()
        if not row.get("has_ip"):
            btn.set_label("No IP")
            btn.set_sensitive(False)
        else:
            btn.set_label("Add" if is_new else "Added")
            btn.set_sensitive(is_new)
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", self._on_add_one, row)
        line.append(btn)
        lb_row.set_child(line)
        return lb_row

    # --- import -----------------------------------------------------------
    def _on_add_one(self, _btn, row: Dict[str, Any]) -> None:
        data = server_connection_data(row, self._default_user)
        try:
            self.ctx.add_connection(data)
        except ValueError as exc:
            self._set_status(f"{row['name']}: {exc}")
            return
        if self._group_name:
            gid = self.ctx.create_group(self._group_name)
            if gid:
                self.ctx.add_connection_to_group(data["nickname"], gid)
        self.ctx.ui.notify(f"Added {row['name']}")
        self._repopulate()

    def _on_bulk_add(self, _btn) -> None:
        new_rows = dedup_new(self._rows, self._existing())
        if not new_rows:
            self._set_status("Nothing new to add.")
            return
        payloads = [server_connection_data(r, self._default_user) for r in new_rows]
        added = 0
        try:
            if self._group_name:
                _gid, infos = self.ctx.add_connection_group(self._group_name, payloads)
                added = len(infos)
            else:
                for data in payloads:
                    try:
                        self.ctx.add_connection(data)
                        added += 1
                    except ValueError:
                        pass
        except Exception as exc:
            self._set_status(f"Import error: {exc}")
            return
        self.ctx.ui.notify(f"Added {added} server(s)")
        self._repopulate()

    def _set_status(self, text: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(text)
