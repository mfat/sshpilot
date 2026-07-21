# askpass_utils.py
import atexit
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import threading
from gettext import gettext as _
from typing import List

# SSH key-path canonicalization lives in secret_storage (the single source of truth, shared with
# credential export). secret_storage is GTK-free and safe to import in the askpass subprocess.
from .secret_storage import (
    normalize_key_path_for_storage as _normalize_key_path_for_storage,
    key_path_lookup_candidates as _get_key_path_lookup_candidates,
)

# libsecret / keyring are imported lazily (and only for a diagnostic log line
# here) — the real passphrase lookups delegate to secret_storage. Keeping these
# off the module top-level avoids loading keyring on the startup import chain.
def _secret_available() -> bool:
    from .secret_storage import _get_secret
    return _get_secret() is not None


def _keyring_available() -> bool:
    from .secret_storage import _get_keyring
    return _get_keyring() is not None

try:
    from .platform_utils import is_macos
except ImportError:
    try:
        from platform_utils import is_macos
    except ImportError:
        def is_macos():
            return False

logger = logging.getLogger(__name__)

_ASKPASS_DIR = None
_ASKPASS_SCRIPT = None

# Address + auth token of the main app's in-process passphrase-prompt IPC server.
# Set by sshpilot.askpass_server.start() (main process) and advertised to SSH
# children via get_ssh_env_with_askpass(). Plain strings only — read from worker
# threads, so this must never import or touch GTK.
_ASKPASS_SOCKET = None
_ASKPASS_TOKEN = None

_SCHEMA = None


def set_askpass_ipc(socket_path: "str | None", token: "str | None") -> None:
    """Publish (or clear) the main app's passphrase-prompt IPC endpoint.

    Called by the main process once its Unix-socket server is listening so that
    every SSH child it spawns can be told where to route passphrase prompts.
    Pass ``(None, None)`` on shutdown to stop advertising it.
    """

    global _ASKPASS_SOCKET, _ASKPASS_TOKEN
    _ASKPASS_SOCKET = socket_path or None
    _ASKPASS_TOKEN = token or None

_ASKPASS_LOG_PATH = None
_ASKPASS_LOG_OFFSET = 0
_ASKPASS_LOG_INITIALIZED = False
_ASKPASS_LOG_THREAD = None
_ASKPASS_LOG_THREAD_STOP = threading.Event()
_ASKPASS_LOG_THREAD_LOCK = threading.Lock()
_ASKPASS_LOG_IO_LOCK = threading.Lock()


def _extract_key_path(prompt: str) -> str:
    """Extract key path from an SSH passphrase prompt string."""
    import re
    patterns = [
        r'passphrase.*for\s+key\s+["\']([^"\']*)["\']',
        r'passphrase.*for\s+["\']([^"\']*)["\']',
        r'passphrase.*for\s+key\s+([^\s:]+)',
        r'passphrase.*for\s+([^\s:]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, prompt, re.IGNORECASE)
        if match:
            key_path = match.group(1).strip()
            if (key_path.startswith('"') and key_path.endswith('"')) or \
               (key_path.startswith("'") and key_path.endswith("'")):
                key_path = key_path[1:-1]
            return key_path
    return ""


# Prompt classification for askpass (password vs passphrase vs OTP vs presence).
# Only the last non-empty line is matched so scrollback like
# "Permission denied (publickey,password)." cannot false-positive.
#
# OpenSSH FIDO presence usually goes to the TTY via notify_start() when stderr
# is a tty — askpass is only used with SSH_ASKPASS_PROMPT=none (no tty / agent).
_PRESENCE_MARKERS = (
    'confirm user presence',
    'tap your security key',
    'tap secure token',
    'touch your yubikey',
    'touch your security key',
    'touch your authenticator',
    'touch the authenticator',
)
_HOSTKEY_MARKERS = (
    '(yes/no',
    'continue connecting',
    "please type 'yes'",
)
# Word-boundary match: bare substrings would misfire on hostnames/usernames
# ("user@alpine's password:" contains 'pin', "scotp1" contains 'otp').
#
# OTP/MFA markers shared with the "…password" exemption below so prompts like
# "one time password" / "TOTP password" are never treated as login passwords
# (which would autofill the stored SSH password into an MFA challenge).
_OTP_MFA_MARKER_RE = re.compile(
    r'\b(?:pin|otp|totp|mfa|2fa|two[-\s]?factor|one[-\s]?time)\b'
)
_TYPED_INTERACTIVE_RE = re.compile(
    r'\b(?:pin|otp|totp|mfa|2fa|two[-\s]?factor|'
    r'verification code|one[-\s]?time|authentication code)\b'
)


def classify_prompt(text: str) -> "str | None":
    """Classify the trailing prompt in *text*.

    Returns ``'password'``, ``'passphrase'``, ``'presence'`` (FIDO touch —
    no typed secret), ``'interactive'`` (OTP/PIN/host-key — typed or yes/no),
    or ``None`` when the text does not look like a prompt.
    """
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1].lower()
    if any(marker in last for marker in _PRESENCE_MARKERS):
        return 'presence'
    if any(marker in last for marker in _HOSTKEY_MARKERS):
        return 'interactive'
    # PIN / OTP may omit a trailing colon ("Enter PIN for authenticator").
    if _TYPED_INTERACTIVE_RE.search(last) and (
            last.endswith(':') or last.startswith('enter ') or ' for ' in last):
        if 'passphrase' in last:
            return 'passphrase'
        # "…'s password:" for a host that legitimately contains an OTP/PIN
        # word is still a login password — but "one-time password",
        # "one time password", "TOTP password", etc. are MFA challenges.
        if 'password' in last and not _OTP_MFA_MARKER_RE.search(last):
            return 'password'
        return 'interactive'
    if last.endswith(':'):
        if 'passphrase' in last:
            return 'passphrase'
        # "Password:" but also PAM's "Password for user@host:".
        if 'password' in last:
            return 'password'
    return None


# One-shot in-memory passwords for the askpass helper (main process only).
# Keyed by a random id advertised as SSHPILOT_SESSION_PASSWORD_ID — the secret
# never leaves this process except over the askpass IPC socket.
_SESSION_PASSWORDS: "dict[str, tuple[str, float]]" = {}
_SESSION_PASSWORDS_LOCK = threading.Lock()
_SESSION_PASSWORD_TTL = 600.0


def _session_password_dir() -> "str | None":
    """Prefer XDG_RUNTIME_DIR (tmpfs, 0700, wiped at logout) over /tmp so a
    staged password never rests on real disk and can't outlive the session."""
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR')
    if runtime_dir and os.path.isdir(runtime_dir):
        return runtime_dir
    return None


def _sweep_stale_session_files(directory: "str | None") -> None:
    """Best-effort removal of staged password files nobody consumed (key auth
    succeeded, app crashed before atexit, …). Age-gated so concurrent connects
    can't delete each other's fresh files."""
    try:
        now = time.time()
        for name in os.listdir(directory or tempfile.gettempdir()):
            if not name.startswith('sshpilot-pw-'):
                continue
            path = os.path.join(directory or tempfile.gettempdir(), name)
            try:
                if now - os.path.getmtime(path) > _SESSION_PASSWORD_TTL:
                    os.unlink(path)
            except Exception:
                pass
    except Exception:
        pass


