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

_SCHEMA = None

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
    """Return the shared Secret.Schema for stored secrets."""

    global _SCHEMA
    if _SCHEMA is None and Secret is not None:
        _SCHEMA = Secret.Schema.new(
            "io.github.mfat.sshpilot",
            Secret.SchemaFlags.NONE,
            {
                "application": Secret.SchemaAttributeType.STRING,
                "type": Secret.SchemaAttributeType.STRING,
                "key_path": Secret.SchemaAttributeType.STRING,
                "host": Secret.SchemaAttributeType.STRING,
                "username": Secret.SchemaAttributeType.STRING,
            },
        )
    return _SCHEMA


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
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
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
    """Background loop that forwards askpass logs to the module logger."""

    forward_askpass_log_to_logger(logger, include_existing=True)

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


def _run_askpass_dialog(key_path: str, log_fn) -> "str | None":
    """Show a GTK4/Adw passphrase dialog. Returns passphrase string or None on cancel."""
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

    try:
        try:
            config_dir = os.path.join(GLib.get_user_config_dir(), "sshpilot")
        except Exception:
            config_dir = os.path.join(os.path.expanduser("~"), ".config", "sshpilot")
        config_file = os.path.join(config_dir, "config.json")
        saved_theme = "default"
        if os.path.exists(config_file):
            try:
                with open(config_file, "r") as f:
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

    app = Adw.Application.new("io.github.mfat.sshpilot.askpass", Gio.ApplicationFlags.FLAGS_NONE)

    def on_activate(app):
        key_name = os.path.basename(key_path) if key_path else "key"
        window = Adw.ApplicationWindow()
        window.set_application(app)
        window.set_title("SSH Pilot")

        dialog = Adw.MessageDialog(
            transient_for=window,
            modal=True,
            heading="Passphrase Required",
            body=f"Please enter the passphrase for key {key_name}:",
        )

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_top(12)
        content_box.set_margin_bottom(12)
        content_box.set_margin_start(12)
        content_box.set_margin_end(12)

        password_entry = Gtk.PasswordEntry()
        password_entry.set_property("placeholder-text", "Passphrase")
        content_box.append(password_entry)

        store_checkbox = Gtk.CheckButton(label="Store passphrase")
        store_checkbox.set_active(False)
        content_box.append(store_checkbox)

        dialog.set_extra_child(content_box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        try:
            password_entry.set_property("activates-default", True)
        except (TypeError, AttributeError):
            pass

        try:
            def on_entry_activate(_entry):
                dialog.emit("response", "ok")
            password_entry.connect("activate", on_entry_activate)
        except (TypeError, AttributeError):
            try:
                key_controller = Gtk.EventControllerKey()
                def on_key_pressed(_ctrl, keyval, _keycode, _state):
                    if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                        dialog.emit("response", "ok")
                        return True
                    return False
                key_controller.connect("key-pressed", on_key_pressed)
                password_entry.add_controller(key_controller)
            except Exception:
                pass

        def on_response(dlg, response_id):
            if response_id == "ok":
                passphrase_result[0] = password_entry.get_text()
                if store_checkbox.get_active() and key_path and passphrase_result[0]:
                    try:
                        store_passphrase(key_path, passphrase_result[0])
                    except Exception:
                        pass
            window.close()
            app.quit()

        dialog.connect("response", on_response)
        dialog.present()
        password_entry.grab_focus()

    app.connect("activate", on_activate)
    app.run(None)
    return passphrase_result[0]


def handle_askpass_cli(prompt: str) -> int:
    """Handle --askpass CLI mode (re-invoked by SSH as SSH_ASKPASS handler).

    Looks up stored passphrases using the app's own environment (keyring,
    libsecret) and falls back to a GTK dialog. Prints the passphrase to
    stdout and returns 0 on success, 1 on failure.
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
        return 1

    if "passphrase" not in pl:
        _log("ASKPASS: No passphrase found, exiting with code 1")
        return 1

    key_path = _extract_key_path(prompt)
    if not key_path:
        _log("ASKPASS: Could not extract key path from prompt")
        return 1

    _log(f"ASKPASS: Extracted key path: {key_path}")

    # Check one-shot session passphrase written by the main app
    session_passphrase_file = os.environ.get("SSHPILOT_SESSION_PASSPHRASE_FILE", "")
    if session_passphrase_file and os.path.exists(session_passphrase_file):
        try:
            with open(session_passphrase_file, "r", encoding="utf-8") as f:
                session_passphrase = f.read().strip()
            if session_passphrase:
                _log("ASKPASS: Found session passphrase from secure temp file")
                print(session_passphrase)
                try:
                    os.unlink(session_passphrase_file)
                except Exception:
                    pass
                return 0
        except Exception as exc:
            _log(f"ASKPASS: Error reading session passphrase file: {exc}")

    # Check keyring / libsecret for stored passphrases
    for candidate in _get_key_path_lookup_candidates(key_path):
        passphrase = lookup_passphrase(candidate)
        if passphrase:
            _log(f"ASKPASS: Found passphrase for {candidate}")
            print(passphrase)
            _log("ASKPASS: Returning passphrase and exiting with code 0")
            return 0
        _log(f"ASKPASS: No passphrase found for {candidate}")

    # Fall back to interactive GUI dialog
    passphrase = _run_askpass_dialog(key_path, _log)
    if passphrase is not None:
        _log("ASKPASS: User entered passphrase in GUI dialog")
        print(passphrase)
        return 0

    _log("ASKPASS: No passphrase found, exiting with code 1")
    return 1


def store_passphrase(key_path: str, passphrase: str) -> bool:
    """Store a key passphrase using keyring (macOS) or libsecret (Linux)."""

    if not key_path:
        return False

    canonical_path = _normalize_key_path_for_storage(key_path)

    # Try keyring first (macOS)
    if keyring and is_macos():
        try:
            keyring.set_password('sshPilot', canonical_path, passphrase)
            return True
        except Exception as e:
            logger.debug(f"Failed to store passphrase in keyring: {e}")
            return False

    # Fall back to libsecret (Linux)
    schema = get_secret_schema()
    if not schema:
        return False
    attributes = {
        "application": "sshPilot",
        "type": "key_passphrase",
        "key_path": canonical_path,
    }
    try:
        Secret.password_store_sync(
            schema,
            attributes,
            Secret.COLLECTION_DEFAULT,
            f"SSH Key Passphrase: {os.path.basename(canonical_path)}",
            passphrase,
            None,
        )
        return True
    except Exception:
        return False


def lookup_passphrase(key_path: str) -> str:
    """Look up a key passphrase using keyring (macOS) or libsecret (Linux)."""

    candidates = _get_key_path_lookup_candidates(key_path)
    if not candidates:
        return ""

    # Try keyring first (macOS)
    if keyring and is_macos():
        for candidate in candidates:
            try:
                passphrase = keyring.get_password('sshPilot', candidate)
                # keyring can return None or empty string when not found
                if passphrase:
                    return passphrase
            except Exception as e:
                logger.debug(f"Failed to retrieve passphrase from keyring for {candidate}: {e}")
                # Continue to next candidate instead of breaking
                continue

    # Fall back to libsecret (Linux)
    schema = get_secret_schema()
    if not schema:
        return ""
    for candidate in candidates:
        attributes = {
            "application": "sshPilot",
            "type": "key_passphrase",
            "key_path": candidate,
        }
        try:
            result = Secret.password_lookup_sync(schema, attributes, None)
        except Exception:
            continue
        if result:
            return result
    return ""


def clear_passphrase(key_path: str) -> bool:
    """Remove a stored key passphrase using keyring (macOS) or libsecret (Linux)."""

    candidates = _get_key_path_lookup_candidates(key_path)
    if not candidates:
        return False

    # Try keyring first (macOS)
    if keyring and is_macos():
        removed_any = False
        for candidate in candidates:
            try:
                keyring.delete_password('sshPilot', candidate)
                removed_any = True
            except Exception as e:
                logger.debug(f"Failed to delete passphrase from keyring: {e}")
        if removed_any:
            return True

    # Fall back to libsecret (Linux)
    schema = get_secret_schema()
    if not schema:
        return False
    removed_any = False
    for candidate in candidates:
        attributes = {
            "application": "sshPilot",
            "type": "key_passphrase",
            "key_path": candidate,
        }
        try:
            if Secret.password_clear_sync(schema, attributes, None):
                removed_any = True
        except Exception:
            continue
    return removed_any

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
            f.write("from sshpilot.askpass_utils import handle_askpass_cli\n")
            f.write("prompt = sys.argv[1] if len(sys.argv) > 1 else ''\n")
            f.write("sys.exit(handle_askpass_cli(prompt))\n")
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

def ensure_key_in_agent(key_path: str) -> bool:
    """Ensure SSH key is loaded in ssh-agent with passphrase"""
    if not os.path.isfile(key_path):
        logger.error(f"Key file not found: {key_path}")
        return False
    
    # Check if key is already in ssh-agent
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
    
    # Add key to ssh-agent using our askpass script
    env = get_ssh_env_with_askpass("force")
    
    try:
        result = subprocess.run(
            ['ssh-add', key_path],
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

def prepare_key_for_connection(key_path: str) -> bool:
    """Prepare SSH key for connection by ensuring it's in ssh-agent"""
    return ensure_key_in_agent(key_path)

def get_scp_ssh_options() -> list:
    """Get SSH options for SCP operations with passphrased keys"""
    return [
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "IdentitiesOnly=yes",
    ]

def connect_ssh_with_key(host: str, username: str, key_path: str, command: str = None) -> subprocess.CompletedProcess:
    """Connect via SSH with proper key handling using ssh_connection_builder"""
    # Ensure key is loaded in ssh-agent
    if not ensure_key_in_agent(key_path):
        raise Exception(f"Failed to load key {key_path} into SSH agent")
    
    try:
        from .ssh_connection_builder import build_ssh_connection, ConnectionContext
        
        # Create a minimal connection object
        class SSHConnection:
            def __init__(self, host, username, key_path):
                self.hostname = host
                self.host = host
                self.nickname = host
                self.username = username
                self.port = 22
                self.keyfile = key_path
                self.key_select_mode = 1  # Use specific key
                self.auth_method = 0  # Key-based
                self.extra_ssh_config = None
                self.identity_agent_disabled = False
        
        connection = SSHConnection(host, username, key_path)
        
        # Build SSH connection command using ssh_connection_builder
        ctx = ConnectionContext(
            connection=connection,
            connection_manager=None,
            config=None,
            command_type='ssh',
            extra_args=[],
            port_forwarding_rules=None,
            remote_command=command,
            local_command=None,
            extra_ssh_config=None,
            known_hosts_path=None,
            native_mode=False,
            quick_connect_mode=False,
            quick_connect_command=None,
        )
        
        ssh_conn_cmd = build_ssh_connection(ctx)
        ssh_cmd = ssh_conn_cmd.command
        env = ssh_conn_cmd.env.copy()
        
        # Ensure askpass is set for passphrase handling
        askpass_env = get_ssh_env_with_askpass("force")
        env.update(askpass_env)
        
        return subprocess.run(ssh_cmd, env=env, capture_output=True, text=True)
    except Exception as e:
        # Fallback to original implementation
        logger.warning(f"Failed to use ssh_connection_builder, falling back: {e}")
        env = get_ssh_env_with_askpass("force")
        ssh_cmd = ["ssh", "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no"]
        if command:
            ssh_cmd.extend([f"{username}@{host}", command])
        else:
            ssh_cmd.append(f"{username}@{host}")
        return subprocess.run(ssh_cmd, env=env, capture_output=True, text=True)
