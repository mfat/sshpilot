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

import hashlib
import ipaddress
import json
import logging
import threading
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

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


# The workspace API (Status7f5Enum) emits only: active, not_started, stopped,
# in_progress, failed. 'stopped' is terminal — the VM is gone and it can't be
# restarted — so show it as "Terminated". Everything else is title-cased from
# the raw value.
_STATUS_LABELS = {"stopped": "Terminated"}


def _display_status(status):
    key = str(status or "").lower()
    return _STATUS_LABELS.get(
        key, (str(status or "unknown")).replace("_", " ").title())


# --- time / detail formatting (for the per-workspace details dialog) -------

def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _humanize_delta(seconds):
    """A coarse '1m 33s' / '1h' / '2d 3h' rendering of a duration in seconds."""
    seconds = int(abs(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _relative(iso, now):
    """'7 hours ago' / 'in 5m' / 'just now' for an ISO timestamp."""
    dt = _parse_iso(iso)
    if dt is None:
        return None
    delta = (now - dt).total_seconds()
    if -60 < delta < 60:
        return "just now"
    human = _humanize_delta(delta)
    return f"{human} ago" if delta >= 0 else f"in {human}"


def _span(a_iso, b_iso):
    a, b = _parse_iso(a_iso), _parse_iso(b_iso)
    if a is None or b is None:
        return None
    return _humanize_delta((b - a).total_seconds())


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


# --- styling (scoped, theme-aware) ----------------------------------------
# Only .ee-* classes; status pills tint their background from the libadwaita
# semantic text colour (.success/.warning/.error) via alpha(currentColor,…) so
# the page follows the user's light/dark theme. Recipe-avatar palette is fixed.
_AVATAR_PALETTE = ("#e95420", "#3776ab", "#3c873a", "#76b900",
                   "#336791", "#00add8", "#8250df", "#d83b01")
_CARD_CSS = """
.ee-pill { border-radius: 99px; padding: 3px 11px 3px 9px; font-weight: 700;
           background-color: alpha(currentColor, 0.15); }
.ee-dot { border-radius: 99px; background-color: currentColor;
          min-width: 7px; min-height: 7px; }
.ee-terminated { color: #a07be0; }
.ee-mono { font-family: monospace; }
.ee-avatar { border-radius: 7px; color: #ffffff; font-weight: 700;
             min-width: 22px; min-height: 22px; }
.ee-c0{background:#e95420;} .ee-c1{background:#3776ab;} .ee-c2{background:#3c873a;}
.ee-c3{background:#76b900;} .ee-c4{background:#336791;} .ee-c5{background:#00add8;}
.ee-c6{background:#8250df;} .ee-c7{background:#d83b01;}
.ee-logo { border-radius: 16px; background: @accent_bg_color; color: #ffffff; }
.ee-card { transition: box-shadow 160ms ease, border-color 160ms ease; }
.ee-card:hover { box-shadow: 0 10px 26px -14px rgba(0,0,0,0.65); }
.ee-edge-running { border-left: 4px solid @success_color; }
.ee-edge-provisioning { border-left: 4px solid @warning_color; }
.ee-edge-failed { border-left: 4px solid @error_color; }
.ee-edge-terminated { border-left: 4px solid #a07be0; }
.ee-edge-dimmed { border-left: 4px solid alpha(@window_fg_color, 0.25); }
"""

# status colour class -> card left-accent class
_EDGE_CLASS = {"success": "ee-edge-running", "warning": "ee-edge-provisioning",
               "error": "ee-edge-failed", "ee-terminated": "ee-edge-terminated",
               "dimmed": "ee-edge-dimmed"}
_css_loaded = False


def _ensure_css():
    global _css_loaded
    if _css_loaded:
        return
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(_CARD_CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _css_loaded = True
    except Exception:
        logger.debug("could not install EasyEnv CSS", exc_info=True)


def _status_meta(status):
    """(display label, css colour class) for a workspace status. Reconciles the
    design taxonomy with the real API: 'stopped' is terminal -> Terminated."""
    s = str(status or "").lower()
    if _is_running(status):
        return ("Running", "success")
    if s in ("in_progress", "not_started", "provisioning", "starting", "pending"):
        return ("Provisioning", "warning")
    if s == "failed":
        return ("Failed", "error")
    if _is_terminal(status):
        return ("Terminated", "ee-terminated")
    return (_display_status(status), "dimmed")


def _recipe_color_class(name):
    h = int(hashlib.md5((name or "").encode("utf-8")).hexdigest(), 16)
    return "ee-c%d" % (h % len(_AVATAR_PALETTE))


def _account_view(a):
    """Pull the header fields out of a /v1/accounts/ item."""
    if not isinstance(a, dict):
        return {}
    email = a.get("title") or (a.get("owner") or {}).get("email") or ""
    plan = (((a.get("current_plan") or {}).get("plan") or {}).get("abbreviation")
            or (a.get("type") or "").title() or "")
    rt = (a.get("current_plan") or {}).get("remaining_time_seconds")
    hours = int(rt // 3600) if isinstance(rt, (int, float)) else None
    local = (email.split("@")[0] if email else "") or "ee"
    return {"email": email, "plan": plan, "hours": hours,
            "initials": local[:2].upper(), "type": (a.get("type") or "").title()}


def _duration_mult(unit):
    return {"minutes": 60, "hours": 3600, "days": 86400}.get(
        str(unit or "hours").lower(), 3600)


def _remaining_seconds(ws, now):
    """Seconds left for a running workspace, from start_time + duration."""
    start = _parse_iso(ws.get("start_time"))
    if start is None:
        return None
    dur = ws.get("duration") or 0
    end = start.timestamp() + dur * _duration_mult(ws.get("duration_unit"))
    return int(end - now.timestamp())


def _visible_cards(cards, search="", filt="all", sort="name"):
    """Apply search / status-filter / sort to card-view dicts (pure)."""
    q = (search or "").strip().lower()
    out = [c for c in cards
           if not q or q in c["title"].lower() or q in c["recipe_name"].lower()]
    if filt and filt != "all":
        out = [c for c in out if c["status_label"].lower() == filt.lower()]
    if sort == "newest":
        out = sorted(out, key=lambda c: c.get("created_at", ""), reverse=True)
    elif sort == "status":
        out = sorted(out, key=lambda c: (c["status_label"], c["title"].lower()))
    else:
        out = sorted(out, key=lambda c: c["title"].lower())
    return out


# Create-dialog duration choices -> (label, n, unit)
_DURATIONS = (("1 hour", 1, "hours"), ("3 hours", 3, "hours"),
              ("8 hours", 8, "hours"), ("24 hours", 24, "hours"))


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        # widgets / state
        self._stack = None            # gate <-> dashboard
        self._token_entry = None
        self._signin_btn = None
        self._signin_btn_label = None
        self._signin_spinner = None
        self._status_label = None
        self._flowbox = None
        self._empty = None
        self._search_entry = None
        self._filter_dd = None
        self._sort_dd = None
        self._count_label = None
        self._account_box = None
        self._cards = []              # current card-view dicts
        self._account_info = None
        self._recipes = []            # [{id,name}]
        self._recipe_values = []
        self._recipe_names = []
        self._timer_labels = []       # (Gtk.Label, card_view) for the 1s tick
        self._prov_bars = []          # provisioning Gtk.ProgressBars to pulse
        self._tick_id = None
        self._tick_n = 0
        self._details_window = None

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
            self.ctx.ui.notify("Paste a service token first")
            return
        self._set_signing(True)

        def worker():
            try:
                self.ctx.secrets.set("service_token", token)
                accounts = EasyEnvClient(token, None, self._base_url()).accounts()
            except Exception as exc:
                self.ctx.run_on_ui_thread(self._signin_failed, str(exc))
                return
            self.ctx.run_on_ui_thread(self._after_signin, accounts)

        threading.Thread(target=worker, daemon=True).start()

    def _signin_failed(self, msg):
        self._set_signing(False)
        self.ctx.ui.notify(f"Sign-in failed: {msg}")

    def _after_signin(self, accounts):
        self._set_signing(False)
        if not accounts:
            self.ctx.ui.notify("No accounts for this token")
            return
        # Auto-select when there's only one; otherwise keep the first (a fuller
        # picker is straightforward to add, but most tokens map to one account).
        acct = accounts[0]
        self.ctx.settings.set("account_uuid", acct.get("uuid"))
        if self._token_entry is not None:
            self._token_entry.set_text("")
        self.ctx.ui.notify(f"Signed in as {acct.get('title')}")
        self._refresh_recipes_async()
        self._refresh_async()

    def _set_signing(self, on):
        if self._signin_btn_label is not None:
            self._signin_btn_label.set_text("Signing in…" if on else "Sign in")
        if self._signin_btn is not None:
            self._signin_btn.set_sensitive(not on)
        if self._signin_spinner is not None:
            self._signin_spinner.set_visible(on)
            (self._signin_spinner.start if on else self._signin_spinner.stop)()

    # --- REST operations (blocking; call from a worker) --------------
    def _do_recipes(self):
        c = self._client()
        if c is None:
            return []
        return [r for r in (_recipe_view(x) for x in c.recipes()) if r]

    def _do_account(self):
        c = self._client()
        if c is None:
            return None
        accts = c.accounts()
        want = self._account()
        a = next((x for x in accts if x.get("uuid") == want),
                 (accts[0] if accts else None))
        return _account_view(a) if a else None

    @staticmethod
    def _card_view(ws, now=None):
        """Flatten a workspace object into the fields a card needs (pure)."""
        if now is None:
            now = datetime.now(timezone.utc)
        boxes = ws.get("boxes") or []
        status = ws.get("status")
        label, cls = _status_meta(status)
        rec = (boxes[0].get("recipe") if boxes else None) or {}
        recipe_name = rec.get("title") or rec.get("uuid") or "—"
        nodes = len(boxes) or 1
        creator = ws.get("creator") or {}
        owner = (f"{creator.get('first_name', '')} {creator.get('last_name', '')}".strip()
                 or creator.get("email") or "you")
        ago = _relative(ws.get("start_time") or ws.get("created_at"), now) or ""
        box0 = boxes[0] if boxes else {}
        ip = box0.get("host_address")
        user = box0.get("ssh_username") or "root"
        port = box0.get("ssh_port") or 22
        ssh_line = f"{user}@{ip}:{port}" if (ip and _looks_routable(ip)) else ""
        running = _is_running(status)
        is_prov = label == "Provisioning"
        if running:
            secs = _remaining_seconds(ws, now)
            timer = (_humanize_delta(secs) + " left") if (secs and secs > 0) else "expiring"
        elif is_prov:
            timer = "Provisioning…"
        else:
            timer = _span(ws.get("start_time"), ws.get("stop_time")) or "—"
        node_text = f"{nodes} nodes" if nodes > 1 else "1 node"
        meta = f"{node_text} · {recipe_name} · by {owner}" + (f" · {ago}" if ago else "")
        return {
            "uuid": str(ws.get("uuid") or ""), "title": ws.get("title") or "",
            "status": status, "status_label": label, "status_class": cls,
            "recipe_name": recipe_name, "nodes": nodes, "node_text": node_text,
            "owner": owner, "ago": ago, "meta_line": meta,
            "ssh_line": ssh_line, "ip": ip, "user": user, "port": port,
            "timer": timer, "running": running, "is_provisioning": is_prov,
            "terminal": _is_terminal(status),
            "created_at": ws.get("created_at") or "",
            "start_time": ws.get("start_time") or "", "raw": ws,
        }

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
    @staticmethod
    def _create_body(name, recipe, nodes, dur_n=1, dur_unit="hours"):
        """Build the POST /v1/workspaces/ body (pure, testable)."""
        nodes = max(1, int(nodes))
        if nodes == 1:
            specs = [{"title": name, "recipe": recipe, "position": 0}]
        else:
            specs = [{"title": f"{name}-{i + 1}", "recipe": recipe, "position": i}
                     for i in range(nodes)]
        return {"title": name, "duration": dur_n, "duration_unit": dur_unit,
                "boxes": specs, "settings": {"public_ip_requested": True}}

    def _create_async(self, name, recipe, nodes, dur_n, dur_unit):
        self.ctx.ui.notify(f"Provisioning {name}…")
        body = self._create_body(name, recipe, nodes, dur_n, dur_unit)

        def worker():
            c = self._client()
            try:
                ws = c.create_workspace(body)
                c.start(ws["uuid"])
            except Exception as exc:
                self.ctx.run_on_ui_thread(self.ctx.ui.notify, f"Create failed: {exc}")
                return
            # Show the Provisioning card immediately; the tick polls it to Running.
            self.ctx.run_on_ui_thread(self._refresh_async)
        threading.Thread(target=worker, daemon=True).start()

    def _after_create(self, ws):
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

    def _clone_async(self, ws_view):
        """Provision a copy of a workspace from the same recipe(s)."""
        self.ctx.ui.notify(f"Cloning {ws_view['title']}…")

        def worker():
            c = self._client()
            try:
                old = c.workspace(ws_view["uuid"]) or {}
                specs = self._recreate_specs(old, ws_view["title"])
                if not specs:
                    self.ctx.run_on_ui_thread(
                        self.ctx.ui.notify,
                        f"{ws_view['title']}: can't clone — no recipe on its boxes")
                    return
                body = {"title": f"{ws_view['title']}-clone",
                        "duration": old.get("duration") or 1,
                        "duration_unit": old.get("duration_unit") or "hours",
                        "boxes": specs, "settings": {"public_ip_requested": True}}
                ws = c.create_workspace(body)
                c.start(ws["uuid"])
            except Exception as exc:
                self.ctx.run_on_ui_thread(self.ctx.ui.notify, f"Clone failed: {exc}")
                return
            self.ctx.run_on_ui_thread(self._on_clone_done, ws_view["title"])
        threading.Thread(target=worker, daemon=True).start()

    def _on_clone_done(self, title):
        self.ctx.ui.notify(f"Cloned {title}")
        self._refresh_async()

    def _copy_ssh(self, cv, widget):
        if not cv.get("ip"):
            return
        cmd = f"ssh {cv['user']}@{cv['ip']} -p {cv['port']}"
        try:
            widget.get_clipboard().set(cmd)
        except Exception:
            logger.debug("clipboard set failed", exc_info=True)
        self.ctx.ui.notify(f"Copied: {cmd}")

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
            if not (self._token() and self._account()):
                self.ctx.run_on_ui_thread(self._render, None, [])
                return
            c = self._client()
            account = None
            try:
                account = self._do_account()
            except Exception:
                logger.exception("easyenv account fetch failed")
            cards = []
            try:
                now = datetime.now(timezone.utc)
                wss = [w for w in c.workspaces() if w.get("uuid")]
                # The list omits host_address; fill it for running workspaces
                # (the plan caps parallel running at ~2, so this is bounded).
                for w in wss:
                    b0 = (w.get("boxes") or [{}])[0]
                    if _is_running(w.get("status")) and not b0.get("host_address"):
                        try:
                            full = c.workspace(w.get("uuid"))
                            if full and full.get("boxes"):
                                w["boxes"] = full["boxes"]
                        except Exception:
                            logger.debug("host fill failed for %s", w.get("uuid"))
                cards = [self._card_view(w, now) for w in wss]
            except Exception:
                logger.exception("easyenv workspace list failed")
            self.ctx.run_on_ui_thread(self._render, account, cards)
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
        self._recipes = [r for r in recipes if r.get("id")]
        self._recipe_values = [r["id"] for r in self._recipes]
        self._recipe_names = [r["name"] for r in self._recipes]

    def _build_page(self):
        _ensure_css()
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._build_gate(), "gate")
        self._stack.add_named(self._build_dashboard(), "dashboard")
        self._stack.set_visible_child_name("gate")
        self._stack.connect("unrealize", lambda *_a: self._stop_tick())
        self._refresh_recipes_async()
        self._refresh_async()
        return self._stack

    # --- sign-in gate ------------------------------------------------
    def _build_gate(self):
        clamp = Adw.Clamp(maximum_size=430)
        clamp.set_valign(Gtk.Align.CENTER)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        col.set_halign(Gtk.Align.CENTER)
        for m in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(col, m)(40)

        logo = Gtk.Image.new_from_icon_name("network-server-symbolic")
        logo.set_pixel_size(28)
        logo.add_css_class("ee-logo")
        logo.set_size_request(58, 58)
        logo.set_halign(Gtk.Align.CENTER)
        col.append(logo)

        h1 = Gtk.Label(label="Connect your EasyEnv account")
        h1.add_css_class("title-1")
        h1.set_margin_top(12)
        h1.set_wrap(True)
        h1.set_justify(Gtk.Justification.CENTER)
        col.append(h1)

        sub = Gtk.Label(label="Provision dev workspaces on easyenv.io and open them "
                              "as native SSH connections — right from sshPilot.")
        sub.add_css_class("dim-label")
        sub.set_wrap(True)
        sub.set_justify(Gtk.Justification.CENTER)
        sub.set_max_width_chars(46)
        sub.set_margin_bottom(8)
        col.append(sub)

        lbl = Gtk.Label(label="Service token")
        lbl.add_css_class("caption-heading")
        lbl.set_halign(Gtk.Align.START)
        col.append(lbl)

        self._token_entry = Gtk.PasswordEntry()
        self._token_entry.set_show_peek_icon(True)
        self._token_entry.set_property("placeholder-text", "ee_sk_…")
        self._token_entry.set_hexpand(True)
        self._token_entry.connect("activate", self._on_signin_clicked)
        col.append(self._token_entry)

        note = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        note.append(Gtk.Image.new_from_icon_name("channel-secure-symbolic"))
        note_lbl = Gtk.Label(label="Stored only in your OS keyring — never written to disk or logs.")
        note_lbl.add_css_class("dim-label")
        note_lbl.add_css_class("caption")
        note_lbl.set_wrap(True)
        note.append(note_lbl)
        col.append(note)

        self._signin_btn = Gtk.Button()
        self._signin_btn.add_css_class("suggested-action")
        self._signin_btn.add_css_class("pill")
        self._signin_btn.set_margin_top(8)
        sb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sb.set_halign(Gtk.Align.CENTER)
        self._signin_spinner = Gtk.Spinner()
        self._signin_spinner.set_visible(False)
        self._signin_btn_label = Gtk.Label(label="Sign in")
        sb.append(self._signin_spinner)
        sb.append(self._signin_btn_label)
        self._signin_btn.set_child(sb)
        self._signin_btn.connect("clicked", self._on_signin_clicked)
        col.append(self._signin_btn)

        link = Gtk.LinkButton.new_with_label(
            DASHBOARD_URL, "Get a token from the EasyEnv dashboard ↗")
        link.set_halign(Gtk.Align.CENTER)
        col.append(link)

        clamp.set_child(col)
        return clamp

    # --- dashboard ---------------------------------------------------
    def _build_dashboard(self):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        clamp = Adw.Clamp(maximum_size=1100)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for m in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(body, m)(24)

        # header: title + account row | New Workspace
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_hexpand(True)
        h1 = Gtk.Label(label="All workspaces")
        h1.add_css_class("title-1")
        h1.set_halign(Gtk.Align.START)
        left.append(h1)
        self._account_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._account_box.set_halign(Gtk.Align.START)
        left.append(self._account_box)
        header.append(left)
        new_btn = Gtk.Button()
        new_btn.add_css_class("suggested-action")
        new_btn.add_css_class("pill")
        new_btn.set_valign(Gtk.Align.CENTER)
        nb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        nb.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        nb.append(Gtk.Label(label="New Workspace"))
        new_btn.set_child(nb)
        new_btn.connect("clicked", lambda _b: self._open_create_dialog())
        header.append(new_btn)
        body.append(header)

        # toolbar: search / filter / sort / count
        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search workspaces…")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", lambda _e: self._repopulate())
        tb.append(self._search_entry)
        self._filter_dd = Gtk.DropDown.new_from_strings(
            ["All statuses", "Running", "Provisioning", "Terminated"])
        self._filter_dd.connect("notify::selected", lambda *_a: self._repopulate())
        tb.append(self._filter_dd)
        self._sort_dd = Gtk.DropDown.new_from_strings(["Name", "Newest", "Status"])
        self._sort_dd.connect("notify::selected", lambda *_a: self._repopulate())
        tb.append(self._sort_dd)
        self._count_label = Gtk.Label(label="")
        self._count_label.add_css_class("dim-label")
        tb.append(self._count_label)
        body.append(tb)

        # card grid — fixed-width cards that pack from the left (not stretched
        # to fill the row when there are only a few).
        self._flowbox = Gtk.FlowBox()
        self._flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flowbox.set_homogeneous(False)
        self._flowbox.set_column_spacing(16)
        self._flowbox.set_row_spacing(16)
        self._flowbox.set_min_children_per_line(1)
        self._flowbox.set_max_children_per_line(4)
        self._flowbox.set_halign(Gtk.Align.START)
        self._flowbox.set_valign(Gtk.Align.START)
        body.append(self._flowbox)

        self._empty = self._build_empty()
        self._empty.set_visible(False)
        body.append(self._empty)

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("dim-label")
        self._status_label.add_css_class("caption")
        self._status_label.set_halign(Gtk.Align.START)
        body.append(self._status_label)

        clamp.set_child(body)
        scroller.set_child(clamp)
        return scroller

    def _build_empty(self):
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_halign(Gtk.Align.CENTER)
        b.set_margin_top(70)
        b.set_margin_bottom(70)
        img = Gtk.Image.new_from_icon_name("network-server-symbolic")
        img.set_pixel_size(44)
        img.add_css_class("dim-label")
        t = Gtk.Label(label="No workspaces match")
        t.add_css_class("title-4")
        s = Gtk.Label(label="Try a different search, or spin up a new workspace.")
        s.add_css_class("dim-label")
        b.append(img)
        b.append(t)
        b.append(s)
        return b

    def _set_status(self, text):
        if self._status_label is not None:
            self._status_label.set_text(text)

    def _render(self, account, cards):
        self._cards = cards or []
        self._account_info = account
        signed = bool(self._token() and self._account())
        if self._stack is not None:
            self._stack.set_visible_child_name("dashboard" if signed else "gate")
        self._update_account_header(account)
        self._repopulate()
        self._ensure_tick()

    def _update_account_header(self, account):
        box = self._account_box
        if box is None:
            return
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt
        if not account:
            return
        av = Gtk.Label(label=account.get("initials") or "EE")
        av.add_css_class("ee-avatar")
        av.add_css_class("ee-c1")
        av.set_size_request(30, 30)
        box.append(av)
        info = Gtk.Label(label=f"{account.get('email', '')} · "
                               f"{account.get('type') or account.get('plan') or ''}")
        info.add_css_class("dim-label")
        box.append(info)
        if account.get("plan"):
            badge = Gtk.Label(label=str(account["plan"]).upper())
            badge.add_css_class("ee-pill")
            badge.add_css_class("warning")
            box.append(badge)
        if account.get("hours") is not None:
            hrs = Gtk.Label(label=f"{account['hours']} h left")
            hrs.add_css_class("ee-pill")
            hrs.add_css_class("accent")
            box.append(hrs)

    def _filter_value(self):
        idx = self._filter_dd.get_selected() if self._filter_dd else 0
        opts = ("all", "running", "provisioning", "terminated")
        return opts[idx] if 0 <= idx < len(opts) else "all"

    def _sort_value(self):
        idx = self._sort_dd.get_selected() if self._sort_dd else 0
        opts = ("name", "newest", "status")
        return opts[idx] if 0 <= idx < len(opts) else "name"

    def _repopulate(self):
        if self._flowbox is None:
            return
        child = self._flowbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._flowbox.remove(child)
            child = nxt
        self._timer_labels = []
        self._prov_bars = []
        search = self._search_entry.get_text() if self._search_entry else ""
        vis = _visible_cards(self._cards, search, self._filter_value(), self._sort_value())
        if self._count_label is not None:
            self._count_label.set_text(
                f"{len(vis)} workspace" + ("" if len(vis) == 1 else "s"))
        self._flowbox.set_visible(bool(vis))
        if self._empty is not None:
            self._empty.set_visible(not vis)
        for cv in vis:
            self._flowbox.append(self._build_card(cv))

    # --- card --------------------------------------------------------
    @staticmethod
    def _status_pill(cv):
        cls = cv["status_class"]
        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        pill.add_css_class("ee-pill")
        pill.add_css_class(cls)
        pill.set_halign(Gtk.Align.START)
        pill.set_valign(Gtk.Align.CENTER)
        dot = Gtk.Box()
        dot.add_css_class("ee-dot")
        dot.add_css_class(cls)
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_size_request(7, 7)
        lbl = Gtk.Label(label=cv["status_label"])
        lbl.add_css_class(cls)
        pill.append(dot)
        pill.append(lbl)
        return pill

    @staticmethod
    def _avatar(name):
        a = Gtk.Label(label=(name[:1] or "?").upper())
        a.add_css_class("ee-avatar")
        a.add_css_class(_recipe_color_class(name))
        a.set_size_request(22, 22)
        return a

    @staticmethod
    def _icon_btn(icon, tip, cb, danger=False):
        b = Gtk.Button.new_from_icon_name(icon)
        b.add_css_class("flat")
        b.set_tooltip_text(tip)
        if danger:
            b.add_css_class("error")
        b.connect("clicked", lambda _b: cb())
        return b

    @staticmethod
    def _primary_btn(label, cb):
        b = Gtk.Button(label=label)
        b.add_css_class("suggested-action")
        if cb is not None:
            b.connect("clicked", lambda _b: cb())
        return b

    def _build_card(self, cv):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("card")
        card.add_css_class("ee-card")
        card.add_css_class(_EDGE_CLASS.get(cv["status_class"], "ee-edge-dimmed"))
        # Fixed width so cards form a tidy grid and pack left instead of
        # stretching to the full row width when there are only a few.
        card.set_size_request(300, -1)
        card.set_hexpand(False)
        card.set_halign(Gtk.Align.START)
        card.set_valign(Gtk.Align.START)
        # Content lives in an inner box whose margins create the internal padding
        # — margins on the .card box itself would sit OUTSIDE its border, and
        # .card has no padding of its own. Inter-card gaps come from FlowBox.
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_hexpand(True)
        for m in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(inner, m)(16)
        ws_view = {"uuid": cv["uuid"], "title": cv["title"], "status": cv["status"]}

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.append(self._status_pill(cv))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        head.append(spacer)
        timer = Gtk.Label(label=cv["timer"])
        timer.add_css_class("dim-label")
        timer.add_css_class("caption")
        head.append(timer)
        inner.append(head)
        if cv["running"] or cv["is_provisioning"]:
            self._timer_labels.append((timer, cv))

        title = Gtk.Label(label=cv["title"])
        title.add_css_class("title-4")
        title.set_halign(Gtk.Align.START)
        title.set_xalign(0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(20)   # bound natural width so it can't widen the card
        inner.append(title)
        meta = Gtk.Label(label=cv["meta_line"])
        meta.add_css_class("dim-label")
        meta.add_css_class("caption")
        meta.set_halign(Gtk.Align.START)
        meta.set_xalign(0)
        meta.set_wrap(False)
        meta.set_ellipsize(Pango.EllipsizeMode.END)
        meta.set_max_width_chars(30)
        inner.append(meta)

        if cv["is_provisioning"]:
            bar = Gtk.ProgressBar()
            bar.set_fraction(0.25)
            inner.append(bar)
            self._prov_bars.append(bar)
            cap = Gtk.Label(label="Allocating public IP & booting box…")
            cap.add_css_class("dim-label")
            cap.add_css_class("caption")
            cap.set_halign(Gtk.Align.START)
            inner.append(cap)

        if cv["ssh_line"]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            sshl = Gtk.Label(label=cv["ssh_line"])
            sshl.add_css_class("ee-mono")
            sshl.add_css_class("caption")
            sshl.set_halign(Gtk.Align.START)
            sshl.set_xalign(0)
            sshl.set_hexpand(True)
            sshl.set_ellipsize(Pango.EllipsizeMode.END)
            sshl.set_max_width_chars(24)
            sshl.set_selectable(True)
            row.append(sshl)
            cp = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
            cp.add_css_class("flat")
            cp.set_tooltip_text("Copy SSH command")
            cp.connect("clicked", lambda _b, c=cv: self._copy_ssh(c, _b))
            row.append(cp)
            inner.append(row)

        inner.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        # Recipe is already shown in the meta line; here just a colour chip
        # (with the recipe in its tooltip) so the footer can't be widened by a
        # long recipe name.
        av = self._avatar(cv["recipe_name"])
        av.set_tooltip_text(cv["recipe_name"])
        av.set_valign(Gtk.Align.CENTER)
        footer.append(av)
        gap = Gtk.Box()
        gap.set_hexpand(True)
        footer.append(gap)

        info = Gtk.Button.new_from_icon_name("dialog-information-symbolic")
        info.add_css_class("flat")
        info.set_tooltip_text("Details")
        info.connect("clicked", lambda _b, v=ws_view: self._show_details_async(v, _b.get_root()))
        footer.append(info)

        if cv["terminal"]:
            footer.append(self._icon_btn("edit-copy-symbolic", "Clone",
                                         lambda v=ws_view: self._clone_async(v)))
            footer.append(self._icon_btn("user-trash-symbolic", "Delete",
                                         lambda v=ws_view: self._do_action_async("delete", v), danger=True))
            footer.append(self._primary_btn("Recreate", lambda v=ws_view: self._recreate_async(v)))
        elif cv["running"]:
            footer.append(self._icon_btn("media-playback-stop-symbolic", "Stop",
                                         lambda v=ws_view: self._do_action_async("stop", v)))
            footer.append(self._icon_btn("edit-copy-symbolic", "Clone",
                                         lambda v=ws_view: self._clone_async(v)))
            footer.append(self._icon_btn("user-trash-symbolic", "Delete",
                                         lambda v=ws_view: self._do_action_async("delete", v), danger=True))
            footer.append(self._primary_btn("Open", lambda v=ws_view: self._open_workspace_async(v, True)))
        elif cv["is_provisioning"]:
            footer.append(self._icon_btn("user-trash-symbolic", "Delete",
                                         lambda v=ws_view: self._do_action_async("delete", v), danger=True))
            disabled = self._primary_btn("Open", None)
            disabled.set_sensitive(False)
            footer.append(disabled)
        else:  # not_started / other transitional
            footer.append(self._icon_btn("edit-copy-symbolic", "Clone",
                                         lambda v=ws_view: self._clone_async(v)))
            footer.append(self._icon_btn("user-trash-symbolic", "Delete",
                                         lambda v=ws_view: self._do_action_async("delete", v), danger=True))
            footer.append(self._primary_btn("Start & open",
                                            lambda v=ws_view: self._open_workspace_async(v, True)))
        inner.append(footer)
        card.append(inner)
        return card

    # --- create dialog ----------------------------------------------
    def _open_create_dialog(self):
        dialog = Adw.Dialog()
        dialog.set_title("New workspace")
        dialog.set_content_width(460)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: dialog.close())
        create = Gtk.Button(label="Create & provision")
        create.add_css_class("suggested-action")
        hb.pack_start(cancel)
        hb.pack_end(create)
        tv.add_top_bar(hb)

        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup()
        name_row = Adw.EntryRow()
        name_row.set_title("Name")
        recipe_row = Adw.ComboRow()
        recipe_row.set_title("Recipe")
        recipe_row.set_model(Gtk.StringList.new(self._recipe_names or ["(sign in to load recipes)"]))
        nodes_row = Adw.SpinRow.new_with_range(1, 16, 1)
        nodes_row.set_title("Nodes")
        nodes_row.set_subtitle("More than one node creates a sidebar group")
        dur_row = Adw.ComboRow()
        dur_row.set_title("Duration")
        dur_row.set_model(Gtk.StringList.new([d[0] for d in _DURATIONS]))
        grp.add(name_row)
        grp.add(recipe_row)
        grp.add(nodes_row)
        grp.add(dur_row)
        page.add(grp)
        hint = Adw.PreferencesGroup()
        hint.set_description("Public IP is always requested so the box is reachable "
                             "over plain SSH; it auto-stops when the duration ends.")
        page.add(hint)
        tv.set_content(page)
        dialog.set_child(tv)

        def on_create(_b):
            name = name_row.get_text().strip()
            if not name:
                self.ctx.ui.notify("Name the workspace first")
                return
            ridx = recipe_row.get_selected()
            if not self._recipe_values or not (0 <= ridx < len(self._recipe_values)):
                self.ctx.ui.notify("Pick a recipe (sign in to load recipes)")
                return
            recipe = self._recipe_values[ridx]
            didx = dur_row.get_selected()
            dur = _DURATIONS[didx] if 0 <= didx < len(_DURATIONS) else _DURATIONS[0]
            dialog.close()
            self._create_async(name, recipe, int(nodes_row.get_value()), dur[1], dur[2])

        create.connect("clicked", on_create)
        dialog.present(self._stack)

    # --- live tick ---------------------------------------------------
    def _ensure_tick(self):
        dynamic = any(c["running"] or c["is_provisioning"] for c in self._cards)
        if dynamic and self._tick_id is None:
            self._tick_id = GLib.timeout_add_seconds(1, self._tick)
        elif not dynamic:
            self._stop_tick()

    def _stop_tick(self):
        if self._tick_id is not None:
            try:
                GLib.source_remove(self._tick_id)
            except Exception:
                pass
            self._tick_id = None

    def _tick(self):
        now = datetime.now(timezone.utc)
        for lbl, cv in self._timer_labels:
            if cv["running"]:
                secs = _remaining_seconds(cv["raw"], now)
                lbl.set_text((_humanize_delta(secs) + " left") if (secs and secs > 0) else "expiring")
        for bar in self._prov_bars:
            try:
                bar.pulse()
            except Exception:
                pass
        self._tick_n = (self._tick_n + 1) % 5
        if self._tick_n == 0 and any(c["is_provisioning"] for c in self._cards):
            self._refresh_async()
        return True

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

    @staticmethod
    def _detail_rows(ws, now=None):
        """Ordered (label, value) pairs mirroring the easyenv dashboard's
        workspace detail panel. Rows with no data are omitted. ``now`` is
        injectable for tests; defaults to current UTC."""
        if now is None:
            now = datetime.now(timezone.utc)
        rows = []

        def add(label, value):
            if value not in (None, "", []):
                rows.append((label, str(value)))

        started = ws.get("start_time")
        add("Started", _relative(started, now))
        add("Startup time", _span(ws.get("starting_at"), started))
        if ws.get("duration"):
            add("Total time", f"{ws.get('duration')} {ws.get('duration_unit') or ''}".strip())
        start_dt = _parse_iso(started)
        if start_dt is not None:
            end_dt = _parse_iso(ws.get("stop_time")) or now
            add("Used time", _humanize_delta((end_dt - start_dt).total_seconds()))
        add("Terminated", _relative(ws.get("stop_time"), now))
        add("Account", (ws.get("account") or {}).get("title"))
        add("Created", _relative(ws.get("created_at"), now))
        prov = ws.get("virtualization_backend")
        add("Provider", prov.title() if isinstance(prov, str) else prov)
        add("ID", ws.get("id"))
        add("UUID", ws.get("uuid"))
        return rows

    # --- details dialog ----------------------------------------------
    def _show_details_async(self, ws_view, parent):
        self._set_status(f"Loading {ws_view['title']}…")

        def worker():
            c = self._client()
            try:
                full = c.workspace(ws_view["uuid"]) or {}
            except Exception as exc:
                self.ctx.run_on_ui_thread(self._set_status, f"Details failed: {exc}")
                return
            self.ctx.run_on_ui_thread(self._present_details, full, parent)
        threading.Thread(target=worker, daemon=True).start()

    def _present_details(self, ws, parent):
        self._set_status("")
        win = Gtk.Window()
        win.set_title(ws.get("title") or "Workspace")
        win.set_modal(True)
        if parent is not None:
            win.set_transient_for(parent)
        win.set_default_size(420, -1)
        # keep a reference so it isn't garbage-collected while shown
        self._details_window = win

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            getattr(box, m)(18)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=ws.get("title") or "")
        title.add_css_class("title-3")
        title.set_halign(Gtk.Align.START)
        head.append(title)
        _slabel, _scls = _status_meta(ws.get("status"))
        badge = Gtk.Label(label=_slabel)
        badge.add_css_class("ee-pill")
        badge.add_css_class(_scls)
        head.append(badge)
        box.append(head)

        boxes = ws.get("boxes") or []
        prov = ws.get("virtualization_backend")
        prov = prov.title() if isinstance(prov, str) else (prov or "")
        sub = Gtk.Label(label=f"{len(boxes)} box{'es' if len(boxes) != 1 else ''}"
                        + (f"  ·  {prov}" if prov else ""))
        sub.add_css_class("dim-label")
        sub.set_halign(Gtk.Align.START)
        box.append(sub)

        progress = ws.get("progress")
        if isinstance(progress, (int, float)):
            usage = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            bar = Gtk.ProgressBar()
            bar.set_fraction(max(0.0, min(1.0, float(progress) / 100.0)))
            bar.set_hexpand(True)
            bar.set_valign(Gtk.Align.CENTER)
            usage.append(bar)
            usage.append(Gtk.Label(label=f"{int(progress)}%"))
            box.append(usage)

        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_column_spacing(18)
        for i, (label, value) in enumerate(self._detail_rows(ws)):
            key = Gtk.Label(label=label)
            key.add_css_class("dim-label")
            key.set_halign(Gtk.Align.START)
            val = Gtk.Label(label=value)
            val.set_halign(Gtk.Align.END)
            val.set_hexpand(True)
            val.set_selectable(True)
            grid.attach(key, 0, i, 1, 1)
            grid.attach(val, 1, i, 1, 1)
        box.append(grid)

        close = Gtk.Button(label="Close")
        close.add_css_class("suggested-action")
        close.set_halign(Gtk.Align.END)
        close.connect("clicked", lambda _b: win.close())
        box.append(close)

        win.set_child(box)
        win.present()


def _recipe_view(r):
    """Normalize a recipe object -> {id, name} (id = uuid for create)."""
    if not isinstance(r, dict):
        return None
    rid = str(r.get("uuid") or r.get("id") or "")
    name = str(r.get("title") or r.get("name") or rid)
    return {"id": rid, "name": name} if rid else None
