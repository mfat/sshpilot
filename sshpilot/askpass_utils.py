"""
SSH_ASKPASS utilities for secure passphrase handling
"""

import os
import logging

logger = logging.getLogger(__name__)

def ensure_askpass_script() -> str:
    """Ensure the SSH_ASKPASS script exists and return its path"""
    askpass_script = os.path.expanduser("~/.local/bin/sshpilot-askpass")
    if not os.path.isfile(askpass_script):
        # Create it once
        script_dir = os.path.dirname(askpass_script)
        if not os.path.exists(script_dir):
            os.makedirs(script_dir, mode=0o700)
        
        # Get the current script's directory to find the sshpilot module
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        
        # Create a Python script file to avoid quote escaping issues
        python_script = os.path.join(script_dir, "sshpilot-askpass.py")
        with open(python_script, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import sys
import os
import re
sys.path.insert(0, "{project_root}")

import secretstorage

def get_passphrase(key_path):
    try:
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection and collection.is_locked():
            collection.unlock()
        
        items = list(collection.search_items({{
            'application': 'sshPilot',
            'type': 'key_passphrase',
            'key_path': key_path
        }}))
        
        if items:
            return items[0].get_secret().decode('utf-8')
        return ""
    except Exception as e:
        return ""

def get_password(host, username):
    try:
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection and collection.is_locked():
            collection.unlock()
        
        items = list(collection.search_items({{
            'application': 'sshPilot',
            'type': 'password',
            'host': host,
            'username': username
        }}))
        
        if items:
            return items[0].get_secret().decode('utf-8')
        return ""
    except Exception as e:
        return ""

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Extract key path from the prompt text
        prompt = sys.argv[1]
        
        # Check if this is a password prompt (not a passphrase prompt)
        if "password" in prompt.lower() and "passphrase" not in prompt.lower():
            # This is likely a password prompt, try to extract host/username from environment
            # or use a fallback approach
            host = os.environ.get('SSHPILOT_HOST', '')
            username = os.environ.get('SSHPILOT_USERNAME', '')
            if host and username:
                password = get_password(host, username)
                if password:
                    print(password)
                    sys.exit(0)
        
        # Look for the key path in the prompt (e.g., "Enter passphrase for /path/to/key: ")
        match = re.search(r'for ([^:]+):', prompt)
        if match:
            key_path = match.group(1)
            passphrase = get_passphrase(key_path)
            if passphrase:
                print(passphrase)
                sys.exit(0)
        
        # Fallback: try to use the argument as-is for key passphrase
        passphrase = get_passphrase(prompt)
        if passphrase:
            print(passphrase)
            sys.exit(0)
        
        # If we reach here, no password/passphrase was found:
        print("", end="")        # ensure no stray text
        sys.exit(1)              # <— signal failure so ssh can fall back to TTY
""")
        os.chmod(python_script, 0o700)
        
        # Create the shell wrapper
        with open(askpass_script, "w") as f:
            f.write(f"#!/bin/sh\n{python_script} \"$1\"\n")
        os.chmod(askpass_script, 0o700)
        
        logger.debug(f"Created SSH_ASKPASS script: {askpass_script}")
    
    return askpass_script

def get_ssh_env_with_askpass() -> dict:
    """Get environment variables configured for SSH_ASKPASS usage"""
    env = os.environ.copy()
    # Don't create the script here - let it be created when actually needed
    askpass_script = os.path.expanduser("~/.local/bin/sshpilot-askpass")
    env['SSH_ASKPASS'] = askpass_script
    env['SSH_ASKPASS_REQUIRE'] = 'force'    # ensure askpass even if a TTY exists
    # no DISPLAY needed for our headless askpass
    return env

def get_ssh_env_with_askpass_for_password(host: str, username: str) -> dict:
    """Get environment variables configured for SSH_ASKPASS usage with password context"""
    env = get_ssh_env_with_askpass()
    # Set host and username context for password retrieval
    env['SSHPILOT_HOST'] = host
    env['SSHPILOT_USERNAME'] = username
    return env