def _sweep_stale_session_password_ids() -> None:
    now = time.time()
    with _SESSION_PASSWORDS_LOCK:
        stale = [
            sid for sid, (_pw, created) in _SESSION_PASSWORDS.items()
            if now - created > _SESSION_PASSWORD_TTL
        ]
        for sid in stale:
            _SESSION_PASSWORDS.pop(sid, None)


def _register_session_password(password: str) -> str:
    """Hold *password* in-process; return a one-shot id for the SSH child env."""
    import secrets

    _sweep_stale_session_password_ids()
    sid = secrets.token_hex(16)
    with _SESSION_PASSWORDS_LOCK:
        _SESSION_PASSWORDS[sid] = (password, time.time())
    return sid


def take_session_password(password_id: str) -> str:
    """Consume a staged in-memory session password (one-shot). Main process only."""
    if not password_id:
        return ''
    with _SESSION_PASSWORDS_LOCK:
        item = _SESSION_PASSWORDS.pop(password_id, None)
    return item[0] if item else ''


def _stage_session_password_file(password: str) -> str:
    """Fallback: write *password* to a 0600 temp file for one askpass read.

    Used only when the askpass IPC server is not advertised (no main-window
    socket). Prefers XDG_RUNTIME_DIR.
    """
    if not password:
        return ''
    try:
        directory = _session_password_dir()
        _sweep_stale_session_files(directory)
        fd, path = tempfile.mkstemp(
            prefix='sshpilot-pw-', dir=directory, text=True)
        try:
            os.write(fd, password.encode('utf-8'))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)

        def _cleanup(p=path):
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass

        atexit.register(_cleanup)
        return path
    except Exception:
        logger.debug("_stage_session_password_file failed", exc_info=True)
        return ''


def stage_session_password(password: str) -> "dict[str, str]":
    """Stage an in-memory login password for one askpass consumption.

    Prefer the main-app askpass IPC (secret stays in process memory; the SSH
    child only gets ``SSHPILOT_SESSION_PASSWORD_ID``). Fall back to a 0600
    temp file under XDG_RUNTIME_DIR when the IPC server is not advertised.
    Returns env updates to merge into the SSH child (may be empty on failure).
    """
    if not password:
        return {}
    if _ASKPASS_SOCKET and _ASKPASS_TOKEN:
        return {"SSHPILOT_SESSION_PASSWORD_ID": _register_session_password(password)}
    path = _stage_session_password_file(password)
    return {"SSHPILOT_SESSION_PASSWORD_FILE": path} if path else {}


def lookup_ssh_password(host: str, username: str) -> str:
    """Look up a stored SSH login password via the selected secret backend."""
    if not host or not username:
        return ''
    try:
        from .secret_storage import get_secret_manager, password_spec
        return get_secret_manager().lookup(password_spec(host, username)) or ''
    except Exception:
        return ''


def get_secret_schema():
    """Return the shared Secret.Schema for stored secrets.

    Delegates to :func:`sshpilot.secret_storage.get_schema` so a single schema
    definition is shared by all backends and call sites.
    """
    from .secret_storage import get_schema
    return get_schema()


def get_askpass_log_path() -> str:
    """Return the path to the askpass log file."""

    global _ASKPASS_LOG_PATH
    if _ASKPASS_LOG_PATH is None:
        log_dir = (
            os.environ.get("SSHPILOT_ASKPASS_LOG_DIR")
            or os.environ.get("XDG_RUNTIME_DIR")
            or tempfile.gettempdir()
        )
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass
        _ASKPASS_LOG_PATH = os.path.join(log_dir, "sshpilot-askpass.log")
    return _ASKPASS_LOG_PATH


def read_new_askpass_log_lines(include_existing: bool = False) -> List[str]:
    """Read newly appended askpass log lines.

    Parameters
    ----------
    include_existing:
        When True, the first call returns existing content in the log file.
        When False, the first call skips any pre-existing content to avoid
        replaying old entries.
    """

    global _ASKPASS_LOG_OFFSET, _ASKPASS_LOG_INITIALIZED

    with _ASKPASS_LOG_IO_LOCK:
        path = get_askpass_log_path()

        try:
            size = os.path.getsize(path)
        except OSError:
            _ASKPASS_LOG_OFFSET = 0
            return []

        if not _ASKPASS_LOG_INITIALIZED:
            _ASKPASS_LOG_INITIALIZED = True
            if not include_existing:
                _ASKPASS_LOG_OFFSET = size
                return []

        if _ASKPASS_LOG_OFFSET > size:
            # File was truncated; restart from beginning
            _ASKPASS_LOG_OFFSET = 0

        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                handle.seek(_ASKPASS_LOG_OFFSET)
                data = handle.read()
                _ASKPASS_LOG_OFFSET = handle.tell()
        except OSError:
            return []

        if not data:
            return []

        return [line for line in data.splitlines() if line.strip()]


def forward_askpass_log_to_logger(log, include_existing: bool = False) -> None:
    """Forward askpass log lines into the main application logger."""

    try:
        lines = read_new_askpass_log_lines(include_existing=include_existing)
    except Exception as exc:
        log.debug(f"Unable to read askpass log: {exc}")
        return

    for line in lines:
        log.info(f"ASKPASS: {line}")


def _askpass_log_forwarder_loop() -> None:
    """Background loop that forwards askpass logs to the module logger.

    Only new lines are forwarded — the log file persists for the whole login
    session, so replaying existing content would dump unrelated history from
    earlier connections (and earlier app runs) into the console.
    """

    forward_askpass_log_to_logger(logger, include_existing=False)

    while not _ASKPASS_LOG_THREAD_STOP.wait(1.0):
        forward_askpass_log_to_logger(logger)


def ensure_askpass_log_forwarder() -> None:
    """Ensure a background thread is forwarding askpass logs to the logger."""

    global _ASKPASS_LOG_THREAD

    with _ASKPASS_LOG_THREAD_LOCK:
        if _ASKPASS_LOG_THREAD and _ASKPASS_LOG_THREAD.is_alive():
            return

        _ASKPASS_LOG_THREAD_STOP.clear()

        thread = threading.Thread(
            target=_askpass_log_forwarder_loop,
            name="AskpassLogForwarder",
            daemon=True,
        )
        try:
            thread.start()
            _ASKPASS_LOG_THREAD = thread
            logger.debug(f"Started askpass log forwarder thread (log: {get_askpass_log_path()})")
        except Exception as exc:
            logger.debug(f"Failed to start askpass log forwarder: {exc}")


