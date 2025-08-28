# askpass_utils.py
import os, tempfile, atexit, shutil, subprocess, logging

logger = logging.getLogger(__name__)

_ASKPASS_DIR = None
_ASKPASS_SCRIPT = None

def ensure_passphrase_askpass() -> str:
    """Ensure the askpass script exists and return its path"""
    global _ASKPASS_DIR, _ASKPASS_SCRIPT
    
    if _ASKPASS_SCRIPT and os.path.exists(_ASKPASS_SCRIPT):
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

    script_body = r'''#!/usr/bin/env python3
import sys, re, os
try:
    import secretstorage
except Exception:
    secretstorage = None

def get_passphrase(key_path: str) -> str:
    """Retrieve passphrase from secretstorage"""
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
    # Disable GNOME keyring interference but keep D-Bus for secretstorage
    os.environ["GNOME_KEYRING_CONTROL"] = ""
    os.environ["GNOME_KEYRING_PID"] = ""
    os.environ["GNOME_KEYRING_SOCKET"] = ""
    # Don't disable DBUS_SESSION_BUS_ADDRESS - secretstorage needs it
    
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    pl = prompt.lower()
    
    # Debug logging
    try:
        with open("/tmp/sshpilot-askpass.log", "a") as f:
            f.write(f"ASKPASS called with prompt: {prompt}\n")
    except Exception:
        pass
    
    # Never handle password prompts in this helper
    if "password" in pl and "passphrase" not in pl:
        try:
            with open("/tmp/sshpilot-askpass.log", "a") as f:
                f.write("ASKPASS: Ignoring password prompt\n")
        except Exception:
            pass
        sys.exit(1)
    
    if "passphrase" in pl:
        key_path = extract_key_path(prompt)
        if key_path:
            try:
                with open("/tmp/sshpilot-askpass.log", "a") as f:
                    f.write(f"ASKPASS: Extracted key path: {key_path}\n")
            except Exception:
                pass
            
            # Try multiple path variations
            candidates = [
                key_path, 
                os.path.expanduser(key_path), 
                os.path.realpath(os.path.expanduser(key_path))
            ]
            seen = set()
            for candidate in [c for c in candidates if not (c in seen or seen.add(c))]:
                passphrase = get_passphrase(candidate)
                if passphrase:
                    try:
                        with open("/tmp/sshpilot-askpass.log", "a") as f:
                            f.write(f"ASKPASS: Found passphrase for {candidate}\n")
                    except Exception:
                        pass
                    print(passphrase)
                    sys.exit(0)
                else:
                    try:
                        with open("/tmp/sshpilot-askpass.log", "a") as f:
                            f.write(f"ASKPASS: No passphrase found for {candidate}\n")
                    except Exception:
                        pass
    
    # Not a passphrase prompt or not found
    try:
        with open("/tmp/sshpilot-askpass.log", "a") as f:
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
    env = os.environ.copy()
    env["SSH_ASKPASS"] = ensure_passphrase_askpass()
    env["SSH_ASKPASS_REQUIRE"] = require
    # Ensure DISPLAY is set for SSH_ASKPASS to work properly
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    # Disable GNOME keyring interference with SSH but keep D-Bus for secretstorage
    env["GNOME_KEYRING_CONTROL"] = ""
    env["GNOME_KEYRING_PID"] = ""
    env["GNOME_KEYRING_SOCKET"] = ""
    # Don't disable DBUS_SESSION_BUS_ADDRESS - secretstorage needs it
    return env

def get_ssh_env_with_askpass_for_password(host: str, username: str) -> dict:
    """Get SSH environment with askpass for password authentication"""
    env = get_ssh_env_with_askpass("force")
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