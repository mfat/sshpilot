# askpass_utils.py
import atexit
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from typing import List

try:
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret
except Exception:  # pragma: no cover - optional dependency
    Secret = None

try:
    import keyring
except Exception:  # pragma: no cover - optional dependency
    keyring = None

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


def _normalize_key_path_for_storage(key_path: str) -> str:
    """Return a canonical representation for storing passphrases."""

    expanded = os.path.expanduser(key_path)
    try:
        return os.path.realpath(expanded)
    except Exception:
        # ``realpath`` can fail on some exotic platforms; fall back to ``abspath``
        return os.path.abspath(expanded)


def _home_alias_for_path(path: str) -> str:
    """Return a home-relative alias (~/...) for *path* when applicable."""

    try:
        home = os.path.expanduser("~")
    except Exception:
        return ""

    if not home:
        return ""

    try:
        relative = os.path.relpath(path, home)
    except ValueError:
        return ""

    if relative in (".", os.curdir):
        return "~"

    if relative.startswith(".."):
        return ""

    return os.path.join("~", relative)


def _get_key_path_lookup_candidates(key_path: str) -> List[str]:
    """Return normalized key path variants for lookup and compatibility."""

    if not key_path:
        return []

    candidates: List[str] = []
    seen = set()

    def _add(path: str) -> None:
        if not path:
            return
        if path in seen:
            return
        seen.add(path)
        candidates.append(path)

    canonical = _normalize_key_path_for_storage(key_path)
    _add(canonical)

    expanded = os.path.expanduser(key_path)
    _add(expanded)

    for base in (canonical, expanded):
        alias = _home_alias_for_path(base)
        if alias:
            _add(alias)

    _add(key_path)

    return candidates


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


def get_secret_schema() -> "Secret.Schema":
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


def _builtin_passphrase_prompt_enabled() -> bool:
    """Whether the built-in GUI passphrase prompt is enabled (default False).

    Off by default: keyring autofill still works (it's resolved before this
    gate), and for a key with no stored passphrase we defer to SSH / the OS /
    ssh-agent to prompt naturally rather than showing our own dialog.
    """
    return bool(_read_app_setting("use-builtin-passphrase-prompt", False))