def stop_askpass_log_forwarder() -> None:
    """Stop the background askpass log forwarder thread."""

    with _ASKPASS_LOG_THREAD_LOCK:
        if not (_ASKPASS_LOG_THREAD and _ASKPASS_LOG_THREAD.is_alive()):
            return

        _ASKPASS_LOG_THREAD_STOP.set()
        # Let the thread exit gracefully; no join to avoid blocking shutdown


atexit.register(stop_askpass_log_forwarder)


def _read_app_setting(key: str, default):
    """Read a top-level setting from ``config.json`` (stdlib only).

    Lets standalone/helper code (the askpass process, agent key-prep) honor the
    Settings → Advanced toggles without importing the app. Returns *default* on
    any error.
    """
    import json

    try:
        config_dir = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
        config_file = os.path.join(config_dir, "sshpilot", "config.json")
        with open(config_file, encoding="utf-8") as handle:
            data = json.load(handle)
        value = data.get(key, default)
        return bool(value) if isinstance(default, bool) else value
    except Exception:
        return default


def _askpass_enabled() -> bool:
    """Whether sshPilot's askpass helper is enabled at all (default True)."""
    return bool(_read_app_setting("use-askpass", True))


def _ask_main_app(request: dict, log_fn, *, ok_key: str = "passphrase") -> "tuple[bool, str | None]":
    """Send a JSON request to the main-app askpass socket.

    Returns ``(handled, value)``:
    - ``(True, "<secret>")`` — the user entered a value.
    - ``(True, None)``       — the user cancelled (do NOT also show a fallback).
    - ``(False, None)``      — main app unreachable; caller should fall back.
    """
    import json
    import socket

    sock_path = os.environ.get("SSHPILOT_ASKPASS_SOCKET", "")
    token = os.environ.get("SSHPILOT_ASKPASS_TOKEN", "")
    if not sock_path or not token:
        return (False, None)

    payload = dict(request)
    payload["token"] = token

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(sock_path)
            client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            # The user may take a while to type; allow a generous read window.
            client.settimeout(600)
            chunks = []
            while b"\n" not in b"".join(chunks):
                data = client.recv(4096)
                if not data:
                    break
                chunks.append(data)
    except Exception as exc:
        log_fn(f"ASKPASS: main-app routing unavailable ({exc}); using fallback")
        return (False, None)

    raw = b"".join(chunks).split(b"\n", 1)[0].strip()
    if not raw:
        log_fn("ASKPASS: main-app closed connection without a reply; using fallback")
        return (False, None)

    try:
        reply = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        log_fn(f"ASKPASS: malformed reply from main app ({exc}); using fallback")
        return (False, None)

    if reply.get("ok"):
        log_fn("ASKPASS: response provided by main-app dialog")
        # First key present wins — an empty string is a valid answer
        # (acknowledged presence reminder, empty kbd-interactive response)
        # and must not collapse into None (= cancel, exit 1).
        for key in (ok_key, "passphrase", "value"):
            value = reply.get(key)
            if value is not None:
                return (True, value)
        return (True, "")
    if reply.get("fallback"):
        log_fn("ASKPASS: main app asked to use the standalone window")
        return (False, None)
    log_fn("ASKPASS: prompt cancelled in main-app dialog")
    return (True, None)


def _route_challenge_to_main_app(
    prompt: str, log_fn
) -> "tuple[bool, str | None]":
    """Ask the main app to collect an interactive MFA/OTP response."""
    return _ask_main_app(
        {"type": "challenge", "prompt": prompt},
        log_fn,
        ok_key="value",
    )


def _route_confirm_to_main_app(
    prompt: str, log_fn
) -> "tuple[bool, str | None]":
    """Ask the main app for a yes/no confirm (SSH_ASKPASS_PROMPT=confirm)."""
    return _ask_main_app(
        {"type": "confirm", "prompt": prompt},
        log_fn,
        ok_key="value",
    )


def _route_presence_to_main_app(
    prompt: str, log_fn
) -> "tuple[bool, str | None]":
    """Ask the main app to show a FIDO touch reminder (no typed secret)."""
    return _ask_main_app(
        {"type": "presence", "prompt": prompt},
        log_fn,
        ok_key="value",
    )


def _route_password_to_main_app(
    prompt: str, log_fn
) -> "tuple[bool, str | None]":
    """Ask the main app to collect an unstored login password."""
    user = (os.environ.get("SSHPILOT_PASSWORD_USER") or "").strip()
    hosts_raw = os.environ.get("SSHPILOT_PASSWORD_HOSTS") or ""
    hosts = [h.strip() for h in hosts_raw.split("\n") if h.strip()]
    return _ask_main_app(
        {
            "type": "password",
            "prompt": prompt,
            "username": user,
            "host": hosts[0] if hosts else "",
        },
        log_fn,
        ok_key="value",
    )


def _standalone_password_label() -> str:
    """Default prompt for the standalone password fallback dialog.

    Derives "user@host's password:" from the askpass env context when known,
    so an empty ssh prompt does not fall back to the challenge dialog's
    "verification code" wording.
    """
    user = (os.environ.get("SSHPILOT_PASSWORD_USER") or "").strip()
    hosts_raw = os.environ.get("SSHPILOT_PASSWORD_HOSTS") or ""
    host = next((h.strip() for h in hosts_raw.split("\n") if h.strip()), "")
    if user and host:
        return f"{user}@{host}'s password:"
    return "Enter your password:"


