"""EasyEnv Workspaces — a real-partner example plugin for sshPilot.

Integrates the easyenv.io CLI (github.com/donedeploy/easyenv-cli), which manages
ephemeral cloud dev "workspaces". Unlike an IP-based VPS provider, easyenv is
**mesh-based**: `easyenv workspace ssh <id>` drops you into a shell over the
easyenv mesh and never exposes a host/port/user/key. So the integration has two
halves, both on the public ``sshpilot.plugins.api`` surface:

1. A **protocol backend** ``easyenv`` whose ``build_spawn`` runs
   ``easyenv workspace ssh <workspace-id>`` in the terminal (same shape as the
   built-in telnet backend). No SSH-only capabilities — SFTP/forwarding/
   copy-key UI stays hidden, since none of it applies to the mesh.
2. A **management page** ("EasyEnv Workspaces", under the Tools menu) that
   drives the CLI for auth, listing, creating, and lifecycle, then uses
   ``ctx.add_connection`` + ``ctx.open_connection`` to open a terminal.

All CLI calls run on a background thread and marshal UI updates back via
``ctx.run_on_ui_thread``. The only sshPilot import is ``sshpilot.plugins.api``.

The same plugin drives the real ``easyenv`` binary or the bundled local stub,
depending on which ``easyenv`` is first on PATH. See the README.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from sshpilot.plugins.api import (  # noqa: E402
    Events,
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    ProtocolError,
    SpawnSpec,
    SshPilotPlugin,
)

logger = logging.getLogger(__name__)

INSTALL_HINT = "The 'easyenv' CLI was not found on PATH. Install it from https://easyenv.io/cli"


class _EasyEnvNotFound(RuntimeError):
    pass


def _is_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def easyenv_argv(args, binary_path=None):
    """Full argv to invoke easyenv with ``args``.

    Honors an explicitly configured ``binary_path``; otherwise resolves
    ``easyenv`` on PATH. Inside a Flatpak sandbox the host binary isn't on the
    sandbox PATH, so the call is routed through ``flatpak-spawn --host`` (the
    app manifest already grants ``--talk-name=org.freedesktop.Flatpak``)."""
    exe = binary_path or shutil.which("easyenv")
    prefix = []
    if _is_flatpak():
        prefix = ["flatpak-spawn", "--host"]
        exe = binary_path or "easyenv"  # resolved on the host, not sandbox PATH
    if not exe:
        raise _EasyEnvNotFound(INSTALL_HINT)
    return prefix + [exe] + list(args)


def _one_workspace(w):
    """Normalize one workspace object from the EasyEnv API/CLI JSON.

    Field names follow the EasyEnv API (WorkspaceList): ``uuid`` is the stable
    id used by `workspace ssh`, ``title`` is the display name, ``status`` is one
    of active/not_started/stopped/in_progress/failed. Alternatives are
    tolerated in case the CLI reshapes the payload."""
    if not isinstance(w, dict):
        return None
    wid = str(w.get("uuid") or w.get("workspace_id") or w.get("id") or "")
    name = str(w.get("title") or w.get("name") or wid)
    status = str(w.get("status") or w.get("state") or "unknown")
    if wid or name:
        return {"id": wid, "name": name, "status": status}
    return None


def _parse_workspaces(text):
    """Parse `workspace list --output json` (list, or a paginated/wrapped dict)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        # EasyEnv list endpoints are paginated (PaginatedWorkspaceListList:
        # {"results": [...]}); also tolerate workspaces/items or a bare object.
        data = (data.get("results") or data.get("workspaces")
                or data.get("items") or [data])
    out = []
    for w in data if isinstance(data, list) else []:
        ws = _one_workspace(w)
        if ws is not None:
            out.append(ws)
    return out


def _parse_one(text):
    try:
        return _one_workspace(json.loads(text))
    except (ValueError, TypeError):
        return None


def _one_machine(m):
    """Normalize one machine/box object. Real shape (machine list / workspace
    .boxes): {uuid, title, status, host_address, recipe{…}}."""
    if not isinstance(m, dict):
        return None
    mid = str(m.get("uuid") or m.get("id") or "")
    name = str(m.get("title") or m.get("name") or mid)
    status = str(m.get("status") or m.get("state") or "unknown")
    if mid:
        return {"id": mid, "name": name, "status": status}
    return None


