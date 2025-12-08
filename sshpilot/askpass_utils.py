# askpass_utils.py
import atexit
import logging
import os
import shutil
import subprocess
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
            except Exception as e:
                logger.debug(f"Failed to retrieve passphrase from keyring: {e}")
                break

            if passphrase:
                return passphrase

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
    """Ensure the askpass script exists and return its path"""
    global _ASKPASS_DIR, _ASKPASS_SCRIPT

    logger.debug("Ensuring askpass script is available")

    if _ASKPASS_SCRIPT and os.path.exists(_ASKPASS_SCRIPT):
        logger.debug(f"Using cached askpass script at {_ASKPASS_SCRIPT}")
        return _ASKPASS_SCRIPT
    
    # Clear cache to force regeneration of the script
    _ASKPASS_SCRIPT = None
    if _ASKPASS_DIR:
        try:
            shutil.rmtree(_ASKPASS_DIR, ignore_errors=True)
        except Exception:
            pass
        _ASKPASS_DIR = None

    _ASKPASS_DIR = tempfile.mkdtemp(prefix="sshpilot-askpass-")
    os.chmod(_ASKPASS_DIR, 0o700)
    path = os.path.join(_ASKPASS_DIR, "askpass.py")
    logger.debug(f"Generating askpass script at {path}")

    script_body = r'''#!/usr/bin/env python3
import sys, re, os, platform, tempfile
LOG_DIR = (
    os.environ.get("SSHPILOT_ASKPASS_LOG_DIR")
    or os.environ.get("XDG_RUNTIME_DIR")
    or tempfile.gettempdir()
)
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    pass
LOG_PATH = os.path.join(LOG_DIR, "sshpilot-askpass.log")
try:
    import gi
    gi.require_version('Secret', '1')
    from gi.repository import Secret
except Exception:
    Secret = None
try:
    import keyring
except Exception:
    keyring = None


# Log availability of keyring and libsecret
try:
    with open(LOG_PATH, "a") as f:
        f.write(f"ASKPASS: keyring {'available' if keyring else 'unavailable'}, libsecret {'available' if Secret else 'unavailable'}\n")
except Exception:
    pass

def get_passphrase(key_path: str) -> str:
    """Retrieve passphrase from keyring or libsecret"""
    # Try keyring first (macOS)
    if keyring and platform.system() == 'Darwin':
        try:
            with open(LOG_PATH, "a") as f:
                f.write(f"ASKPASS: Trying keyring for {key_path}\n")
            passphrase = keyring.get_password('sshPilot', key_path)
            if passphrase:
                try:
                    with open(LOG_PATH, "a") as f:
                        f.write("ASKPASS: Retrieved passphrase from keyring\n")
                except Exception:
                    pass
                return passphrase
            else:
                try:
                    with open(LOG_PATH, "a") as f:
                        f.write("ASKPASS: No passphrase in keyring\n")
                except Exception:
                    pass
        except Exception as e:
            try:
                with open(LOG_PATH, "a") as f:
                    f.write(f"ASKPASS: keyring error: {e}\n")
            except Exception:
                pass

    # Fall back to libsecret (Linux)
    if Secret is None:
        try:
            with open(LOG_PATH, "a") as f:
                f.write("ASKPASS: libsecret module not available\n")
        except Exception:
            pass
        return ""
    try:
        with open(LOG_PATH, "a") as f:
            f.write("ASKPASS: Trying libsecret\n")
        schema = Secret.Schema.new("io.github.mfat.sshpilot", Secret.SchemaFlags.NONE, {
            "application": Secret.SchemaAttributeType.STRING,
            "type": Secret.SchemaAttributeType.STRING,
            "key_path": Secret.SchemaAttributeType.STRING,
            "host": Secret.SchemaAttributeType.STRING,
            "username": Secret.SchemaAttributeType.STRING,
        })

        attributes = {
            "application": "sshPilot",
            "type": "key_passphrase",
            "key_path": key_path,
        }
        secret = Secret.password_lookup_sync(schema, attributes, None)
        if secret is not None:

            try:
                with open(LOG_PATH, "a") as f:
                    f.write("ASKPASS: Retrieved passphrase from libsecret\n")
            except Exception:
                pass
            return secret
        else:
            try:
                with open(LOG_PATH, "a") as f:
                    f.write("ASKPASS: No matching libsecret item found\n")
            except Exception:
                pass
    except Exception as e:
        try:
            with open(LOG_PATH, "a") as f:
                f.write(f"ASKPASS: libsecret error: {e}\n")
        except Exception:
            pass
    return ""

def extract_key_path(prompt: str) -> str:
    """Extract key path from SSH passphrase prompt"""
    # Common SSH passphrase prompt formats:
    # "Enter passphrase for key '/path/to/key':"
    # "Enter passphrase for '/path/to/key':"
    # "Enter passphrase for key /path/to/key:"
    # "Enter passphrase for /path/to/key:"
    
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
            # Remove any remaining quotes
            if (key_path.startswith('"') and key_path.endswith('"')) or \
               (key_path.startswith("'") and key_path.endswith("'")):
                key_path = key_path[1:-1]
            return key_path
    
    return ""

if __name__ == "__main__":
    # Disable GNOME keyring interference but keep D-Bus for libsecret
    os.environ["GNOME_KEYRING_CONTROL"] = ""
    os.environ["GNOME_KEYRING_PID"] = ""
    os.environ["GNOME_KEYRING_SOCKET"] = ""
    # Don't disable DBUS_SESSION_BUS_ADDRESS - libsecret needs it
    
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    pl = prompt.lower()
    
    # Debug logging
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"ASKPASS called with prompt: {prompt}\n")
    except Exception:
        pass
    
    # Never handle password prompts in this helper
    if "password" in pl and "passphrase" not in pl:
        try:
            with open(LOG_PATH, "a") as f:
                f.write("ASKPASS: Ignoring password prompt\n")
        except Exception:
            pass
        sys.exit(1)
    
    if "passphrase" in pl:
        key_path = extract_key_path(prompt)
        if key_path:
            try:
                with open(LOG_PATH, "a") as f:
                    f.write(f"ASKPASS: Extracted key path: {key_path}\n")
            except Exception:
                pass
            
            def _iter_candidates(original):
                seen = set()

                expanded = os.path.expanduser(original)
                canonical = os.path.realpath(expanded)

                home = os.path.expanduser("~")

                for path in (canonical, expanded):
                    if path and path not in seen:
                        seen.add(path)
                        yield path

                    try:
                        rel = os.path.relpath(path, home)
                    except Exception:
                        rel = None

                    if rel in (".", os.curdir):
                        alias = "~"
                    elif rel and not str(rel).startswith(".."):
                        alias = os.path.join("~", rel)
                    else:
                        alias = None

                    if alias and alias not in seen:
                        seen.add(alias)
                        yield alias

                if original and original not in seen:
                    seen.add(original)
                    yield original

            for candidate in _iter_candidates(key_path):
                passphrase = get_passphrase(candidate)
                if passphrase:
                    try:
                        with open(LOG_PATH, "a") as f:
                            f.write(f"ASKPASS: Found passphrase for {candidate}\n")
                    except Exception:
                        pass
                    print(passphrase)
                    try:
                        with open(LOG_PATH, "a") as f:
                            f.write("ASKPASS: Returning passphrase and exiting with code 0\n")
                    except Exception:
                        pass
                    sys.exit(0)
                else:
                    try:
                        with open(LOG_PATH, "a") as f:
                            f.write(f"ASKPASS: No passphrase found for {candidate}\n")
                    except Exception:
                        pass
    
    # Not a passphrase prompt or not found
    try:
        with open(LOG_PATH, "a") as f:
            f.write("ASKPASS: No passphrase found, exiting with code 1\n")
    except Exception:
        pass
    sys.exit(1)
'''
    with open(path, "w", encoding="utf-8") as f:
        f.write(script_body)
    os.chmod(path, 0o700)

    def _cleanup():
        try:
            if _ASKPASS_DIR:
                shutil.rmtree(_ASKPASS_DIR, ignore_errors=True)
        except Exception:
            pass
    atexit.register(_cleanup)
    _ASKPASS_SCRIPT = path
    return path

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
    """Connect via SSH with proper key handling"""
    # Ensure key is loaded in ssh-agent
    if not ensure_key_in_agent(key_path):
        raise Exception(f"Failed to load key {key_path} into SSH agent")
    
    # Get SSH environment with askpass
    env = get_ssh_env_with_askpass("force")
    
    # Build SSH command
    ssh_cmd = ["ssh", "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no"]
    
    if command:
        ssh_cmd.extend([f"{username}@{host}", command])
    else:
        ssh_cmd.append(f"{username}@{host}")
    
    return subprocess.run(ssh_cmd, env=env, capture_output=True, text=True)