def _run_challenge_dialog(prompt: str, log_fn) -> "str | None":
    """Standalone GTK dialog for OTP / verification-code style prompts."""
    try:
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        gi.require_version('Gio', '2.0')
        gi.require_version('Gdk', '4.0')
        from gi.repository import Gtk, Adw, Gio, Gdk
    except Exception as exc:
        log_fn(f"ASKPASS: GTK not available for challenge dialog: {exc}")
        return None

    log_fn("ASKPASS: Showing interactive challenge GUI dialog")
    result = [None]
    Adw.init()
    app = Adw.Application.new(
        "io.github.mfat.sshpilot.askpass.challenge",
        Gio.ApplicationFlags.NON_UNIQUE,
    )

    def on_activate(application):
        window = Adw.ApplicationWindow(application=application)
        window.set_title(_("Authentication Required"))
        window.set_resizable(False)
        window.set_default_size(420, -1)
        done = [False]

        entry = Adw.PasswordEntryRow()
        entry.set_title(_("Response"))

        cancel_btn = Gtk.Button(label=_("Cancel"))
        ok_btn = Gtk.Button(label=_("Continue"))
        ok_btn.add_css_class("suggested-action")

        def _finish(ok: bool):
            if done[0]:
                return
            done[0] = True
            if ok:
                result[0] = entry.get_text() or None
            try:
                application.quit()
            except Exception:
                pass

        cancel_btn.connect("clicked", lambda _b: _finish(False))
        ok_btn.connect("clicked", lambda _b: _finish(True))

        header = Adw.HeaderBar()
        header.pack_start(cancel_btn)
        header.pack_end(ok_btn)

        body = Gtk.Label(
            label=(prompt or "Please enter the verification code:").strip(),
            wrap=True,
            xalign=0,
        )
        body.add_css_class("body")

        group = Adw.PreferencesGroup()
        group.add(entry)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.append(body)
        box.append(group)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(box)
        window.set_content(toolbar)

        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def _on_key(_ctrl, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                _finish(False)
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                _finish(True)
                return True
            return False

        key_controller.connect("key-pressed", _on_key)
        window.add_controller(key_controller)
        window.present()
        entry.grab_focus()

    app.connect("activate", on_activate)
    app.run(None)
    return result[0]


def _lookup_via_main_app(key_path: str, log_fn) -> "str | None":
    """Resolve a stored passphrase via the running main app's warm cache.

    The askpass subprocess has no secret cache of its own, so with a session
    backend (Bitwarden) a direct lookup cold-loads the whole vault from ``bw`` on
    every invocation (~seconds). The main process already holds the unlocked vault
    in memory, so ask it over the existing askpass IPC socket. Returns the
    passphrase, or ``None`` when unavailable (caller falls back to a local lookup).
    This never prompts — it only reads an already-resolved/cached value.
    """
    import json
    import socket

    sock_path = os.environ.get("SSHPILOT_ASKPASS_SOCKET", "")
    token = os.environ.get("SSHPILOT_ASKPASS_TOKEN", "")
    if not sock_path or not token or not key_path:
        return None

    request = json.dumps({"token": token, "type": "lookup", "key_path": key_path})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            # Short timeout on purpose: a warm-cache answer is near-instant when the
            # main loop is free (the common case). When the loop is blocked (combined
            # auth runs build_ssh_connection under run_until_complete on the main
            # thread), it can't service this socket — so time out quickly and let the
            # caller fall back to a local lookup rather than stalling the whole connect.
            client.settimeout(1.5)
            client.connect(sock_path)
            client.sendall((request + "\n").encode("utf-8"))
            chunks = []
            while b"\n" not in b"".join(chunks):
                data = client.recv(4096)
                if not data:
                    break
                chunks.append(data)
    except Exception as exc:
        log_fn(f"ASKPASS: main-app lookup unavailable ({exc})")
        return None

    raw = b"".join(chunks).split(b"\n", 1)[0].strip()
    if not raw:
        return None
    try:
        reply = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if reply.get("ok") and reply.get("passphrase"):
        return reply.get("passphrase")
    return None


def _lookup_session_password_via_main_app(password_id: str, log_fn) -> "str | None":
    """Fetch a one-shot in-memory session password from the main app over IPC."""
    import json
    import socket

    sock_path = os.environ.get("SSHPILOT_ASKPASS_SOCKET", "")
    token = os.environ.get("SSHPILOT_ASKPASS_TOKEN", "")
    if not sock_path or not token or not password_id:
        return None

    request = json.dumps({
        "token": token, "type": "session_password", "id": password_id,
    })
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.5)
            client.connect(sock_path)
            client.sendall((request + "\n").encode("utf-8"))
            chunks = []
            while b"\n" not in b"".join(chunks):
                data = client.recv(4096)
                if not data:
                    break
                chunks.append(data)
    except Exception as exc:
        log_fn(f"ASKPASS: session-password IPC unavailable ({exc})")
        return None

    raw = b"".join(chunks).split(b"\n", 1)[0].strip()
    if not raw:
        return None
    try:
        reply = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if reply.get("ok") and reply.get("value"):
        return reply.get("value")
    return None


def _resolve_askpass_password(_log) -> "str | None":
    """Return a stored login password for this SSH child, or None to defer to TTY.

    Sources (first hit wins):
    1. ``SSHPILOT_SESSION_PASSWORD_ID`` — one-shot in-memory password held by
       the main app (fetched over askpass IPC; never written to disk).
    2. ``SSHPILOT_SESSION_PASSWORD_FILE`` — fallback one-shot temp file when
       IPC was unavailable at staging time (unlinked after read).
    3. Secret backend lookup for ``SSHPILOT_PASSWORD_USER`` @ each host in
       ``SSHPILOT_PASSWORD_HOSTS`` (newline-separated).
    """
    session_id = (os.environ.get("SSHPILOT_SESSION_PASSWORD_ID") or "").strip()
    if session_id:
        value = _lookup_session_password_via_main_app(session_id, _log)
        if value:
            _log("ASKPASS: Found session password via main-app IPC")
            return value
        _log("ASKPASS: session-password id present but IPC returned nothing")

    session_file = os.environ.get("SSHPILOT_SESSION_PASSWORD_FILE", "")
    if session_file and os.path.exists(session_file):
        try:
            with open(session_file, encoding="utf-8") as f:
                value = f.read()
            if value.endswith("\n"):
                value = value[:-1]
            try:
                os.unlink(session_file)
            except Exception:
                pass
            if value:
                _log("ASKPASS: Found session password from secure temp file")
                return value
        except Exception as exc:
            _log(f"ASKPASS: Error reading session password file: {exc}")

    user = (os.environ.get("SSHPILOT_PASSWORD_USER") or "").strip()
    hosts_raw = os.environ.get("SSHPILOT_PASSWORD_HOSTS") or ""
    hosts = [h.strip() for h in hosts_raw.split("\n") if h.strip()]
    if not user or not hosts:
        _log("ASKPASS: password prompt but no host/user context; deferring to TTY")
        return None
    for host in hosts:
        value = lookup_ssh_password(host, user)
        if value:
            _log(f"ASKPASS: Found stored password for {user}@{host}")
            return value
        _log(f"ASKPASS: No stored password for {user}@{host}")
    return None


def _run_presence_dialog(prompt: str, log_fn) -> "str | None":
    """Touch/presence reminder — no text field. Stays up until Close or SIGTERM.

    OpenSSH's notify_start() sets SSH_ASKPASS_PROMPT=none, redirects stdout to
    /dev/null, and SIGTERMs this process when the touch completes.
    """
    try:
        import signal
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        gi.require_version('Gio', '2.0')
        from gi.repository import Gtk, Adw, Gio, GLib
    except Exception as exc:
        log_fn(f"ASKPASS: GTK not available for presence dialog: {exc}")
        return ""

    log_fn("ASKPASS: Showing security-key presence reminder")
    Adw.init()
    app = Adw.Application.new(
        "io.github.mfat.sshpilot.askpass.presence",
        Gio.ApplicationFlags.NON_UNIQUE,
    )

    def on_activate(application):
        window = Adw.ApplicationWindow(application=application)
        window.set_title(_("Security Key"))
        window.set_resizable(False)
        window.set_default_size(420, -1)

        def _close(*_a):
            try:
                application.quit()
            except Exception:
                pass
            return GLib.SOURCE_REMOVE

        try:
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _close)
        except Exception:
            pass

        close_btn = Gtk.Button(label=_("Close"))
        close_btn.add_css_class("suggested-action")
        close_btn.connect("clicked", _close)

        header = Adw.HeaderBar()
        header.pack_end(close_btn)

        body = Gtk.Label(
            label=(prompt or "Touch your security key to continue.").strip(),
            wrap=True,
            xalign=0,
        )
        body.add_css_class("body")
        hint = Gtk.Label(
            label=_("Touch your security key when it blinks."),
            wrap=True,
            xalign=0,
        )
        hint.add_css_class("dim-label")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.append(body)
        box.append(hint)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(box)
        window.set_content(toolbar)
        window.present()

    app.connect("activate", on_activate)
    app.run(None)
    return ""


