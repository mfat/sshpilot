# askpass_utils.py
import os, tempfile, atexit, shutil

_ASKPASS_DIR = None
_ASKPASS_SCRIPT = None

def ensure_passphrase_askpass() -> str:
    global _ASKPASS_DIR, _ASKPASS_SCRIPT
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

    script_body = '''#!/usr/bin/env python3
import sys, re
try:
    import secretstorage
except Exception:
    secretstorage = None

def get_passphrase(key_path: str) -> str:
    if secretstorage is None:
        return ""
    try:
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection and collection.is_locked():
            collection.unlock()
        items = list(collection.search_items({
            "application": "sshPilot",
            "type": "key_passphrase",
            "key_path": key_path
        }))
        if items:
            return items[0].get_secret().decode("utf-8")
    except Exception:
        pass
    return ""

if __name__ == "__main__":
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    pl = prompt.lower()
    if "passphrase" in pl:
        # Handle: "Enter passphrase for key '/path':" and "Enter passphrase for '/path':"
        m = re.search(r'passphrase.*for (?:key )?["\']?([^"\':]+)["\']?:', prompt, re.IGNORECASE)
        if m:
            key_path = m.group(1).strip(' \'"')
            # try raw, ~-expanded, and fully resolved path; de-dup order
            import os
            candidates = [key_path, os.path.expanduser(key_path), os.path.realpath(os.path.expanduser(key_path))]
            seen = set()
            for key_path in [c for c in candidates if not (c in seen or seen.add(c))]:
                p = get_passphrase(key_path)
                if p:
                    print(p)
                    sys.exit(0)
    # Not a passphrase prompt or not found
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
    """Force regeneration of the askpass script (useful after fixes)"""
    global _ASKPASS_DIR, _ASKPASS_SCRIPT
    _ASKPASS_SCRIPT = None
    if _ASKPASS_DIR:
        try:
            shutil.rmtree(_ASKPASS_DIR, ignore_errors=True)
        except Exception:
            pass
        _ASKPASS_DIR = None
    return ensure_passphrase_askpass()

def get_askpass_env(require: str = "prefer") -> dict:
    env = os.environ.copy()
    env["SSH_ASKPASS"] = ensure_passphrase_askpass()
    env["SSH_ASKPASS_REQUIRE"] = require  # use "prefer" for GUI apps
    return env

def get_ssh_env_with_askpass(require: str = "prefer") -> dict:
    """Get SSH environment with askpass for passphrase handling.
    This function is used for general askpass environment setup.
    """
    env = os.environ.copy()
    env["SSH_ASKPASS"] = ensure_passphrase_askpass()
    env["SSH_ASKPASS_REQUIRE"] = require  # use "prefer" for GUI apps
    return env

def get_ssh_env_with_askpass_for_password(host: str, username: str) -> dict:
    """Get SSH environment with askpass for password authentication.
    This function is used when we want to use the askpass mechanism for password prompts.
    """
    env = os.environ.copy()
    env["SSH_ASKPASS"] = ensure_passphrase_askpass()
    env["SSH_ASKPASS_REQUIRE"] = "prefer"  # use "prefer" for GUI apps
    return env

def get_scp_ssh_options() -> list:
    """Get SSH options for SCP operations with passphrased keys.
    These options force public key authentication and prevent password fallback.
    """
    return [
        "-o", "PreferredAuthentications=publickey",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "IdentitiesOnly=yes",   # if you pass -i
    ]

def get_ssh_env_with_forced_askpass() -> dict:
    """Get SSH environment with forced askpass for passphrase handling.
    This function is used for SCP operations where we want to force askpass
    and prevent fallback to password authentication.
    """
    env = os.environ.copy()
    env["SSH_ASKPASS"] = ensure_passphrase_askpass()
    env["SSH_ASKPASS_REQUIRE"] = "force"  # force askpass for passphrased keys
    return env
