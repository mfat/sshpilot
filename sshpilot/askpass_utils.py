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

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Extract key path from the prompt text
        prompt = sys.argv[1]
        # Look for the key path in the prompt (e.g., "Enter passphrase for /path/to/key: ")
        match = re.search(r'for ([^:]+):', prompt)
        if match:
            key_path = match.group(1)
            print(get_passphrase(key_path))
        else:
            # Fallback: try to use the argument as-is
            print(get_passphrase(prompt))
""")
        os.chmod(python_script, 0o700)
        
        # Create the shell wrapper
        with open(askpass_script, "w") as f:
            f.write(f"#!/bin/sh\n{python_script} \"$1\"\n")
        os.chmod(askpass_script, 0o700)
        
        logger.debug(f"Created SSH_ASKPASS script: {askpass_script}")
    
    return askpass_script

def create_temp_askpass_script(password: str) -> str:
    """Create a temporary SSH_ASKPASS script for a specific password"""
    import tempfile
    
    # Create temporary script file
    script_fd, script_path = tempfile.mkstemp(prefix='ssh_askpass_', suffix='.sh')
    
    # Write the script content - use printf for safer password handling
    script_content = f"""#!/bin/sh
printf '%s' '{password.replace("'", "'\"'\"'")}'
"""
    os.write(script_fd, script_content.encode('utf-8'))
    os.close(script_fd)
    
    # Make the script executable
    os.chmod(script_path, 0o700)
    
    return script_path

def get_ssh_env_with_askpass() -> dict:
    """Get environment variables configured for SSH_ASKPASS usage"""
    env = os.environ.copy()
    # Don't create the script here - let it be created when actually needed
    askpass_script = os.path.expanduser("~/.local/bin/sshpilot-askpass")
    env['SSH_ASKPASS'] = askpass_script
    env['SSH_ASKPASS_REQUIRE'] = 'force'
    env['DISPLAY'] = ':0'
    return env