def _run_confirm_dialog(prompt: str, log_fn) -> "str | None":
    """Yes/No confirm for SSH_ASKPASS_PROMPT=confirm (e.g. ssh-add -c)."""
    try:
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        gi.require_version('Gio', '2.0')
        from gi.repository import Adw, Gio
    except Exception as exc:
        log_fn(f"ASKPASS: GTK not available for confirm dialog: {exc}")
        return None

    log_fn("ASKPASS: Showing confirm dialog")
    result = [None]
    Adw.init()
    app = Adw.Application.new(
        "io.github.mfat.sshpilot.askpass.confirm",
        Gio.ApplicationFlags.NON_UNIQUE,
    )

    def on_activate(application):
        dialog = Adw.AlertDialog()
        dialog.set_heading(_("Confirm"))
        dialog.set_body((prompt or "Allow this key operation?").strip())
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", _("Allow"))
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def _on_response(_d, response):
            if response == "ok":
                result[0] = "yes"
            try:
                application.quit()
            except Exception:
                pass

        dialog.connect("response", _on_response)
        dialog.present(None)

    app.connect("activate", on_activate)
    app.run(None)
    return result[0]


def _looks_like_login_password(prompt: str, kind: "str | None") -> bool:
    """True when *prompt* is a login-password ask (not a key passphrase)."""
    if kind == 'password':
        return True
    if kind is not None:
        return False
    lowered = prompt.lower()
    return "password" in lowered and "passphrase" not in lowered


def _prompt_via_main_or_dialog(
    prompt: str,
    log_fn,
    route_fn,
    dialog_fn,
    *,
    map_routed=None,
    success_log: str = "",
    cancel_log: str = "",
    dialog_success_log: str = "",
    dialog_cancel_log: str = "",
) -> "str | None":
    """Try main-app IPC first; on fallback run the standalone dialog.

    *map_routed* customizes how a successful IPC value is returned (presence
    maps any non-None to ``\"\"``). When omitted, *routed* is returned as-is.
    """
    handled, routed = route_fn(prompt, log_fn)
    if handled:
        if map_routed is not None:
            return map_routed(routed)
        if routed is not None:
            if success_log:
                log_fn(success_log)
            return routed
        if cancel_log:
            log_fn(cancel_log)
        return None

    value = dialog_fn(prompt, log_fn)
    if value is not None:
        if dialog_success_log:
            log_fn(dialog_success_log)
        return value
    if dialog_cancel_log:
        log_fn(dialog_cancel_log)
    return None


def _handle_presence_prompt(prompt: str, log_fn) -> "str | None":
    """FIDO / presence reminder: success is empty string, cancel is None."""
    return _prompt_via_main_or_dialog(
        prompt,
        log_fn,
        _route_presence_to_main_app,
        _run_presence_dialog,
        map_routed=lambda routed: "" if routed is not None else None,
    )


def _handle_confirm_prompt(prompt: str, log_fn) -> "str | None":
    return _prompt_via_main_or_dialog(
        prompt, log_fn, _route_confirm_to_main_app, _run_confirm_dialog,
    )


def _handle_challenge_prompt(
    prompt: str,
    log_fn,
    *,
    success_log: str = "ASKPASS: Returning interactive response from main-app dialog",
    dialog_success_log: str = "ASKPASS: Returning interactive response from GUI dialog",
    dialog_cancel_log: str = "ASKPASS: No interactive response; exiting with code 1",
) -> "str | None":
    return _prompt_via_main_or_dialog(
        prompt,
        log_fn,
        _route_challenge_to_main_app,
        _run_challenge_dialog,
        success_log=success_log,
        cancel_log="ASKPASS: user cancelled interactive prompt",
        dialog_success_log=dialog_success_log,
        dialog_cancel_log=dialog_cancel_log,
    )


def _handle_password_prompt(prompt: str, log_fn) -> "str | None":
    """Resolve from store, else ask via main app / standalone password dialog."""
    value = _resolve_askpass_password(log_fn)
    if value:
        log_fn("ASKPASS: Returning password to caller")
        return value
    # No vault/session password — ask the user. prefer does not fall back
    # to the TTY (same as MFA), so a dialog is required.
    log_fn("ASKPASS: no stored password; asking user")

    def _password_dialog(p, log):
        # Standalone fallback: password-appropriate default so an empty prompt
        # does not read as "verification code" (OTP/challenge default).
        return _run_challenge_dialog(p or _standalone_password_label(), log)

    return _prompt_via_main_or_dialog(
        prompt,
        log_fn,
        _route_password_to_main_app,
        _password_dialog,
        success_log="ASKPASS: Returning password from main-app dialog",
        cancel_log="ASKPASS: user cancelled password prompt",
        dialog_success_log="ASKPASS: Returning password from GUI dialog",
        dialog_cancel_log="ASKPASS: No password available; exiting with code 1",
    )


def _resolve_passphrase_for_askpass(key_path: str, log_fn) -> "str | None":
    """Resolve a key passphrase from session file, local store, or main-app IPC."""
    # Check one-shot session passphrase written by the main app
    session_passphrase_file = os.environ.get("SSHPILOT_SESSION_PASSPHRASE_FILE", "")
    if session_passphrase_file and os.path.exists(session_passphrase_file):
        try:
            with open(session_passphrase_file, encoding="utf-8") as f:
                session_passphrase = f.read().strip()
            if session_passphrase:
                log_fn("ASKPASS: Found session passphrase from secure temp file")
                try:
                    os.unlink(session_passphrase_file)
                except Exception:
                    pass
                return session_passphrase
        except Exception as exc:
            log_fn(f"ASKPASS: Error reading session passphrase file: {exc}")

    # Resolve from storage. Two routes:
    #   * local — read the selected backend in this subprocess. Instant for the
    #     platform stores (libsecret/keyring), but for a session vault
    #     (Bitwarden/Vaultwarden) it cold-starts `bw` (~1-2s) because the
    #     in-memory vault cache lives in the *main* process, not here.
    #   * main-app IPC — ask the running app, which already holds the unlocked
    #     vault in memory (near-instant, no `bw` spawn).
    # Session vault: IPC first. Platform stores: local first.
    try:
        from .secret_storage import get_secret_manager
        _selected = (os.environ.get("SSHPILOT_SECRET_BACKEND") or "auto").strip().lower()
        # Session backends (bw) hold the vault only in the main process, so IPC
        # is the only warm route. rbw has a shared agent, but a local `rbw get`
        # still costs ~1s per connect on a large vault — prefer IPC for it too.
        prefer_ipc = bool(get_secret_manager().is_session_backed(_selected)) or _selected == "rbw"
    except Exception:
        prefer_ipc = False

    def _resolve_local():
        for candidate in _get_key_path_lookup_candidates(key_path):
            passphrase = lookup_passphrase(candidate)
            if passphrase:
                log_fn(f"ASKPASS: Found passphrase for {candidate}")
                return passphrase
            log_fn(f"ASKPASS: No passphrase found for {candidate}")
        return None

    def _resolve_main_app():
        value = _lookup_via_main_app(key_path, log_fn)
        if value:
            log_fn("ASKPASS: passphrase resolved via main-app cache")
        return value

    resolvers = ((_resolve_main_app, _resolve_local) if prefer_ipc
                 else (_resolve_local, _resolve_main_app))
    for resolve in resolvers:
        value = resolve()
        if value:
            log_fn("ASKPASS: Returning passphrase to caller")
            return value

    # No stored passphrase — defer to SSH / the OS / ssh-agent.
    # Login-password and MFA use the graphical path above; unstored key
    # passphrases do not.
    log_fn("ASKPASS: No stored passphrase; deferring to system/SSH")
    return None