def _run_askpass_dialog(key_path: str, log_fn) -> "str | None":
    """Show a GTK4/Adwaita passphrase dialog. Returns the passphrase string, or
    None on cancel. Built from non-deprecated Adwaita widgets (an Adw.Window with
    a header bar and a boxed-list Adw.PasswordEntryRow)."""
    import json

    try:
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        gi.require_version('Gio', '2.0')
        gi.require_version('Gdk', '4.0')
        gi.require_version('GLib', '2.0')
        from gi.repository import Gtk, Adw, GLib, Gio, Gdk
    except Exception as exc:
        log_fn(f"ASKPASS: GTK not available: {exc}")
        return None

    log_fn("ASKPASS: No stored passphrase found, showing GUI dialog")

    passphrase_result = [None]
    Adw.init()

    app = Adw.Application.new("io.github.mfat.sshpilot.askpass", Gio.ApplicationFlags.NON_UNIQUE)

    def on_activate(app):
        try:
            try:
                config_dir = os.path.join(GLib.get_user_config_dir(), "sshpilot")
            except Exception:
                config_dir = os.path.join(os.path.expanduser("~"), ".config", "sshpilot")
            config_file = os.path.join(config_dir, "config.json")
            saved_theme = "default"
            if os.path.exists(config_file):
                try:
                    with open(config_file) as f:
                        saved_theme = str(json.load(f).get("app-theme", "default"))
                except Exception:
                    pass
            style_manager = Adw.StyleManager.get_default()
            if saved_theme == "light":
                style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            elif saved_theme == "dark":
                style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            else:
                style_manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
        except Exception:
            pass

        key_name = os.path.basename(key_path) if key_path else "key"

        # Adwaita-styled prompt: a window whose header bar carries the
        # Cancel/Unlock actions, with the passphrase in a boxed-list row.
        window = Adw.ApplicationWindow(application=app)
        window.set_title("Passphrase Required")
        window.set_resizable(False)
        window.set_default_size(400, -1)

        done = [False]

        # ── widgets ───────────────────────────────────────────────────────
        password_row = Adw.PasswordEntryRow()
        password_row.set_title("Passphrase")

        store_row = Adw.SwitchRow()
        store_row.set_title("Store passphrase")
        store_row.set_active(False)

        cancel_btn = Gtk.Button(label="Cancel")
        ok_btn = Gtk.Button(label="Unlock")
        ok_btn.add_css_class("suggested-action")

        # ── behaviour ─────────────────────────────────────────────────────
        def _record_and_quit(ok: bool):
            if done[0]:
                return
            done[0] = True
            if ok:
                passphrase_result[0] = password_row.get_text()
                if store_row.get_active() and key_path and passphrase_result[0]:
                    try:
                        store_passphrase(key_path, passphrase_result[0])
                    except Exception:
                        pass
            try:
                app.quit()
            except Exception:
                pass

        cancel_btn.connect("clicked", lambda _b: _record_and_quit(False))
        ok_btn.connect("clicked", lambda _b: _record_and_quit(True))
        window.set_default_widget(ok_btn)

        # ── layout ────────────────────────────────────────────────────────
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        header.pack_start(cancel_btn)
        header.pack_end(ok_btn)

        body_label = Gtk.Label(label=f"Enter the passphrase for key “{key_name}”.")
        body_label.set_wrap(True)
        body_label.set_xalign(0.0)
        body_label.add_css_class("dim-label")

        group = Adw.PreferencesGroup()
        group.add(password_row)
        group.add(store_row)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(24)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.append(body_label)
        content.append(group)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(content)
        window.set_content(toolbar)

        # ── dismissal: window close, Escape, Enter ────────────────────────
        def _on_close_request(_w):
            if not done[0]:
                done[0] = True
            return False

        window.connect("close-request", _on_close_request)

        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def _on_key(_ctrl, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                _record_and_quit(False)
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                _record_and_quit(True)
                return True
            return False

        key_controller.connect("key-pressed", _on_key)
        window.add_controller(key_controller)

        window.present()
        password_row.grab_focus()

    app.connect("activate", on_activate)
    app.run(None)
    return passphrase_result[0]


def _route_passphrase_to_main_app(
    key_path: str, prompt: str, log_fn
) -> "tuple[bool, str | None]":
    """Ask the running main app to show the passphrase prompt in-process.

    Returns ``(handled, value)``:
    - ``(True, "<passphrase>")`` — the user entered a passphrase.
    - ``(True, None)``           — the user cancelled (do NOT also show the
      standalone window).
    - ``(False, None)``          — the main app is unreachable / errored; the
      caller should fall back to the standalone dialog.
    """
    import json
    import socket

    sock_path = os.environ.get("SSHPILOT_ASKPASS_SOCKET", "")
    token = os.environ.get("SSHPILOT_ASKPASS_TOKEN", "")
    if not sock_path or not token:
        return (False, None)

    request = json.dumps(
        {"token": token, "type": "passphrase", "key_path": key_path, "prompt": prompt}
    )

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(sock_path)
            client.sendall((request + "\n").encode("utf-8"))
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
        log_fn("ASKPASS: passphrase provided by main-app dialog")
        return (True, reply.get("passphrase"))
    if reply.get("fallback"):
        log_fn("ASKPASS: main app asked to use the standalone window")
        return (False, None)
    log_fn("ASKPASS: passphrase prompt cancelled in main-app dialog")
    return (True, None)


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
            # main loop is free (the common preload-on-worker-thread path). When the
            # loop is blocked (combined auth runs build_ssh_connection under
            # run_until_complete on the main thread), it can't service this socket —
            # so time out quickly and let the caller fall back to a local lookup
            # rather than stalling the whole connect.
            client.settimeout(3)
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


def handle_askpass_cli(prompt: str) -> "str | None":
    """Handle --askpass CLI mode (re-invoked by SSH as SSH_ASKPASS handler).

    Looks up stored passphrases using the app's own environment (keyring,
    libsecret) and falls back to a GTK dialog. Returns the passphrase string
    on success, or None on failure. The caller is responsible for writing the
    passphrase to the real stdout fd.
    """
    log_path = get_askpass_log_path()

    def _log(msg: str) -> None:
        try:
            with open(log_path, "a") as f:
                f.write(f"{msg}\n")
        except Exception:
            pass

    _log(
        f"ASKPASS: keyring {'available' if keyring else 'unavailable'}, "
        f"libsecret {'available' if Secret else 'unavailable'}"
    )
    _log(f"ASKPASS called with prompt: {prompt}")

    pl = prompt.lower()

    if "password" in pl and "passphrase" not in pl:
        _log("ASKPASS: Ignoring password prompt")
        return None

    if "passphrase" not in pl:
        _log("ASKPASS: No passphrase found, exiting with code 1")
        return None

    key_path = _extract_key_path(prompt)
    if not key_path:
        _log("ASKPASS: Could not extract key path from prompt")
        return None

    _log(f"ASKPASS: Extracted key path: {key_path}")

    # Check one-shot session passphrase written by the main app
    session_passphrase_file = os.environ.get("SSHPILOT_SESSION_PASSPHRASE_FILE", "")
    if session_passphrase_file and os.path.exists(session_passphrase_file):
        try:
            with open(session_passphrase_file, encoding="utf-8") as f:
                session_passphrase = f.read().strip()
            if session_passphrase:
                _log("ASKPASS: Found session passphrase from secure temp file")
                try:
                    os.unlink(session_passphrase_file)
                except Exception:
                    pass
                return session_passphrase
        except Exception as exc:
            _log(f"ASKPASS: Error reading session passphrase file: {exc}")

    # Resolve via the selected secret backend directly. For Bitwarden this runs a
    # targeted `bw` lookup using the inherited BW_SESSION; for libsecret/keyring it reads
    # the platform store in-process. This works even when the main app's GTK loop is
    # blocked (e.g. combined auth), so we try it before the IPC fallback.
    for candidate in _get_key_path_lookup_candidates(key_path):
        passphrase = lookup_passphrase(candidate)
        if passphrase:
            _log(f"ASKPASS: Found passphrase for {candidate}")
            _log("ASKPASS: Returning passphrase to caller")
            return passphrase
        _log(f"ASKPASS: No passphrase found for {candidate}")

    # Fallback: ask the running main app to resolve it from its warm cache (covers the
    # case where the serve daemon isn't reachable from this subprocess but the app is).
    main_app_value = _lookup_via_main_app(key_path, _log)
    if main_app_value:
        _log("ASKPASS: passphrase resolved via main-app cache")
        return main_app_value

    # Fall back to the built-in GUI dialog, unless the user has turned it off in
    # settings — in that case defer to SSH / the system keyring prompt.
    if not _builtin_passphrase_prompt_enabled():
        _log("ASKPASS: built-in passphrase prompt disabled; deferring to system/SSH")
        return None

    # Prefer routing the prompt to the running main app so it renders as a modal
    # child of the main window (avoids the prompt hiding behind it on Wayland).
    handled, routed = _route_passphrase_to_main_app(key_path, prompt, _log)
    if handled:
        if routed is not None:
            return routed
        _log("ASKPASS: user cancelled main-app dialog, exiting with code 1")
        return None

    # Main app not reachable: show our own standalone window as before.
    passphrase = _run_askpass_dialog(key_path, _log)
    if passphrase is not None:
        _log("ASKPASS: User entered passphrase in GUI dialog")
        return passphrase

    _log("ASKPASS: No passphrase found, exiting with code 1")
    return None


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

def get_ssh_env_with_askpass(require: str = "prefer") -> dict:
    """Get SSH environment with askpass for passphrase handling"""
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
    return env

def get_ssh_env_with_askpass_for_password(host: str, username: str) -> dict:
    """Return a copy of the environment without SSH_ASKPASS variables.

    Previously this helper forced use of the askpass script for password
    authentication.  We now want the OpenSSH client to prompt the user
    directly when sshpass is unavailable, so we explicitly strip any
    askpass related variables that might interfere with interactive
    prompts.
    """
    env = os.environ.copy()
    env.pop("SSH_ASKPASS", None)
    env.pop("SSH_ASKPASS_REQUIRE", None)
    return env

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

