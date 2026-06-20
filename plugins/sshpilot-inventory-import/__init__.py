"""Inventory Import — bulk-import hosts from Ansible, CSV, or plain host lists.

A non-protocol sshPilot plugin. Pick an inventory file, preview which hosts are
new or already saved, then import selected rows as SSH connections (optionally
into a sidebar group).

Capabilities exercised (all from ``sshpilot.plugins.api``):
* enumerating saved hosts (``ctx.list_connections`` — needs app API >= 1.4;
  without it every host previews as new)
* creating/updating connections and groups (``ctx.add_connection`` /
  ``add_connection_group`` / ``update_connection`` / ``add_connection_to_group``)
* per-plugin persisted settings (``ctx.settings``)
* a UI page (``ctx.ui.register_page``) and toasts (``ctx.ui.notify``)
* background file parsing (``ctx.run_on_ui_thread``)

Pure parsing/diff logic lives at module top with no GTK import, so it's
unit-testable without a display; ``gi`` is imported lazily inside the page
factory, which only runs inside the running app.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sshpilot.plugins.api import PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)

DEFAULT_PORT = 22
FORMAT_PLAIN = "plain"
FORMAT_CSV = "csv"
FORMAT_ANSIBLE_INI = "ansible-ini"
FORMAT_ANSIBLE_YAML = "ansible-yaml"

FORMAT_CHOICES = (
    (FORMAT_PLAIN, "Plain hosts (one per line)"),
    (FORMAT_CSV, "CSV (nickname, host, user, port)"),
    (FORMAT_ANSIBLE_INI, "Ansible INI inventory"),
    (FORMAT_ANSIBLE_YAML, "Ansible YAML inventory (simple)"),
)

STATUS_NEW = "new"
STATUS_CHANGED = "changed"
STATUS_EXISTS = "exists"


@dataclass
class HostRow:
    """One host to import."""

    nickname: str
    host: str
    username: str = ""
    port: int = DEFAULT_PORT
    group: str = ""

    def connection_data(self, default_user: str = "") -> Dict[str, Any]:
        user = (self.username or default_user or "").strip()
        data: Dict[str, Any] = {
            "protocol": "ssh",
            "nickname": self.nickname,
            "host": self.host,
            "hostname": self.host,
            "port": int(self.port or DEFAULT_PORT),
        }
        if user:
            data["username"] = user
        return data


@dataclass
class DiffRow:
    """A host row plus its import status vs existing connections."""

    row: HostRow
    status: str = STATUS_NEW
    existing_nickname: str = ""


# --- pure parsing (no GTK) --------------------------------------------------

def _clean_host(value: str) -> str:
    return (value or "").strip()


def _parse_port(raw: Any, default: int = DEFAULT_PORT) -> int:
    if raw is None or raw == "":
        return default
    try:
        port = int(raw)
        if 0 < port < 65536:
            return port
    except (TypeError, ValueError):
        pass
    return default


def _unique_nickname(base: str, used: set) -> str:
    nick = base.strip() or "host"
    if nick not in used:
        used.add(nick)
        return nick
    index = 2
    while f"{nick}-{index}" in used:
        index += 1
    candidate = f"{nick}-{index}"
    used.add(candidate)
    return candidate


def parse_plain_hosts(text: str, *, default_user: str = "") -> List[HostRow]:
    """Parse ``host``, ``host:port``, or ``ip hostname`` lines."""
    rows: List[HostRow] = []
    used: set = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        host = ""
        port = DEFAULT_PORT
        nickname = ""

        if " " in line and not line.startswith("["):
            parts = line.split()
            if len(parts) >= 2 and _looks_like_ip(parts[0]):
                host = parts[1]
                nickname = parts[1]
            else:
                host = parts[0]
                nickname = parts[0]
        elif ":" in line and not line.startswith("["):
            host_part, _, port_part = line.rpartition(":")
            host = host_part.strip()
            nickname = host
            port = _parse_port(port_part.strip())
        else:
            host = line
            nickname = line

        host = _clean_host(host)
        if not host:
            continue
        nickname = _unique_nickname(nickname or host, used)
        rows.append(HostRow(
            nickname=nickname, host=host,
            username=default_user, port=port,
        ))
    return rows


def _looks_like_ip(value: str) -> bool:
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", value):
        return True
    if ":" in value and re.match(r"^[0-9a-fA-F:]+$", value):
        return True
    return False


def parse_csv_hosts(text: str, *, default_user: str = "") -> List[HostRow]:
    """Parse CSV with header row: nickname, host, user/username, port."""
    rows: List[HostRow] = []
    used: set = set()
    reader = csv.DictReader(io.StringIO(text or ""))
    if not reader.fieldnames:
        return rows

    def col(*names: str) -> Optional[str]:
        lower = {k.lower().strip(): k for k in reader.fieldnames if k}
        for name in names:
            key = lower.get(name.lower())
            if key:
                return key
        return None

    nick_col = col("nickname", "name", "alias")
    host_col = col("host", "hostname", "address", "ip")
    user_col = col("user", "username")
    port_col = col("port")
    group_col = col("group")

    for item in reader:
        host = _clean_host(item.get(host_col or "", "") if host_col else "")
        if not host:
            continue
        nickname = _clean_host(item.get(nick_col or "", "") if nick_col else "")
        nickname = _unique_nickname(nickname or host, used)
        user = _clean_host(item.get(user_col or "", "") if user_col else "")
        port = _parse_port(item.get(port_col) if port_col else None)
        group = _clean_host(item.get(group_col or "", "") if group_col else "")
        rows.append(HostRow(
            nickname=nickname, host=host,
            username=user or default_user, port=port, group=group,
        ))
    return rows


def parse_ansible_ini(text: str, *, default_user: str = "") -> List[HostRow]:
    """Parse Ansible INI inventory (``[group]``, ``host ansible_host=…``)."""
    host_vars: Dict[str, Dict[str, str]] = {}
    current_group = ""

    for raw_line in (text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        section = re.match(r"^\[(.+)\]$", line)
        if section:
            name = section.group(1).strip()
            if name.endswith(":vars") or name.endswith(":children"):
                current_group = name.rsplit(":", 1)[0]
            else:
                current_group = name
            continue

        if " " in line:
            hostname, vars_part = line.split(None, 1)
        else:
            hostname, vars_part = line, ""

        hostname = _clean_host(hostname)
        if not hostname:
            continue

        entry = host_vars.setdefault(hostname, {})
        if current_group:
            entry.setdefault("ansible_group", current_group)
        entry.update(_parse_ansible_kv(vars_part))

    rows: List[HostRow] = []
    used: set = set()
    for hostname, vars_map in host_vars.items():
        host = _clean_host(vars_map.get("ansible_host") or hostname)
        user = _clean_host(vars_map.get("ansible_user") or default_user)
        port = _parse_port(vars_map.get("ansible_port"))
        group = _clean_host(vars_map.get("ansible_group") or "")
        nickname = _unique_nickname(hostname, used)
        rows.append(HostRow(
            nickname=nickname, host=host,
            username=user, port=port, group=group,
        ))
    return rows


def _parse_ansible_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    pattern = r"(\w+)=(\"[^\"]*\"|'[^']*'|\S+)"
    for key, value in re.findall(pattern, text or ""):
        out[key] = value.strip().strip('"').strip("'")
    return out


def parse_ansible_yaml_simple(text: str, *, default_user: str = "") -> List[HostRow]:
    """Parse a subset of Ansible YAML inventory (2-space indent, no lists).

    Supports ``all: hosts: name: {ansible_host: …}`` and ``groupname: hosts:``.
    """
    rows: List[HostRow] = []
    used: set = set()
    current_group = ""
    current_host: Optional[str] = None
    host_vars: Dict[str, Dict[str, str]] = {}

    for raw_line in (text or "").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        line = raw_line.strip()
        if line.endswith(":") and not line.startswith("ansible_"):
            key = line[:-1].strip()
            if indent == 0:
                current_group = key if key not in ("all", "ungrouped") else ""
                current_host = None
            elif indent >= 4 and key not in ("hosts", "children", "vars"):
                current_host = key
                host_vars.setdefault(current_host, {})
                if current_group:
                    host_vars[current_host]["ansible_group"] = current_group
            continue

        if ":" in line and current_host:
            k, _, v = line.partition(":")
            host_vars.setdefault(current_host, {})[k.strip()] = (
                v.strip().strip('"').strip("'"))

    for hostname, vars_map in host_vars.items():
        host = _clean_host(vars_map.get("ansible_host") or hostname)
        user = _clean_host(vars_map.get("ansible_user") or default_user)
        port = _parse_port(vars_map.get("ansible_port"))
        group = _clean_host(vars_map.get("ansible_group") or "")
        nickname = _unique_nickname(hostname, used)
        rows.append(HostRow(
            nickname=nickname, host=host,
            username=user, port=port, group=group,
        ))
    return rows


def parse_inventory(text: str, fmt: str, *, default_user: str = "") -> List[HostRow]:
    """Dispatch to the parser for ``fmt``."""
    if fmt == FORMAT_CSV:
        return parse_csv_hosts(text, default_user=default_user)
    if fmt == FORMAT_ANSIBLE_INI:
        return parse_ansible_ini(text, default_user=default_user)
    if fmt == FORMAT_ANSIBLE_YAML:
        return parse_ansible_yaml_simple(text, default_user=default_user)
    return parse_plain_hosts(text, default_user=default_user)


def _conn_key(host: str, username: str, port: int) -> Tuple[str, str, int]:
    return (host.lower(), (username or "").lower(), int(port or DEFAULT_PORT))


@dataclass
class ExistingConn:
    nickname: str
    host: str
    username: str
    port: int


def diff_inventory(
    rows: Sequence[HostRow],
    existing: Iterable[Any],
    *,
    default_user: str = "",
) -> List[DiffRow]:
    """Compare parsed rows against saved connections."""
    by_nick: Dict[str, ExistingConn] = {}
    by_key: Dict[Tuple[str, str, int], ExistingConn] = {}
    for conn in existing:
        nick = getattr(conn, "nickname", "") or ""
        host = (getattr(conn, "host", "") or "").strip()
        user = (getattr(conn, "username", "") or "").strip()
        port = int(getattr(conn, "port", DEFAULT_PORT) or DEFAULT_PORT)
        info = ExistingConn(nickname=nick, host=host, username=user, port=port)
        if nick:
            by_nick[nick.lower()] = info
        if host:
            by_key[_conn_key(host, user, port)] = info

    out: List[DiffRow] = []
    for row in rows:
        user = (row.username or default_user or "").strip()
        key = _conn_key(row.host, user, row.port)
        if row.nickname.lower() in by_nick:
            ex = by_nick[row.nickname.lower()]
            if _conn_key(ex.host, ex.username, ex.port) == key:
                out.append(DiffRow(row=row, status=STATUS_EXISTS,
                                   existing_nickname=ex.nickname))
            else:
                out.append(DiffRow(row=row, status=STATUS_CHANGED,
                                   existing_nickname=ex.nickname))
        elif key in by_key:
            ex = by_key[key]
            out.append(DiffRow(row=row, status=STATUS_EXISTS,
                               existing_nickname=ex.nickname))
        else:
            out.append(DiffRow(row=row, status=STATUS_NEW))
    return out


@dataclass
class ImportPlan:
    """What an import will do, bucketed so the UI handler stays thin.

    * ``new_by_group``: {group_name: [connection_data, …]} — created in groups.
    * ``new_no_group``: [connection_data, …] — created at the sidebar root.
    * ``changed``: [(nickname, connection_data, group), …] — existing nickname,
      different host: update in place (and re-group if a group is set).
    """

    new_by_group: Dict[str, List[Dict[str, Any]]]
    new_no_group: List[Dict[str, Any]]
    changed: List[Tuple[str, Dict[str, Any], str]]


def plan_import(
    diff_rows: Sequence[DiffRow],
    selected_indices: Iterable[int],
    *,
    default_user: str = "",
    default_group: str = "",
) -> ImportPlan:
    """Turn the selected diff rows into an actionable import plan. Each row's
    group is its own ``row.group`` when set, else ``default_group``. EXISTS rows
    are skipped (nothing to do); unselected rows are ignored."""
    new_by_group: Dict[str, List[Dict[str, Any]]] = {}
    new_no_group: List[Dict[str, Any]] = []
    changed: List[Tuple[str, Dict[str, Any], str]] = []
    selected = set(selected_indices)
    for index, item in enumerate(diff_rows):
        if index not in selected or item.status == STATUS_EXISTS:
            continue
        row = item.row
        group = (row.group or default_group or "").strip()
        data = row.connection_data(default_user=default_user)
        if item.status == STATUS_CHANGED:
            changed.append((row.nickname, data, group))
        elif group:
            new_by_group.setdefault(group, []).append(data)
        else:
            new_no_group.append(data)
    return ImportPlan(new_by_group=new_by_group, new_no_group=new_no_group,
                      changed=changed)


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._format = ctx.settings.get("format", FORMAT_PLAIN)
        self._default_user = ctx.settings.get("default_user", "")
        self._default_group = ctx.settings.get("default_group", "")
        self._last_path = ctx.settings.get("last_path", "")
        self._diff_rows: List[DiffRow] = []
        self._selected: set = set()
        self._list_box = None
        self._status_label = None
        self._parse_btn = None
        self._import_btn = None

        ctx.ui.register_page(
            "import", "Inventory Import", "document-import-symbolic",
            self._build_page)

    def deactivate(self) -> None:
        logger.info("inventory-import: deactivate")

    def _save_prefs(self) -> None:
        self.ctx.settings.set("format", self._format)
        self.ctx.settings.set("default_user", self._default_user)
        self.ctx.settings.set("default_group", self._default_group)
        if self._last_path:
            self.ctx.settings.set("last_path", self._last_path)

    # --- UI (gi imported lazily) ------------------------------------------
    def _build_page(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk, Pango

        self._Gtk = Gtk
        self._Adw = Adw

        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for fn in (box.set_margin_top, box.set_margin_bottom,
                   box.set_margin_start, box.set_margin_end):
            fn(18)
        outer.set_child(box)

        title = Gtk.Label(label="Import Host Inventory")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        subtitle = Gtk.Label(
            label="Load hosts from a plain list, CSV, or Ansible inventory file. "
                  "Preview new vs existing connections, then import selected rows.")
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_wrap(True)
        subtitle.set_xalign(0)
        box.append(subtitle)

        opts = Adw.PreferencesGroup(title="Options")
        self._format_dropdown = Gtk.DropDown.new_from_strings(
            [label for _val, label in FORMAT_CHOICES])
        for index, (value, _label) in enumerate(FORMAT_CHOICES):
            if value == self._format:
                self._format_dropdown.set_selected(index)
                break
        format_row = Adw.ActionRow(title="Format")
        format_row.add_suffix(self._format_dropdown)
        opts.add(format_row)

        self._user_entry = Adw.EntryRow(title="Default SSH username")
        self._user_entry.set_text(self._default_user)
        opts.add(self._user_entry)

        self._group_entry = Adw.EntryRow(title="Sidebar group (optional)")
        self._group_entry.set_text(self._default_group)
        opts.add(self._group_entry)
        box.append(opts)

        file_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._path_label = Gtk.Label(label=self._last_path or "(no file chosen)")
        self._path_label.set_hexpand(True)
        self._path_label.set_halign(Gtk.Align.START)
        self._path_label.set_ellipsize(Pango.EllipsizeMode.END)
        file_row.append(self._path_label)
        pick_btn = Gtk.Button(label="Choose file…")
        pick_btn.connect("clicked", self._on_pick_file)
        file_row.append(pick_btn)
        box.append(file_row)

        self._parse_btn = Gtk.Button(label="Parse & preview")
        self._parse_btn.add_css_class("suggested-action")
        self._parse_btn.set_halign(Gtk.Align.START)
        self._parse_btn.connect("clicked", self._on_parse_clicked)
        box.append(self._parse_btn)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        box.append(self._list_box)

        self._import_btn = Gtk.Button(label="Import selected")
        self._import_btn.set_halign(Gtk.Align.START)
        self._import_btn.set_sensitive(False)
        self._import_btn.connect("clicked", self._on_import_clicked)
        box.append(self._import_btn)

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("dim-label")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        return outer

    def _selected_format(self) -> str:
        index = self._format_dropdown.get_selected()
        if 0 <= index < len(FORMAT_CHOICES):
            return FORMAT_CHOICES[index][0]
        return FORMAT_PLAIN

    def _on_pick_file(self, _btn) -> None:
        Gtk = self._Gtk
        dialog = Gtk.FileDialog(title="Choose inventory file")
        dialog.open(None, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result) -> None:
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        path = file.get_path()
        if path:
            self._last_path = path
            self._path_label.set_text(path)
            self._save_prefs()

    def _on_parse_clicked(self, _btn) -> None:
        path = self._last_path
        if not path:
            self._set_status("Choose an inventory file first.")
            return
        self._format = self._selected_format()
        self._default_user = self._user_entry.get_text().strip()
        self._default_group = self._group_entry.get_text().strip()
        self._save_prefs()
        self._parse_btn.set_sensitive(False)
        self._set_status("Parsing…")

        def worker():
            error = ""
            diff_rows: List[DiffRow] = []
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                rows = parse_inventory(
                    text, self._format, default_user=self._default_user)
                existing = self.ctx.list_connections() if hasattr(
                    self.ctx, "list_connections") else []
                diff_rows = diff_inventory(
                    rows, existing, default_user=self._default_user)
            except OSError as exc:
                error = str(exc)
            self.ctx.run_on_ui_thread(
                self._on_parse_done, diff_rows, error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_parse_done(self, diff_rows: List[DiffRow], error: str) -> None:
        self._parse_btn.set_sensitive(True)
        if error:
            self._set_status(f"Failed to read file: {error}")
            return
        self._diff_rows = diff_rows
        self._selected = {
            index for index, item in enumerate(diff_rows)
            if item.status == STATUS_NEW
        }
        self._rebuild_preview()
        new_count = sum(1 for d in diff_rows if d.status == STATUS_NEW)
        self._set_status(
            f"Found {len(diff_rows)} host(s): {new_count} new, "
            f"{sum(1 for d in diff_rows if d.status == STATUS_CHANGED)} changed, "
            f"{sum(1 for d in diff_rows if d.status == STATUS_EXISTS)} existing.")
        self._import_btn.set_sensitive(bool(self._selected))

    def _rebuild_preview(self) -> None:
        Gtk = self._Gtk
        while child := self._list_box.get_first_child():
            self._list_box.remove(child)

        if not self._diff_rows:
            empty = Gtk.Label(label="No hosts parsed yet.")
            empty.add_css_class("dim-label")
            empty.set_margin_top(12)
            empty.set_margin_bottom(12)
            self._list_box.append(empty)
            return

        for index, item in enumerate(self._diff_rows):
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hbox.set_margin_top(6)
            hbox.set_margin_bottom(6)
            check = Gtk.CheckButton()
            check.set_active(index in self._selected)
            check.set_sensitive(item.status != STATUS_EXISTS)
            check.connect("toggled", self._on_row_toggled, index)
            hbox.append(check)
            label = Gtk.Label(
                label=self._row_label(item),
                xalign=0, hexpand=True)
            hbox.append(label)
            row.set_child(hbox)
            self._list_box.append(row)

    def _row_label(self, item: DiffRow) -> str:
        row = item.row
        user = row.username or self._default_user
        bits = [f"{row.nickname} → {row.host}:{row.port}"]
        if user:
            bits.append(f"user={user}")
        if row.group:
            bits.append(f"group={row.group}")
        status = item.status.upper()
        if item.status == STATUS_EXISTS:
            status += f" ({item.existing_nickname})"
        bits.append(f"[{status}]")
        return "  ".join(bits)

    def _on_row_toggled(self, check, index: int) -> None:
        if check.get_active():
            self._selected.add(index)
        else:
            self._selected.discard(index)
        self._import_btn.set_sensitive(bool(self._selected))

    def _on_import_clicked(self, _btn) -> None:
        if not self._selected:
            return
        default_user = self._user_entry.get_text().strip()
        default_group = self._group_entry.get_text().strip()
        plan = plan_import(
            self._diff_rows, self._selected,
            default_user=default_user, default_group=default_group)

        imported = 0
        errors: List[str] = []

        # New connections that belong in a group — one batched sidebar rebuild
        # per group.
        for group_name, payloads in plan.new_by_group.items():
            try:
                _gid, infos = self.ctx.add_connection_group(group_name, payloads)
                imported += len(infos)
            except Exception as exc:  # provisioning is best-effort
                errors.append(f"{group_name}: {exc}")

        # New connections at the sidebar root.
        for data in plan.new_no_group:
            try:
                self.ctx.add_connection(data)
                imported += 1
            except ValueError as exc:
                errors.append(f"{data.get('nickname', '?')}: {exc}")

        # Existing nicknames whose host/user/port changed — update in place.
        for nickname, data, group_name in plan.changed:
            try:
                if self.ctx.update_connection(nickname, data):
                    imported += 1
                    if group_name:
                        gid = self.ctx.create_group(group_name)
                        if gid:
                            self.ctx.add_connection_to_group(nickname, gid)
                else:
                    errors.append(f"{nickname}: not found")
            except Exception as exc:
                errors.append(f"{nickname}: {exc}")

        self._set_status(
            f"Imported {imported} connection(s)."
            + (f" Errors: {'; '.join(errors)}" if errors else ""))
        self.ctx.ui.notify(f"Imported {imported} connection(s)")
        self._selected.clear()
        self._import_btn.set_sensitive(False)
        # Re-diff against the now-updated connection list so imported rows flip
        # to EXISTS and the preview reflects reality.
        self._refresh_diff()

    def _refresh_diff(self) -> None:
        """Recompute statuses for the already-parsed rows against the current
        saved connections and rebuild the preview."""
        rows = [item.row for item in self._diff_rows]
        existing = (self.ctx.list_connections()
                    if hasattr(self.ctx, "list_connections") else [])
        self._diff_rows = diff_inventory(
            rows, existing, default_user=self._default_user)
        self._selected = {
            index for index, item in enumerate(self._diff_rows)
            if item.status == STATUS_NEW
        }
        self._rebuild_preview()
        self._import_btn.set_sensitive(bool(self._selected))

    def _set_status(self, text: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(text)