def handle_askpass_cli(prompt: str) -> "str | None":
    """Handle --askpass CLI mode (re-invoked by SSH as SSH_ASKPASS handler).

    Honors OpenSSH's ``SSH_ASKPASS_PROMPT`` hint when set:

    * ``none`` — FIDO touch reminder (no typed secret; stay until Close/SIGTERM)
    * ``confirm`` — yes/no (``ssh-add -c`` style)

    Otherwise answers key passphrase / login password from the secret backend,
    or prompts for OTP/PIN. Returns the secret (or ``\"\"`` / ``\"yes\"`` for
    notify/confirm), or None on cancel/failure.
    """
    log_path = get_askpass_log_path()

    def _log(msg: str) -> None:
        try:
            with open(log_path, "a") as f:
                f.write(f"{msg}\n")
        except Exception:
            pass

    _log(
        f"ASKPASS: keyring {'available' if _keyring_available() else 'unavailable'}, "
        f"libsecret {'available' if _secret_available() else 'unavailable'}"
    )
    hint = (os.environ.get("SSH_ASKPASS_PROMPT") or "").strip().lower()
    _log(f"ASKPASS called with prompt: {prompt!r} hint={hint!r}")

    # Background/passive spawns (e.g. the autocomplete history fetch) set
    # AUTOFILL_ONLY: answer silently from stored secrets, never show UI.
    if (os.environ.get("SSHPILOT_ASKPASS_AUTOFILL_ONLY") or "").strip() == "1":
        only_kind = classify_prompt(prompt)
        lowered = prompt.lower()
        if _looks_like_login_password(prompt, only_kind):
            value = _resolve_askpass_password(_log)
            _log("ASKPASS: autofill-only mode; "
                 + ("answered from store" if value else "no stored password, declining"))
            return value
        if only_kind != 'passphrase' and "passphrase" not in lowered:
            _log("ASKPASS: autofill-only mode; declining interactive prompt")
            return None
        # Passphrases fall through: that path is already dialog-free
        # (vault/session lookup, then defer to SSH/agent).

    # A blank prompt with no UI hint carries no question to answer — OpenSSH
    # always passes prompt text, so this is a bare/misfired invocation (e.g.
    # running the helper with no argv). Decline instead of falling through to
    # the "unrecognized prompt" branch, which would pop a verification-code
    # dialog out of nowhere.
    if not (prompt or "").strip() and not hint:
        _log("ASKPASS: empty prompt with no hint; declining")
        return None

    # Official OpenSSH askpass UI hints (see readpass.c / notify_start).
    if hint == "none":
        _log("ASKPASS: SSH_ASKPASS_PROMPT=none (FIDO presence reminder)")
        return _handle_presence_prompt(prompt, _log)

    if hint == "confirm":
        _log("ASKPASS: SSH_ASKPASS_PROMPT=confirm (yes/no)")
        return _handle_confirm_prompt(prompt, _log)

    kind = classify_prompt(prompt)
    if kind == 'presence':
        _log("ASKPASS: presence prompt (classifier); reminder UI")
        return _handle_presence_prompt(prompt, _log)

    if kind == 'interactive':
        # OpenSSH with SSH_ASKPASS_REQUIRE=prefer does NOT fall back to the
        # TTY when askpass fails — declining here leaves the user with no way
        # to enter an OTP. Prompt them (never autofill MFA from the vault).
        _log("ASKPASS: interactive prompt (OTP/PIN); asking user")
        return _handle_challenge_prompt(prompt, _log)

    if _looks_like_login_password(prompt, kind):
        return _handle_password_prompt(prompt, _log)

    if kind != 'passphrase' and "passphrase" not in prompt.lower():
        # Unknown keyboard-interactive / custom MFA text: prefer a typed
        # challenge dialog over exiting 1 (OpenSSH will not fall back to the
        # TTY once askpass was invoked under REQUIRE=prefer).
        _log("ASKPASS: unrecognized prompt; treating as interactive challenge")
        return _handle_challenge_prompt(
            prompt,
            _log,
            success_log="ASKPASS: Returning response from main-app dialog",
            dialog_success_log="ASKPASS: Returning response from GUI dialog",
        )

    key_path = _extract_key_path(prompt)
    if not key_path:
        _log("ASKPASS: Could not extract key path from prompt")
        return None

    _log(f"ASKPASS: Extracted key path: {key_path}")
    return _resolve_passphrase_for_askpass(key_path, _log)



def run_askpass_and_write(prompt: str) -> int:
    """Entry-point wrapper: separates password output from all other I/O.

    Saves the real stdout fd, redirects fd 1 to stderr so any accidental
    logging or print cannot contaminate the password, calls
    handle_askpass_cli, then writes the returned passphrase to the original
    stdout fd using os.write (bypassing Python buffering).

    Returns 0 on success, 1 on failure.
    """
    try:
        real_stdout_fd = os.dup(1)
    except OSError:
        real_stdout_fd = None

    try:
        try:
            os.dup2(2, 1)
        except OSError:
            pass
        sys.stdout = sys.stderr

        passphrase = handle_askpass_cli(prompt)
    finally:
        if real_stdout_fd is not None:
            try:
                os.dup2(real_stdout_fd, 1)
            except OSError:
                pass
            try:
                os.close(real_stdout_fd)
            except OSError:
                pass

    if passphrase is None:
        return 1

    try:
        os.write(1, (passphrase + "\n").encode())
    except OSError:
        return 1
    return 0


def store_passphrase(key_path: str, passphrase: str) -> bool:
    """Store a key passphrase via the selected secret backend."""

    if not key_path:
        return False

    from .secret_storage import get_secret_manager, passphrase_spec

    canonical_path = _normalize_key_path_for_storage(key_path)
    return get_secret_manager().store(passphrase_spec(canonical_path), passphrase)