def _parse_machines(text):
    """Parse `machine list -w <ws> --output json` (a list), or a `workspace
    get` object whose machines live under `boxes` (or `machines`)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = data.get("boxes") or data.get("machines") or data.get("results") or []
    out = []
    for m in data if isinstance(data, list) else []:
        mm = _one_machine(m)
        if mm is not None:
            out.append(mm)
    return out


class EasyEnvBackend(ProtocolBackend):
    """Protocol backend: the connection IS `easyenv workspace ssh <id>`."""

    protocol_id = "easyenv"
    display_name = "EasyEnv Workspace"
    default_port = None

    def capabilities(self):
        # Mesh-mediated: none of sshPilot's SSH-only features (SFTP,
        # port-forwarding, ssh-copy-id, system terminal) apply.
        return frozenset()

    def connection_fields(self):
        return [
            FieldSpec(key="workspace_id", label="Workspace UUID", kind="text",
                      required=True, placeholder="workspace uuid"),
            FieldSpec(key="machine_id", label="Machine UUID (optional)", kind="text",
                      placeholder="machine uuid; empty = first machine"),
            FieldSpec(key="machine_name", label="Machine name (display)", kind="text",
                      placeholder="optional label"),
        ]

    def validate(self, data):
        if not (data.get("workspace_id") or data.get("host") or data.get("nickname")):
            return ["A workspace id is required."]
        return []

    def build_spawn(self, connection, ctx):
        data = getattr(connection, "data", None) or {}
        wsid = data.get("workspace_id") or data.get("host") or getattr(connection, "nickname", "")
        if not wsid:
            raise ProtocolError("No workspace id configured for this connection.")
        machine = data.get("machine_id") or data.get("machine")
        if machine:
            # Connect to a specific node: easyenv machine ssh <m> -w <ws>
            args = ["machine", "ssh", str(machine), "-w", str(wsid)]
        else:
            # No machine pinned: easyenv workspace ssh <ws> (first machine)
            args = ["workspace", "ssh", str(wsid)]
        try:
            argv = easyenv_argv(args, data.get("binary_path"))
        except _EasyEnvNotFound as exc:
            raise ProtocolError(str(exc)) from exc
        return SpawnSpec(argv=argv, env=dict(os.environ))


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._status_label = None
        self._list_box = None
        self._auth_label = None

        ctx.register_protocol(EasyEnvBackend())
        ctx.ui.register_page("workspaces", "EasyEnv Workspaces",
                             "network-server-symbolic", self._build_page)
        ctx.events.subscribe(Events.APP_STARTED, self._on_app_started)
        ctx.events.subscribe(Events.SESSION_OPENED, self._on_session_opened)
        ctx.events.subscribe(Events.CONNECTION_DELETED, self._on_connection_deleted)

    # --- CLI plumbing (blocking; call from a worker thread) -----------
    def _binary_path(self):
        try:
            return self.ctx.settings.get("binary_path") if self.ctx.settings else None
        except Exception:
            return None

    def _run(self, args, timeout=30):
        argv = easyenv_argv(args, self._binary_path())
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    # --- testable, widget-free operations -----------------------------
    def _do_whoami(self):
        try:
            out = self._run(["auth", "whoami"], timeout=15)
            return out.stdout.strip() if out.returncode == 0 else None
        except (_EasyEnvNotFound, OSError, subprocess.SubprocessError):
            return None

    def _do_login(self, token):
        out = self._run(["auth", "login", "--token", token], timeout=20)
        return out.returncode == 0

    def _do_list(self):
        out = self._run(["workspace", "list", "--output", "json"], timeout=30)
        return _parse_workspaces(out.stdout) if out.returncode == 0 else []

    def _do_create(self, name, template="python_devenv", ttl="8h"):
        args = ["workspace", "create", "--name", name, "--output", "json"]
        if template:
            args += ["--template", template]
        if ttl:
            args += ["--ttl", ttl]
        out = self._run(args, timeout=120)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip() or "workspace create failed")
        ws = _parse_one(out.stdout)
        if ws is None:
            raise RuntimeError("could not parse created workspace")
        return ws

    def _do_action(self, verb, wsid):
        args = ["workspace", verb, wsid]
        if verb == "delete":
            args.append("--force")
        out = self._run(args, timeout=60)
        return out.returncode == 0

    def _do_machines(self, wsid):
        """List a workspace's machines/nodes via `machine list -w <ws>`."""
        out = self._run(["machine", "list", "-w", wsid, "--output", "json"], timeout=30)
        return _parse_machines(out.stdout) if out.returncode == 0 else []

    @staticmethod
    def _node_nicknames(ws_title, machines):
        """Map machines -> globally-unique, stable, readable nicknames.

        Machine titles repeat within a workspace (e.g. 3x "Ubuntu 24.04 LTS"),
        so duplicates get a 1-based index assigned in stable uuid order. The
        workspace title prefix disambiguates across workspaces."""
        from collections import Counter
        counts = Counter(m["name"] for m in machines)
        seen = {}
        result = []
        for m in sorted(machines, key=lambda x: x["id"]):
            label = m["name"]
            if counts[label] > 1:
                seen[label] = seen.get(label, 0) + 1
                label = f"{label} {seen[label]}"
            result.append((m, f"{ws_title} / {label}"))
        return result

    def _provision_workspace(self, ws_title, wsid, machines, open_first=False):
        """Materialize a workspace in sshPilot (UI thread). One machine -> a
        single connection; multiple -> a group with one connection per node.
        Idempotent (find-or-create group, reuse existing connections)."""
        if not machines:
            machines = [{"id": "", "name": ws_title, "status": "unknown"}]

        if len(machines) == 1:
            m = machines[0]
            data = {"protocol": "easyenv", "nickname": ws_title, "host": ws_title,
                    "workspace_id": wsid, "machine_id": m["id"],
                    "machine_name": m["name"]}
            try:
                self.ctx.add_connection(data)
            except ValueError:
                pass
            self.ctx.ui.notify(f"EasyEnv: {ws_title} ready")
            if open_first:
                self.ctx.open_connection(ws_title)
            return ws_title

        node_data = []
        first_nick = None
        for m, nick in self._node_nicknames(ws_title, machines):
            node_data.append({"protocol": "easyenv", "nickname": nick, "host": nick,
                              "workspace_id": wsid, "machine_id": m["id"],
                              "machine_name": m["name"]})
            if first_nick is None:
                first_nick = nick
        self.ctx.add_connection_group(f"EasyEnv: {ws_title}", node_data)
        self.ctx.ui.notify(f"EasyEnv: {ws_title} — {len(machines)} nodes ready")
        if open_first and first_nick:
            self.ctx.open_connection(first_nick)
        return first_nick

    def _open_workspace_async(self, ws, open_first=False):
        """Enumerate a workspace's machines off-thread, then provision."""
        def worker():
            try:
                machines = self._do_machines(ws["id"])
            except Exception:
                logger.exception("easyenv machine list failed")
                machines = []
            self.ctx.run_on_ui_thread(self._provision_workspace, ws["name"],
                                      ws["id"], machines, open_first)
        threading.Thread(target=worker, daemon=True).start()

    # --- events -------------------------------------------------------
    def _on_app_started(self, _payload):
        self._refresh_async()

    def _on_session_opened(self, info):
        logger.info("easyenv: session opened for %s", info.connection.nickname)

    def _on_connection_deleted(self, info):
        logger.info("easyenv: connection %s removed from sshPilot", info.nickname)

    # --- UI -----------------------------------------------------------
    def _build_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(box, m)(18)

        title = Gtk.Label(label="EasyEnv Workspaces")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        # Auth row
        self._auth_label = Gtk.Label(label="Checking sign-in…")
        self._auth_label.set_halign(Gtk.Align.START)
        box.append(self._auth_label)

        login_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._token_entry = Gtk.Entry()
        self._token_entry.set_visibility(False)
        self._token_entry.set_placeholder_text("service token from easyenv.io")
        self._token_entry.set_hexpand(True)
        login_btn = Gtk.Button(label="Log in")
        login_btn.connect("clicked", self._on_login_clicked)
        login_row.append(self._token_entry)
        login_row.append(login_btn)
        box.append(login_row)

        # Create row
        create_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._name_entry = Gtk.Entry()
        self._name_entry.set_placeholder_text("workspace name")
        self._name_entry.set_hexpand(True)
        self._template_entry = Gtk.Entry()
        self._template_entry.set_placeholder_text("template (e.g. python_devenv)")
        create_btn = Gtk.Button(label="Create")
        create_btn.add_css_class("suggested-action")
        create_btn.connect("clicked", self._on_create_clicked)
        create_row.append(self._name_entry)
        create_row.append(self._template_entry)
        create_row.append(create_btn)
        box.append(create_row)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.set_halign(Gtk.Align.START)
        refresh_btn.connect("clicked", lambda _b: self._refresh_async())
        box.append(refresh_btn)

        self._list_box = Gtk.ListBox()
        self._list_box.add_css_class("boxed-list")
        box.append(self._list_box)

        self._status_label = Gtk.Label(label="")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        self._refresh_async()
        return box

    def _set_status(self, text):
        if self._status_label is not None:
            self._status_label.set_text(text)

    def _on_login_clicked(self, _btn):
        token = self._token_entry.get_text().strip()
        if not token:
            return
        self._set_status("Signing in…")

        def worker():
            ok = False
            try:
                ok = self._do_login(token)
            except Exception:
                logger.exception("easyenv login failed")
            self.ctx.run_on_ui_thread(self._after_login, ok)

        threading.Thread(target=worker, daemon=True).start()

    def _after_login(self, ok):
        self._token_entry.set_text("")
        self._set_status("Signed in." if ok else "Sign-in failed.")
        self._refresh_async()

    def _on_create_clicked(self, _btn):
        name = self._name_entry.get_text().strip()
        if not name:
            return
        template = self._template_entry.get_text().strip() or "python_devenv"
        self._set_status(f"Creating {name}…")

        def worker():
            try:
                ws = self._do_create(name, template)
            except Exception as exc:
                self.ctx.run_on_ui_thread(self._set_status, f"Create failed: {exc}")
                return
            self.ctx.run_on_ui_thread(self._after_create, ws)

        threading.Thread(target=worker, daemon=True).start()

    def _after_create(self, ws):
        self._name_entry.set_text("")
        self._set_status(f"Created {ws['name']}.")
        self.ctx.ui.notify(f"Workspace {ws['name']} created")
        # Materialize the workspace (group of nodes if multi-VM); don't
        # auto-open every node — let the user expand and pick one.
        self._open_workspace_async(ws, open_first=False)
        self._refresh_async()

    def _refresh_async(self):
        def worker():
            account = self._do_whoami()
            items = self._do_list() if account else []
            self.ctx.run_on_ui_thread(self._render, account, items)

        threading.Thread(target=worker, daemon=True).start()

    def _render(self, account, items):
        if self._auth_label is not None:
            self._auth_label.set_text(
                f"Signed in as {account}" if account else "Not signed in — paste a token below")
        if self._list_box is None:
            return
        child = self._list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list_box.remove(child)
            child = nxt
        for ws in items:
            self._list_box.append(self._workspace_row(ws))

    def _workspace_row(self, ws):
        row = Gtk.ListBoxRow()
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(hb, m)(6)
        label = Gtk.Label(label=f"{ws['name']}  ·  {ws['status']}")
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        hb.append(label)
        for text, verb in (("Open", None), ("Start", "start"),
                           ("Stop", "stop"), ("Delete", "delete")):
            btn = Gtk.Button(label=text)
            btn.connect("clicked", self._on_row_action, verb, ws)
            hb.append(btn)
        row.set_child(hb)
        return row

    def _on_row_action(self, _btn, verb, ws):
        if verb is None:  # Open in sshPilot (group of nodes; open first)
            self._set_status(f"Opening {ws['name']}…")
            self._open_workspace_async(ws, open_first=True)
            return
        self._set_status(f"{verb} {ws['name']}…")

        def worker():
            ok = False
            try:
                ok = self._do_action(verb, ws["id"])
            except Exception:
                logger.exception("easyenv %s failed", verb)
            self.ctx.run_on_ui_thread(self._after_action, verb, ws, ok)

        threading.Thread(target=worker, daemon=True).start()

    def _after_action(self, verb, ws, ok):
        self._set_status(f"{verb} {ws['name']}: {'ok' if ok else 'failed'}")
        self._refresh_async()
