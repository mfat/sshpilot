"""EasyEnv Workspaces — a real-partner example plugin for sshPilot.

Integrates easyenv.io entirely through its **REST API** and connects via
**standard SSH to public-IP boxes** — no `easyenv` CLI binary and no NetBird
mesh. Provisioning a workspace from a **recipe** with
``settings.public_ip_requested = true`` gives each box a routable public IP plus
``host_address`` / ``ssh_username`` / ``ssh_port`` / ``vm_password``; the plugin
turns those into normal sshPilot SSH connections (one node → one connection;
several nodes → a sidebar group of per-node connections). Opening one is just
sshPilot's native SSH (the ``vm_password`` is stored in the keyring and fed via
sshpass), so it works from anywhere.

Recipes are used rather than the pre-baked multi-VM *templates* because only
recipe-built boxes can be given a public IP over REST; template workspaces come
back on the NetBird mesh (unroutable ``box-…`` host names) and can't be reached
by plain SSH. Boxes without a routable address are skipped with a warning.

Auth is the partner's header scheme: ``X-Service-Token`` + ``Account-ID``. The
user pastes a service token from
https://dashboard.easyenv.io/auth/login?redirect=/dashboard/profile ; the
plugin stores it in the keyring (``ctx.secrets``) and the active account uuid in
``ctx.settings``.

Only stdlib (urllib/json/threading) + gi + ``sshpilot.plugins.api`` are
imported — no third-party deps, no CLI.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from sshpilot.plugins.api import Events, PluginContext, SshPilotPlugin  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.easyenv.io"
DASHBOARD_URL = "https://dashboard.easyenv.io/auth/login?redirect=/dashboard/profile"
HTTP_TIMEOUT = 30
POLL_INTERVAL = 10
POLL_TIMEOUT = 600

# Forced into each provisioned connection's ssh config: accept the ephemeral
# host key, and use the vm_password (don't let the user's agent keys trip
# "Too many authentication failures" before password auth is tried).
EASYENV_SSH_OPTIONS = (
    "StrictHostKeyChecking accept-new\n"
    "UserKnownHostsFile /dev/null\n"
    "PreferredAuthentications password\n"
    "PubkeyAuthentication no\n"
    "IdentitiesOnly yes"
)


class EasyEnvError(RuntimeError):
    pass


# --- REST client (stdlib urllib) ------------------------------------------

def _api(method, path, token, account, base_url=DEFAULT_BASE_URL, body=None,
         timeout=HTTP_TIMEOUT):
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Service-Token", token or "")
    req.add_header("Account-ID", account or "")
    req.add_header("Content-Type", "application/json")
    # Cloudflare in front of the API rejects the default "Python-urllib/x.y"
    # user agent (403 / error 1010); send an explicit one.
    req.add_header("User-Agent", "sshpilot-easyenv-plugin/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise EasyEnvError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise EasyEnvError(f"{method} {path} failed: {exc.reason}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _results(payload):
    if isinstance(payload, dict):
        return payload.get("results") or payload.get("items") or []
    return payload if isinstance(payload, list) else []


class EasyEnvClient:
    def __init__(self, token, account, base_url=DEFAULT_BASE_URL):
        self.token = token
        self.account = account
        self.base_url = base_url or DEFAULT_BASE_URL

    def _call(self, method, path, body=None):
        return _api(method, path, self.token, self.account, self.base_url, body)

    def accounts(self):
        # account not known yet -> empty Account-ID
        return _results(_api("GET", "/v1/accounts/", self.token, "", self.base_url))

    def recipes(self, term=""):
        q = "?search=" + urllib.parse.quote(term) if term else ""
        return _results(self._call("GET", f"/v1/recipes/{q}"))

    def workspaces(self):
        return _results(self._call("GET", "/v1/workspaces/"))

    def workspace(self, uuid):
        return self._call("GET", f"/v1/workspaces/{uuid}/")

    def create_workspace(self, body):
        return self._call("POST", "/v1/workspaces/", body)

    def start(self, uuid):
        return self._call("POST", f"/v1/workspaces/{uuid}/start/")

    def stop(self, uuid):
        return self._call("POST", f"/v1/workspaces/{uuid}/stop/")

    def delete(self, uuid):
        return self._call("DELETE", f"/v1/workspaces/{uuid}/")


def _ws_view(w):
    if not isinstance(w, dict):
        return None
    uuid = str(w.get("uuid") or w.get("id") or "")
    title = str(w.get("title") or uuid)
    status = str(w.get("status") or "unknown")
    return {"uuid": uuid, "title": title, "status": status} if uuid else None


def _is_running(status):
    return str(status or "").lower() in ("active", "started", "running")


# EasyEnv workspaces are ephemeral: once their duration expires (or they're
# stopped) they become terminal — the VM is gone and the API rejects start with
# "Stopped workspace cannot be started." A terminal workspace can't be resumed,
# only recreated.
_TERMINAL_STATUSES = (
    "stopped", "failed", "expired", "terminated", "archived", "deleted", "error",
)


def _is_terminal(status):
    return str(status or "").lower() in _TERMINAL_STATUSES


def _friendly(exc):
    """A user-facing message for an EasyEnvError; collapses the known
    'cannot be started' 400 into a plain, actionable sentence."""
    text = str(exc)
    if "cannot be started" in text.lower():
        return "stopped/expired and can't be restarted — use Recreate to make a fresh one"
    return text


def _looks_routable(addr):
    """True if ``addr`` is something plain SSH can reach: a literal IP, or a
    DNS name with a dot. EasyEnv's NetBird mesh boxes use unroutable names like
    ``box-3VZo6G4A-hAm88YxD`` (no dots) — those are not reachable without the
    mesh, so we treat them as non-routable and skip them."""
    if not addr:
        return False
    try:
        ipaddress.ip_address(addr)
        return True
    except ValueError:
        pass
    return "." in addr and not addr.lower().startswith("box-")


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._status_label = None
        self._list_box = None
        self._auth_label = None
        self._token_entry = None
        self._name_entry = None
        self._recipe_dropdown = None
        self._recipe_values = []  # parallel to dropdown labels (recipe uuid)
        self._nodes_spin = None

        ctx.ui.register_page("workspaces", "EasyEnv Workspaces",
                             "network-server-symbolic", self._build_page)
        ctx.events.subscribe(Events.APP_STARTED, self._on_app_started)

    # --- config / client ---------------------------------------------
    def _token(self):
        try:
            return self.ctx.secrets.get("service_token")
        except Exception:
            return None

    def _account(self):
        return self.ctx.settings.get("account_uuid")

    def _base_url(self):
        return self.ctx.settings.get("base_url", DEFAULT_BASE_URL)

    def _client(self):
        token = self._token()
        if not token:
            return None
        return EasyEnvClient(token, self._account(), self._base_url())

    # --- events ------------------------------------------------------
    def _on_app_started(self, _payload):
        self._refresh_async()

    # --- auth --------------------------------------------------------
    def _on_signin_clicked(self, _btn):
        token = self._token_entry.get_text().strip() if self._token_entry else ""
        if not token:
            self._set_status("Paste a service token first.")
            return
        self._set_status("Signing in…")

        def worker():
            try:
                self.ctx.secrets.set("service_token", token)
                accounts = EasyEnvClient(token, None, self._base_url()).accounts()
            except Exception as exc:
                self.ctx.run_on_ui_thread(self._set_status, f"Sign-in failed: {exc}")
                return
            self.ctx.run_on_ui_thread(self._after_signin, accounts)

        threading.Thread(target=worker, daemon=True).start()

    def _after_signin(self, accounts):
        if not accounts:
            self._set_status("No accounts for this token.")
            return
        # Auto-select when there's only one; otherwise keep the first (a fuller
        # picker is straightforward to add, but most tokens map to one account).
        acct = accounts[0]
        self.ctx.settings.set("account_uuid", acct.get("uuid"))
        if self._token_entry is not None:
            self._token_entry.set_text("")
        self._set_status(f"Signed in as {acct.get('title')}")
        self._refresh_recipes_async()
        self._refresh_async()

    # --- REST operations (blocking; call from a worker) --------------
    def _do_recipes(self):
        c = self._client()
        if c is None:
            return []
        return [r for r in (_recipe_view(x) for x in c.recipes()) if r]

    def _do_list(self):
        c = self._client()
        if c is None:
            return []
        return [v for v in (_ws_view(w) for w in c.workspaces()) if v]

    def _poll_active(self, c, uuid):
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            ws = c.workspace(uuid)
            status = (ws or {}).get("status")
            self.ctx.run_on_ui_thread(
                self._set_status, f"{(ws or {}).get('title', uuid)}: {status}")
            if _is_running(status):
                return ws
            if str(status).lower() in ("failed", "stopped"):
                raise EasyEnvError(f"workspace entered {status!r}")
            time.sleep(POLL_INTERVAL)
        raise EasyEnvError("timed out waiting for the workspace to become active")

    # --- connection materialization (UI thread) ----------------------
    @staticmethod
    def _box_to_data(nickname, box):
        return {
            "protocol": "ssh",
            "nickname": nickname,
            "hostname": box.get("host_address"),
            "host": box.get("host_address"),
            "username": box.get("ssh_username") or "root",
            "port": int(box.get("ssh_port") or 22),
            "password": box.get("vm_password") or "",
            "auth_method": 1,  # password mode -> sshPilot feeds vm_password via sshpass
            "extra_ssh_config": EASYENV_SSH_OPTIONS,
        }

    @staticmethod
    def _node_nicknames(ws_title, boxes):
        """Globally-unique, stable, readable nicknames; index duplicate box
        titles (e.g. several 'Ubuntu 24.04 LTS') in stable uuid order."""
        from collections import Counter
        titles = [str(b.get("title") or b.get("uuid") or "node") for b in boxes]
        counts = Counter(titles)
        seen = {}
        pairs = []
        for b in sorted(boxes, key=lambda x: str(x.get("uuid") or "")):
            label = str(b.get("title") or b.get("uuid") or "node")
            if counts[label] > 1:
                seen[label] = seen.get(label, 0) + 1
                label = f"{label} {seen[label]}"
            pairs.append((b, f"{ws_title} / {label}"))
        return pairs

    def _upsert_connection(self, data):
        try:
            self.ctx.add_connection(data)
        except ValueError:
            # Exists already — refresh host/password (workspace may have been
            # stopped and restarted with a new IP/credentials).
            self.ctx.update_connection(data["nickname"], data)

    def _materialize(self, ws, open_after=False):
        all_boxes = ws.get("boxes") or []
        title = ws.get("title") or ws.get("uuid")
        boxes = [b for b in all_boxes if _looks_routable(b.get("host_address"))]
        mesh = [b for b in all_boxes
                if b.get("host_address") and not _looks_routable(b.get("host_address"))]
        if not boxes:
            if mesh:
                self.ctx.ui.notify(
                    f"EasyEnv: {title} is mesh-only (no public IP) — recreate it "
                    f"from a recipe with public IP to reach it over SSH")
            else:
                self.ctx.ui.notify(f"EasyEnv: {title} has no reachable boxes yet")
            return
        if mesh:
            self.ctx.ui.notify(
                f"EasyEnv: {title} — skipped {len(mesh)} mesh-only node(s) "
                f"without a public IP")
        if len(boxes) == 1:
            nick = title
            self._upsert_connection(self._box_to_data(nick, boxes[0]))
            self.ctx.ui.notify(f"EasyEnv: {title} ready")
            if open_after:
                self.ctx.open_connection(nick)
            return

        group_id = self.ctx.create_group(f"EasyEnv: {title}")
        first_nick = None
        for box, nick in self._node_nicknames(title, boxes):
            self._upsert_connection(self._box_to_data(nick, box))
            if group_id:
                self.ctx.add_connection_to_group(nick, group_id)
            if first_nick is None:
                first_nick = nick
        self.ctx.ui.notify(f"EasyEnv: {title} — {len(boxes)} nodes ready")
        if open_after and first_nick:
            self.ctx.open_connection(first_nick)

    # --- provisioning / lifecycle (off-thread workers) ---------------
    def _on_create_clicked(self, _btn):
        name = self._name_entry.get_text().strip() if self._name_entry else ""
        if not name:
            return
        idx = self._recipe_dropdown.get_selected() if self._recipe_dropdown else -1
        if not self._recipe_values or not (0 <= idx < len(self._recipe_values)):
            self._set_status("Pick a recipe (sign in to load recipes).")
            return
        recipe = self._recipe_values[idx]
        nodes = int(self._nodes_spin.get_value()) if self._nodes_spin else 1
        nodes = max(1, nodes)
        self._set_status(f"Creating {name}…")

        def worker():
            c = self._client()
            try:
                if nodes == 1:
                    box_specs = [{"title": name, "recipe": recipe, "position": 0}]
                else:
                    box_specs = [{"title": f"{name}-{i + 1}", "recipe": recipe,
                                  "position": i} for i in range(nodes)]
                body = {"title": name, "duration": 1, "duration_unit": "hours",
                        "boxes": box_specs,
                        "settings": {"public_ip_requested": True}}
                ws = c.create_workspace(body)
                c.start(ws["uuid"])
                ws = self._poll_active(c, ws["uuid"])
            except Exception as exc:
                self.ctx.run_on_ui_thread(self._set_status, f"Create failed: {exc}")
                return
            self.ctx.run_on_ui_thread(self._after_create, ws)

        threading.Thread(target=worker, daemon=True).start()

    def _after_create(self, ws):
        if self._name_entry is not None:
            self._name_entry.set_text("")
        self._materialize(ws, open_after=False)
        self._refresh_async()

    def _open_workspace_async(self, ws_view, open_after=True):
        """Open the workspace: materialize if running, start+poll if it's in a
        transitional state, or refuse (with a clear message) if it's terminal —
        a stopped/expired workspace cannot be restarted."""
        def worker():
            c = self._client()
            try:
                ws = c.workspace(ws_view["uuid"])
                status = (ws or {}).get("status")
                if _is_terminal(status):
                    self.ctx.run_on_ui_thread(
                        self.ctx.ui.notify,
                        f"EasyEnv: {ws_view['title']} is {status} and can't be "
                        f"reopened — use Recreate to make a fresh one")
                    return
                if not _is_running(status):
                    self.ctx.run_on_ui_thread(self._set_status,
                                              f"Starting {ws_view['title']}…")
                    c.start(ws_view["uuid"])
                    ws = self._poll_active(c, ws_view["uuid"])
            except EasyEnvError as exc:
                logger.warning("easyenv open %s failed: %s", ws_view["title"], exc)
                self.ctx.run_on_ui_thread(
                    self._set_status, f"{ws_view['title']}: {_friendly(exc)}")
                return
            except Exception as exc:
                self.ctx.run_on_ui_thread(self._set_status, f"Open failed: {exc}")
                return
            self.ctx.run_on_ui_thread(self._materialize, ws, open_after)
        threading.Thread(target=worker, daemon=True).start()

    def _recreate_async(self, ws_view):
        """A terminal workspace can't be restarted, but it remembers its boxes'
        recipes — provision a fresh workspace from the same recipe(s)."""
        self._set_status(f"Recreating {ws_view['title']}…")

        def worker():
            c = self._client()
            try:
                old = c.workspace(ws_view["uuid"]) or {}
                specs = self._recreate_specs(old, ws_view["title"])
                if not specs:
                    self.ctx.run_on_ui_thread(
                        self._set_status,
                        f"{ws_view['title']}: can't recreate — no recipe on its boxes")
                    return
                title = f"{ws_view['title']}-new"
                body = {"title": title,
                        "duration": old.get("duration") or 1,
                        "duration_unit": old.get("duration_unit") or "hours",
                        "boxes": specs,
                        "settings": {"public_ip_requested": True}}
                ws = c.create_workspace(body)
                c.start(ws["uuid"])
                ws = self._poll_active(c, ws["uuid"])
            except EasyEnvError as exc:
                logger.warning("easyenv recreate %s failed: %s", ws_view["title"], exc)
                self.ctx.run_on_ui_thread(
                    self._set_status, f"Recreate failed: {_friendly(exc)}")
                return
            except Exception as exc:
                self.ctx.run_on_ui_thread(self._set_status, f"Recreate failed: {exc}")
                return
            self.ctx.run_on_ui_thread(self._after_recreate, ws)
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _recreate_specs(old, fallback_title):
        """Build a create-body ``boxes`` list from a (terminal) workspace's
        existing boxes, reusing each box's recipe uuid. Boxes without a recipe
        (e.g. legacy mesh-only ones) are skipped."""
        specs = []
        for i, b in enumerate(old.get("boxes") or []):
            rid = (b.get("recipe") or {}).get("uuid")
            if not rid:
                continue
            specs.append({"title": b.get("title") or fallback_title,
                          "recipe": rid, "position": i})
        return specs

    def _after_recreate(self, ws):
        self._materialize(ws, open_after=True)
        self._refresh_async()

    def _do_action_async(self, verb, ws_view):
        self._set_status(f"{verb} {ws_view['title']}…")

        def worker():
            c = self._client()
            msg = None
            try:
                if verb == "start":
                    c.start(ws_view["uuid"])
                elif verb == "stop":
                    c.stop(ws_view["uuid"])
                elif verb == "delete":
                    c.delete(ws_view["uuid"])
            except EasyEnvError as exc:
                # Expected API rejections (e.g. starting a terminal workspace) —
                # no traceback, just a clear message.
                logger.warning("easyenv %s %s failed: %s", verb, ws_view["title"], exc)
                msg = _friendly(exc)
            except Exception as exc:
                logger.exception("easyenv %s failed", verb)
                msg = str(exc)
            self.ctx.run_on_ui_thread(self._after_action, verb, ws_view, msg)
        threading.Thread(target=worker, daemon=True).start()

    def _after_action(self, verb, ws_view, msg):
        if msg:
            self._set_status(f"{verb} {ws_view['title']}: {msg}")
        else:
            self._set_status(f"{verb} {ws_view['title']}: ok")
        self._refresh_async()

    # --- UI ----------------------------------------------------------
    def _refresh_async(self):
        def worker():
            account = self._account() if self._token() else None
            items = self._do_list() if (self._token() and account) else []
            self.ctx.run_on_ui_thread(self._render, account, items)
        threading.Thread(target=worker, daemon=True).start()

    def _refresh_recipes_async(self):
        def worker():
            try:
                recipes = self._do_recipes()
            except Exception:
                logger.exception("easyenv recipes failed")
                recipes = []
            self.ctx.run_on_ui_thread(self._populate_recipes, recipes)
        threading.Thread(target=worker, daemon=True).start()

    def _populate_recipes(self, recipes):
        self._recipe_values = [r["id"] for r in recipes if r.get("id")]
        labels = [r["name"] for r in recipes] or ["(sign in to load recipes)"]
        if self._recipe_dropdown is not None:
            try:
                self._recipe_dropdown.set_model(Gtk.StringList.new(labels))
            except Exception:
                logger.debug("could not update recipe dropdown")

    def _build_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(box, m)(18)

        title = Gtk.Label(label="EasyEnv Workspaces")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        self._auth_label = Gtk.Label(label="Checking sign-in…")
        self._auth_label.set_halign(Gtk.Align.START)
        box.append(self._auth_label)

        login_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._token_entry = Gtk.Entry()
        self._token_entry.set_visibility(False)
        self._token_entry.set_placeholder_text("paste service token")
        self._token_entry.set_hexpand(True)
        get_token = Gtk.LinkButton.new_with_label(DASHBOARD_URL, "Get token")
        signin = Gtk.Button(label="Sign in")
        signin.connect("clicked", self._on_signin_clicked)
        login_row.append(self._token_entry)
        login_row.append(get_token)
        login_row.append(signin)
        box.append(login_row)

        create_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._name_entry = Gtk.Entry()
        self._name_entry.set_placeholder_text("workspace name")
        self._name_entry.set_hexpand(True)
        self._recipe_dropdown = Gtk.DropDown.new_from_strings(["(sign in to load recipes)"])
        nodes_label = Gtk.Label(label="nodes")
        nodes_label.add_css_class("dim-label")
        self._nodes_spin = Gtk.SpinButton.new_with_range(1, 16, 1)
        self._nodes_spin.set_value(1)
        self._nodes_spin.set_tooltip_text(
            "More than one node creates a sidebar group of per-node connections")
        create_btn = Gtk.Button(label="Create")
        create_btn.add_css_class("suggested-action")
        create_btn.connect("clicked", self._on_create_clicked)
        create_row.append(self._name_entry)
        create_row.append(self._recipe_dropdown)
        create_row.append(nodes_label)
        create_row.append(self._nodes_spin)
        create_row.append(create_btn)
        box.append(create_row)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.set_halign(Gtk.Align.START)
        refresh_btn.connect("clicked", lambda _b: (self._refresh_recipes_async(), self._refresh_async()))
        box.append(refresh_btn)

        self._list_box = Gtk.ListBox()
        self._list_box.add_css_class("boxed-list")
        box.append(self._list_box)

        self._status_label = Gtk.Label(label="")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        self._refresh_recipes_async()
        self._refresh_async()
        return box

    def _set_status(self, text):
        if self._status_label is not None:
            self._status_label.set_text(text)

    def _render(self, account, items):
        if self._auth_label is not None:
            self._auth_label.set_text(
                f"Signed in as {account}" if account
                else "Not signed in — paste a service token below")
        if self._list_box is None:
            return
        child = self._list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list_box.remove(child)
            child = nxt
        for ws in items:
            self._list_box.append(self._workspace_row(ws))

    @staticmethod
    def _row_actions(status):
        """Offer only the actions that make sense for the current state: a
        terminal (stopped/expired) workspace can't be opened or started, only
        recreated or deleted."""
        if _is_terminal(status):
            return (("Recreate", "recreate"), ("Delete", "delete"))
        if _is_running(status):
            return (("Open", "open"), ("Stop", "stop"), ("Delete", "delete"))
        return (("Start", "start"), ("Delete", "delete"))  # transitional

    def _workspace_row(self, ws):
        row = Gtk.ListBoxRow()
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for m in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(hb, m)(6)
        label = Gtk.Label(label=f"{ws['title']}  ·  {ws['status']}")
        label.set_halign(Gtk.Align.START)
        label.set_hexpand(True)
        hb.append(label)
        for text, verb in self._row_actions(ws["status"]):
            btn = Gtk.Button(label=text)
            if verb == "stop":
                btn.set_tooltip_text(
                    "Ends the workspace; it can't be restarted afterwards")
            btn.connect("clicked", self._on_row_action, verb, ws)
            hb.append(btn)
        row.set_child(hb)
        return row

    def _on_row_action(self, _btn, verb, ws):
        if verb == "open":
            self._set_status(f"Opening {ws['title']}…")
            self._open_workspace_async(ws, open_after=True)
            return
        if verb == "recreate":
            self._recreate_async(ws)
            return
        self._do_action_async(verb, ws)


def _recipe_view(r):
    """Normalize a recipe object -> {id, name} (id = uuid for create)."""
    if not isinstance(r, dict):
        return None
    rid = str(r.get("uuid") or r.get("id") or "")
    name = str(r.get("title") or r.get("name") or rid)
    return {"id": rid, "name": name} if rid else None