def lookup_passphrase(key_path: str) -> str:
    """Look up a key passphrase via the selected secret backend.

    Tries each normalized candidate path; the backend itself falls through to the
    platform default stores so passphrases saved under a previous backend resolve.
    """

    candidates = _get_key_path_lookup_candidates(key_path)
    if not candidates:
        return ""

    from .secret_storage import get_secret_manager, passphrase_spec

    manager = get_secret_manager()
    for candidate in candidates:
        result = manager.lookup(passphrase_spec(candidate))
        if result:
            return result
    return ""


def resolve_passphrase_for_ipc(key_path: str) -> str:
    """Server-side (main-process) passphrase resolve for the askpass IPC that never blocks
    on a slow backend.

    For rbw a plain ``rbw get`` costs ~1s, which would exceed the IPC client's short
    timeout and make it fall back to a *second* local lookup — worse than not using IPC.
    So for rbw we answer only from the main process's warm value cache; on a cold miss we
    warm it in the background (so the next connect is instant) and return "" immediately,
    letting the client fall back locally this once. Other backends already resolve from a
    warm/instant store, so use the normal path."""
    selected = (os.environ.get("SSHPILOT_SECRET_BACKEND") or "auto").strip().lower()
    if selected != "rbw":
        return lookup_passphrase(key_path)

    from .secret_storage import get_secret_manager, passphrase_spec
    manager = get_secret_manager()
    rbw = manager.get_backend("rbw")
    peek = getattr(rbw, "peek", None)
    if callable(peek):
        for candidate in _get_key_path_lookup_candidates(key_path):
            value = peek(passphrase_spec(candidate).keyring_account)
            if value:
                return value
    # Cold cache: warm it off-thread for next time, answer "not found" now. Guard against
    # piling up concurrent `rbw get`s when ssh retries askpass rapidly for the same key.
    _warm_passphrase_async(key_path)
    return ""


_warming_keys: set = set()
_warming_lock = threading.Lock()


def _warm_passphrase_async(key_path: str) -> None:
    """Resolve ``key_path`` once in the background to populate the main-app value cache,
    deduping so rapid askpass retries don't launch a stampede of concurrent lookups."""
    with _warming_lock:
        if key_path in _warming_keys:
            return
        _warming_keys.add(key_path)

    def _run():
        try:
            lookup_passphrase(key_path)
        finally:
            with _warming_lock:
                _warming_keys.discard(key_path)

    threading.Thread(target=_run, daemon=True).start()


def clear_passphrase(key_path: str) -> bool:
    """Remove a stored key passphrase from all available secret backends."""

    candidates = _get_key_path_lookup_candidates(key_path)
    if not candidates:
        return False

    from .secret_storage import get_secret_manager, passphrase_spec

    manager = get_secret_manager()
    removed_any = False
    for candidate in candidates:
        if manager.delete(passphrase_spec(candidate)):
            removed_any = True
    return removed_any


def store_sudo_password(host: str, username: str, password: str) -> bool:
    """Store a host's **sudo** password via the selected secret backend.

    Routed through :class:`SecretManager` (like SSH passwords/passphrases) so it
    honours the user's chosen backend instead of always hitting libsecret/keyring.
    Kept under its own ``type=sudo_password`` schema (see ``sudo_password_spec``)
    so it never collides with the SSH login password."""
    if not host:
        return False

    from .secret_storage import get_secret_manager, sudo_password_spec

    return get_secret_manager().store(sudo_password_spec(host, username), password)


def lookup_sudo_password(host: str, username: str) -> str:
    """Look up a host's stored sudo password ("" if none) via the secret backend."""
    if not host:
        return ""

    from .secret_storage import get_secret_manager, sudo_password_spec

    return get_secret_manager().lookup(sudo_password_spec(host, username)) or ""


def clear_sudo_password(host: str, username: str) -> bool:
    """Remove a host's stored sudo password (e.g. when it proves wrong)."""
    if not host:
        return False

    from .secret_storage import get_secret_manager, sudo_password_spec

    return get_secret_manager().delete(sudo_password_spec(host, username))


# Substrings in sudo's stderr when the user cannot use sudo at all (as opposed to
# merely needing a password or having entered a wrong one). Used to tell "no sudo
# access on this host" apart from "wrong/needs password".
_SUDO_NOT_ALLOWED_MARKERS = (
    "is not in the sudoers",
    "is not allowed to execute",
    "is not allowed to run sudo",
    "unknown user",
)


def is_sudo_denied_error(text: str) -> bool:
    """True if ``text`` (sudo stderr/stdout) indicates the user has no sudo
    access at all, rather than a wrong or missing password."""
    low = (text or "").lower()
    return any(marker in low for marker in _SUDO_NOT_ALLOWED_MARKERS)


def ensure_passphrase_askpass() -> str:
    """Ensure the askpass shell wrapper exists and return its path.

    Generates a tiny ``askpass.sh`` that re-invokes the running application
    with ``--askpass``, guaranteeing passphrase lookup runs in the exact same
    Python environment (and therefore has access to keyring/libsecret).
    """
    global _ASKPASS_DIR, _ASKPASS_SCRIPT

    logger.debug("Ensuring askpass script is available")

    if _ASKPASS_SCRIPT and os.path.exists(_ASKPASS_SCRIPT):
        logger.debug(f"Using cached askpass script at {_ASKPASS_SCRIPT}")
        return _ASKPASS_SCRIPT

    _ASKPASS_SCRIPT = None
    if _ASKPASS_DIR:
        try:
            shutil.rmtree(_ASKPASS_DIR, ignore_errors=True)
        except Exception:
            pass
        _ASKPASS_DIR = None

    _ASKPASS_DIR = tempfile.mkdtemp(prefix="sshpilot-askpass-")
    os.chmod(_ASKPASS_DIR, 0o700)

    script_path = os.path.join(_ASKPASS_DIR, "askpass.sh")
    logger.debug(f"Generating askpass shell wrapper at {script_path}")

    if getattr(sys, 'frozen', False):
        # PyInstaller bundle: sys.executable IS the compiled binary.
        # run.py intercepts --askpass before GTK initialisation.
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" --askpass "$@"\n')
    else:
        # Homebrew / source / Flatpak.
        # sys.argv[0] may be a shell shim (Homebrew), not a Python file,
        # so we cannot call `python sys.argv[0] --askpass`.  Instead we write
        # a tiny Python helper into the same temp dir; it adds the package
        # parent to sys.path explicitly and calls handle_askpass_cli directly.
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        helper_path = os.path.join(_ASKPASS_DIR, "askpass_helper.py")
        with open(helper_path, "w", encoding="utf-8") as f:
            f.write("import sys\n")
            f.write(f"sys.path.insert(0, {pkg_parent!r})\n")
            f.write("from sshpilot.askpass_utils import run_askpass_and_write\n")
            f.write("prompt = sys.argv[1] if len(sys.argv) > 1 else ''\n")
            f.write("sys.exit(run_askpass_and_write(prompt))\n")
        os.chmod(helper_path, 0o600)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{helper_path}" "$@"\n')

    script_mode = os.stat(script_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    os.chmod(script_path, script_mode)

    def _cleanup():
        try:
            if _ASKPASS_DIR:
                shutil.rmtree(_ASKPASS_DIR, ignore_errors=True)
        except Exception:
            pass

    atexit.register(_cleanup)
    _ASKPASS_SCRIPT = script_path
    return script_path


def ensure_askpass_script() -> str:
    """Ensure the askpass script is available for passphrase handling"""
    return ensure_passphrase_askpass()

def force_regenerate_askpass_script() -> str:
    """Force regeneration of the askpass script"""
    global _ASKPASS_DIR, _ASKPASS_SCRIPT
    _ASKPASS_SCRIPT = None
    if _ASKPASS_DIR:
        try:
            shutil.rmtree(_ASKPASS_DIR, ignore_errors=True)
        except Exception:
            pass
        _ASKPASS_DIR = None
    return ensure_passphrase_askpass()

def get_ssh_env_with_askpass(
    require: str = "prefer",
    *,
    password_user: "str | None" = None,
    password_hosts: "List[str] | None" = None,
    session_password: "str | None" = None,
) -> dict:
    """Get SSH environment with askpass for passphrase and/or password handling.

    ``require`` is OpenSSH's ``SSH_ASKPASS_REQUIRE`` (``prefer`` recommended so
    declined MFA prompts can fall back to the TTY). When *password_user* /
    *password_hosts* are set, the helper can autofill login-password prompts
    from the secret backend. *session_password* stages an in-memory password
    for the same purpose (IPC id when the prompt server is up; otherwise a
    one-shot temp file under XDG_RUNTIME_DIR).
    """
    ensure_askpass_log_forwarder()
    env = os.environ.copy()
    env["SSH_ASKPASS"] = ensure_passphrase_askpass()
    env["SSH_ASKPASS_REQUIRE"] = require
    # Ensure DISPLAY is set for SSH_ASKPASS to work properly
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    # Disable GNOME keyring interference with SSH but keep D-Bus for libsecret
    env["GNOME_KEYRING_CONTROL"] = ""
    env["GNOME_KEYRING_PID"] = ""
    env["GNOME_KEYRING_SOCKET"] = ""
    # Don't disable DBUS_SESSION_BUS_ADDRESS - libsecret needs it
    # If the main app is running an in-process passphrase-prompt server, tell the
    # askpass helper how to reach it so the prompt renders as a modal child of the
    # main window instead of a stray top-level that can hide behind it. Set them
    # authoritatively (clearing any stale inherited value when not advertising).
    if _ASKPASS_SOCKET and _ASKPASS_TOKEN:
        env["SSHPILOT_ASKPASS_SOCKET"] = _ASKPASS_SOCKET
        env["SSHPILOT_ASKPASS_TOKEN"] = _ASKPASS_TOKEN
    else:
        env.pop("SSHPILOT_ASKPASS_SOCKET", None)
        env.pop("SSHPILOT_ASKPASS_TOKEN", None)

    # Login-password context for the askpass helper (optional).
    if password_user:
        env["SSHPILOT_PASSWORD_USER"] = str(password_user)
    else:
        env.pop("SSHPILOT_PASSWORD_USER", None)
    hosts = [str(h).strip() for h in (password_hosts or []) if str(h).strip()]
    if hosts:
        env["SSHPILOT_PASSWORD_HOSTS"] = "\n".join(hosts)
    else:
        env.pop("SSHPILOT_PASSWORD_HOSTS", None)
    env.pop("SSHPILOT_SESSION_PASSWORD_ID", None)
    env.pop("SSHPILOT_SESSION_PASSWORD_FILE", None)
    if session_password:
        env.update(stage_session_password(session_password))
    return env

def get_ssh_env_with_askpass_for_password(host: str, username: str) -> dict:
    """SSH env with askpass wired for a login-password autofill on *host*/*username*.

    Uses ``SSH_ASKPASS_REQUIRE=prefer`` so OTP/MFA prompts declined by the helper
    can fall back to the TTY.
    """
    return get_ssh_env_with_askpass(
        "prefer",
        password_user=username or None,
        password_hosts=[host] if host else None,
    )

def get_ssh_env_with_forced_askpass() -> dict:
    """Get SSH environment with forced askpass for passphrase handling"""
    return get_ssh_env_with_askpass("force")

def ensure_key_in_agent(key_path: str, *, force: bool = False, lifetime: int = 0) -> bool:
    """Ensure an SSH key is loaded (and unlocked) in ssh-agent.

    ``force=True`` skips the ``ssh-add -l`` presence check and always runs
    ``ssh-add``. This is required for gnome-keyring, which *advertises* an
    on-disk key (its fingerprint shows in ``ssh-add -l``) but REFUSES to sign it
    while locked — so a presence-only skip would leave the key locked and ssh
    cannot fall back to the on-disk file. Re-running ``ssh-add`` decrypts the key
    client-side (our askpass autofills the passphrase from the keyring) and hands
    the unlocked key to the agent. It is idempotent and silent when the
    passphrase is in the keyring.

    ``lifetime > 0`` adds ``-t <lifetime>`` so the agent drops the key after that
    many seconds.
    """
    if not os.path.isfile(key_path):
        logger.error(f"Key file not found: {key_path}")
        return False

    # Check if key is already in ssh-agent. Skipped when force=True because a
    # listed key may be locked (gnome-keyring) and still need an actual ssh-add.
    if not force:
        try:
            result = subprocess.run(
                ['ssh-add', '-l'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and key_path in result.stdout:
                logger.debug(f"Key already in ssh-agent: {key_path}")
                return True
        except Exception:
            pass

    # Adding the key to the agent requires our askpass to supply the passphrase.
    # If the askpass helper is disabled, don't try — let SSH prompt natively.
    if not _askpass_enabled():
        logger.debug("Askpass disabled; not adding key to agent: %s", key_path)
        return False

    # Add key to ssh-agent using our askpass script
    env = get_ssh_env_with_askpass("force")

    add_cmd = ['ssh-add']
    if lifetime and lifetime > 0:
        add_cmd += ['-t', str(int(lifetime))]
    add_cmd.append(key_path)

    try:
        result = subprocess.run(
            add_cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            logger.debug(f"Successfully added key to ssh-agent: {key_path}")
            return True
        else:
            logger.error(f"Failed to add key to ssh-agent: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout adding key to ssh-agent: {key_path}")
        return False
    except Exception as e:
        logger.error(f"Error adding key to ssh-agent: {e}")
        return False

def prepare_key_for_connection(key_path: str, *, force: bool = True) -> bool:
    """Prepare SSH key for connection by ensuring it's unlocked in ssh-agent.

    Forces the ``ssh-add`` by default so a gnome-keyring-locked key is actually
    unlocked (see ``ensure_key_in_agent``).
    """
    return ensure_key_in_agent(key_path, force=force)

def get_scp_ssh_options() -> list:
    """Get SSH options for SCP operations with passphrased keys"""
    return [
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "IdentitiesOnly=yes",
    ]

