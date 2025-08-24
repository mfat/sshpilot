"""
SSH utilities for building consistent SSH options across the application
"""

import os
import logging
import subprocess
import tempfile
from typing import List, Optional, Tuple

try:
    import gi
    gi.require_version('Vte', '3.91')
    from gi.repository import Vte, GLib
except ImportError:
    Vte = None
    GLib = None

logger = logging.getLogger(__name__)

def build_connection_ssh_options(connection, config=None, for_ssh_copy_id=False):
    """Build SSH options that match the exact connection settings used for SSH.
    
    This function replicates the SSH option building logic from terminal.py
    to ensure ssh-copy-id and scp use the same settings as SSH connections.
    
    Args:
        connection: The connection object
        config: Optional config object
        for_ssh_copy_id: If True, filter out options not supported by ssh-copy-id
    """
    options = []
    
    # Read SSH behavior from config with sane defaults (same as terminal.py)
    try:
        if config is None:
            from .config import Config
            config = Config()
        ssh_cfg = config.get_ssh_config() if hasattr(config, 'get_ssh_config') else {}
    except Exception:
        ssh_cfg = {}
    
    apply_adv = bool(ssh_cfg.get('apply_advanced', False))
    connect_timeout = int(ssh_cfg.get('connection_timeout', 10)) if apply_adv else None
    connection_attempts = int(ssh_cfg.get('connection_attempts', 1)) if apply_adv else None
    keepalive_interval = int(ssh_cfg.get('keepalive_interval', 30)) if apply_adv else None
    keepalive_count = int(ssh_cfg.get('keepalive_count_max', 3)) if apply_adv else None
    strict_host = str(ssh_cfg.get('strict_host_key_checking', '')) if apply_adv else ''
    auto_add_host_keys = bool(ssh_cfg.get('auto_add_host_keys', True))
    batch_mode = bool(ssh_cfg.get('batch_mode', False)) if apply_adv else False
    compression = bool(ssh_cfg.get('compression', True)) if apply_adv else False

    # Determine auth method from connection (same as terminal.py)
    password_auth_selected = False
    try:
        # In our UI: 0 = key-based, 1 = password
        password_auth_selected = (getattr(connection, 'auth_method', 0) == 1)
    except Exception:
        password_auth_selected = False

    # Apply advanced args only when user explicitly enabled them (same as terminal.py)
    if apply_adv:
        # Only enable BatchMode when NOT doing password auth (BatchMode disables prompts)
        if batch_mode and not password_auth_selected:
            options.extend(['-o', 'BatchMode=yes'])
        if connect_timeout is not None:
            options.extend(['-o', f'ConnectTimeout={connect_timeout}'])
        if connection_attempts is not None:
            options.extend(['-o', f'ConnectionAttempts={connection_attempts}'])
        if keepalive_interval is not None:
            options.extend(['-o', f'ServerAliveInterval={keepalive_interval}'])
        if keepalive_count is not None:
            options.extend(['-o', f'ServerAliveCountMax={keepalive_count}'])
        if strict_host:
            options.extend(['-o', f'StrictHostKeyChecking={strict_host}'])
        if compression and not for_ssh_copy_id:
            options.append('-C')

    # Apply auto-add host keys policy even when advanced block is off (same as terminal.py)
    try:
        if (not strict_host) and auto_add_host_keys:
            options.extend(['-o', 'StrictHostKeyChecking=accept-new'])
    except Exception:
        pass

    # Ensure SSH exits immediately on failure rather than waiting in background (same as terminal.py)
    options.extend(['-o', 'ExitOnForwardFailure=yes'])
    
    # Default to accepting new host keys non-interactively on fresh installs (same as terminal.py)
    try:
        if (not strict_host) and auto_add_host_keys:
            options.extend(['-o', 'StrictHostKeyChecking=accept-new'])
    except Exception:
        pass
    
    # Only add verbose flag if explicitly enabled in config (same as terminal.py)
    # Note: ssh-copy-id doesn't support -v flags, only -x for debug
    try:
        verbosity = int(ssh_cfg.get('verbosity', 0))
        debug_enabled = bool(ssh_cfg.get('debug_enabled', False))
        v = max(0, min(3, verbosity))
        if not for_ssh_copy_id:
            for _ in range(v):
                options.append('-v')
        # Map verbosity to LogLevel to ensure messages are not suppressed by defaults
        if v == 1:
            options.extend(['-o', 'LogLevel=VERBOSE'])
        elif v == 2:
            options.extend(['-o', 'LogLevel=DEBUG2'])
        elif v >= 3:
            options.extend(['-o', 'LogLevel=DEBUG3'])
        elif debug_enabled:
            options.extend(['-o', 'LogLevel=DEBUG'])
    except Exception as e:
        logger.warning(f"Could not check SSH verbosity/debug settings: {e}")
    
    # Add key file/options only for key-based auth (same as terminal.py)
    # Note: ssh-copy-id already specifies the key with -i, so we don't add it again
    if not password_auth_selected:
        if hasattr(connection, 'keyfile') and connection.keyfile and \
           os.path.isfile(connection.keyfile) and \
           not connection.keyfile.startswith('Select key file'):

            try:
                key_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
            except Exception:
                key_mode = 0

            if not for_ssh_copy_id or key_mode == 1:
                options.extend(['-i', connection.keyfile])

            if key_mode == 1 and not for_ssh_copy_id:
                try:
                    options.extend(['-o', 'IdentitiesOnly=yes'])
                except Exception:
                    pass
    else:
        # For ssh-copy-id, don't restrict authentication methods - let it try all keys first
        if not for_ssh_copy_id:
            # Prefer password/interactive methods when user chose password auth (same as terminal.py)
            options.extend(['-o', 'PreferredAuthentications=password,keyboard-interactive'])
            options.extend(['-o', 'PubkeyAuthentication=no'])
    
    # Add X11 forwarding if enabled (same as terminal.py) - not supported by ssh-copy-id
    if hasattr(connection, 'x11_forwarding') and connection.x11_forwarding and not for_ssh_copy_id:
        options.append('-X')
    
    return options


def custom_ssh_copy_id(connection, public_key_path, config=None, connection_manager=None, terminal_widget=None):
    """
    Custom ssh-copy-id implementation that first tries key-based auth and falls
    back to password authentication when necessary.

    Args:
        connection: The connection object
        public_key_path: Path to the public key to copy
        config: Optional config object
        connection_manager: Optional connection manager for password retrieval
        terminal_widget: Optional terminal widget for interactive password prompts

    Returns:
        Tuple of (success: bool, message: str)
    """
    logger.info(
        f"Custom ssh-copy-id: Copying {public_key_path} to {connection.username}@{connection.host}"
    )

    try:
        # Verify public key file exists
        if not os.path.exists(public_key_path):
            return False, f"Public key file not found: {public_key_path}"

        # Read the public key content
        with open(public_key_path, 'r') as f:
            public_key_content = f.read().strip()

        if not public_key_content:
            return False, f"Public key file is empty: {public_key_path}"

        # Build base SSH options
        ssh_options = build_connection_ssh_options(connection, config, for_ssh_copy_id=True)

        # Add port if specified
        if hasattr(connection, 'port') and connection.port != 22:
            ssh_options.extend(['-p', str(connection.port)])

        target = f"{connection.username}@{connection.host}"

        # Retrieve saved password if available
        saved_password = None
        if connection_manager and hasattr(connection_manager, 'get_password'):
            try:
                saved_password = connection_manager.get_password(
                    connection.host, connection.username
                )
                if saved_password:
                    logger.info(
                        f"Custom ssh-copy-id: Found saved password for {connection.username}@{connection.host}"
                    )
                else:
                    logger.debug(
                        f"Custom ssh-copy-id: No saved password found for {connection.username}@{connection.host}"
                    )
            except Exception as e:
                logger.debug(
                    f"Custom ssh-copy-id: Could not retrieve saved password: {e}"
                )
        else:
            logger.debug(
                "Custom ssh-copy-id: No connection manager or get_password method available"
            )

        # --------------------------------------------------------------
        # Step 1: Gather candidate key files
        candidate_keys = []
        try:
            # Include selected key file when in single-key mode
            if int(getattr(connection, 'key_select_mode', 0) or 0) == 1:
                keyfile = getattr(connection, 'keyfile', None)
                if keyfile and os.path.exists(os.path.expanduser(keyfile)):
                    candidate_keys.append(os.path.expanduser(keyfile))

            # Include any identity files from config
            try:
                cfg = (
                    config
                    if config is not None
                    else __import__('sshpilot.config', fromlist=['Config']).Config()
                )
                ssh_cfg = cfg.get_ssh_config() if hasattr(cfg, 'get_ssh_config') else {}
                id_files = ssh_cfg.get('identity_files') or ssh_cfg.get('identityfile') or []
                if isinstance(id_files, str):
                    id_files = [id_files]
                for path in id_files:
                    expanded = os.path.expanduser(path)
                    if os.path.exists(expanded) and expanded not in candidate_keys:
                        candidate_keys.append(expanded)
            except Exception as e:
                logger.debug(
                    f"Custom ssh-copy-id: Could not load identity files from config: {e}"
                )

            # Include standard default key locations
            default_keys = [
                '~/.ssh/id_ed25519',
                '~/.ssh/id_rsa',
                '~/.ssh/id_ecdsa',
                '~/.ssh/id_ecdsa_sk',
                '~/.ssh/id_ed25519_sk',
                '~/.ssh/id_dsa',
                '~/.ssh/id_xmss',
            ]
            for path in default_keys:
                expanded = os.path.expanduser(path)
                if os.path.exists(expanded) and expanded not in candidate_keys:
                    candidate_keys.append(expanded)
        except Exception as e:
            logger.debug(f"Custom ssh-copy-id: Error gathering key files: {e}")

        logger.debug(f"Custom ssh-copy-id: Candidate keys: {candidate_keys}")

        # --------------------------------------------------------------
        # Step 2: Attempt key-based copy first
        if candidate_keys:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.sh', delete=False
            ) as temp_script:
                script_content = f"""#!/bin/bash
set -e

# Create .ssh directory if it doesn't exist
mkdir -p ~/.ssh

# Set proper permissions
chmod 700 ~/.ssh

# Append the public key to authorized_keys
echo "{public_key_content}" >> ~/.ssh/authorized_keys

# Set proper permissions on authorized_keys
chmod 600 ~/.ssh/authorized_keys

echo "Public key successfully installed"
"""
                temp_script.write(script_content)
                temp_script_path = temp_script.name

            os.chmod(temp_script_path, 0o755)

            key_cmd = ['ssh'] + ssh_options + [
                '-o', 'IdentitiesOnly=yes',
                '-o', 'PreferredAuthentications=publickey',
                '-o', 'PasswordAuthentication=no',
                '-o', 'NumberOfPasswordPrompts=0',
            ]
            for key in candidate_keys:
                key_cmd.extend(['-i', key])
            key_cmd.extend([target, f'bash < {temp_script_path}'])

            logger.debug(
                f"Custom ssh-copy-id: Trying key-only copy: {' '.join(key_cmd)}"
            )
            success, message = _run_ssh_copy_id_with_subprocess(
                key_cmd, temp_script_path
            )
            if success:
                return success, message

        # --------------------------------------------------------------
        # Step 3: Fallback to password/interactive authentication
        logger.debug(
            "Custom ssh-copy-id: Key-only attempt failed, falling back to password methods"
        )

        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.sh', delete=False
        ) as temp_script:
            script_content = f"""#!/bin/bash
set -e

# Create .ssh directory if it doesn't exist
mkdir -p ~/.ssh

# Set proper permissions
chmod 700 ~/.ssh

# Append the public key to authorized_keys
echo "{public_key_content}" >> ~/.ssh/authorized_keys

# Set proper permissions on authorized_keys
chmod 600 ~/.ssh/authorized_keys

echo "Public key successfully installed"
"""
            temp_script.write(script_content)
            temp_script_path = temp_script.name

        os.chmod(temp_script_path, 0o755)

        ssh_cmd = ['ssh'] + ssh_options + [
            '-o', 'PreferredAuthentications=publickey,password,keyboard-interactive',
            '-o', 'PubkeyAuthentication=yes',
            '-o', 'PasswordAuthentication=yes',
            '-o', 'KbdInteractiveAuthentication=yes',
            '-o', 'NumberOfPasswordPrompts=1',
            '-o', 'IdentitiesOnly=no',
            target,
            f'bash < {temp_script_path}',
        ]

        logger.debug(
            f"Custom ssh-copy-id: Executing fallback command: {' '.join(ssh_cmd)}"
        )

        if saved_password and terminal_widget:
            return _run_ssh_copy_id_with_saved_password(
                ssh_cmd, temp_script_path, saved_password, terminal_widget
            )
        elif terminal_widget:
            return _run_ssh_copy_id_with_terminal(
                ssh_cmd, temp_script_path, terminal_widget
            )
        else:
            return _run_ssh_copy_id_with_subprocess(ssh_cmd, temp_script_path)

    except Exception as e:
        logger.error(f"Custom ssh-copy-id: Unexpected error: {e}")
        return False, f"Unexpected error: {str(e)}"


def _run_ssh_copy_id_with_saved_password(ssh_cmd, temp_script_path, saved_password, terminal_widget):
    """Run ssh-copy-id using saved password with sshpass"""
    try:
        logger.info(f"Running ssh-copy-id with saved password using sshpass")
        
        # Check if sshpass is available
        import shutil
        sshpass_path = None
        if shutil.which('sshpass'):
            sshpass_path = 'sshpass'
        elif os.path.exists('/app/bin/sshpass'):
            sshpass_path = '/app/bin/sshpass'
        
        if not sshpass_path:
            logger.warning("sshpass not found, falling back to interactive terminal")
            return _run_ssh_copy_id_with_terminal(ssh_cmd, temp_script_path, terminal_widget)
        
        logger.info(f"Using sshpass at: {sshpass_path}")
        
        # Create a temporary file to store the password securely
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as pw_file:
            pw_file.write(saved_password)
            pw_file_path = pw_file.name
        
        # Set secure permissions on the password file
        os.chmod(pw_file_path, 0o600)
        
        # Build sshpass command
        sshpass_cmd = [sshpass_path, '-f', pw_file_path] + ssh_cmd
        
        if Vte is None or GLib is None:
            logger.error("Vte or GLib not available, falling back to subprocess")
            # Clean up password file before returning
            try:
                os.unlink(pw_file_path)
            except Exception:
                pass
            return _run_ssh_copy_id_with_subprocess(sshpass_cmd, temp_script_path)
        
        # Get SSH environment with askpass for passphrase handling
        from .askpass_utils import get_ssh_env_with_askpass
        env = get_ssh_env_with_askpass("force")
        
        # Convert env dict to list of strings for Vte.spawn_async
        env_list = []
        for key, value in env.items():
            env_list.append(f"{key}={value}")
        
        # Start the sshpass process in the terminal
        pid = terminal_widget.vte.spawn_async(
            Vte.PtyFlags.DEFAULT,
            os.path.expanduser('~') or '/',
            sshpass_cmd,
            env_list,
            GLib.SpawnFlags.DEFAULT,
            None,  # Child setup function
            None,  # Child setup data
            -1,    # Timeout (-1 = default)
            None,  # Cancellable
            None,  # Callback
            ()     # User data - empty tuple for Flatpak VTE compatibility
        )
        
        if pid == 0:
            # Clean up password file before returning
            try:
                os.unlink(pw_file_path)
            except Exception:
                pass
            return False, "Failed to start sshpass process in terminal"
        
        logger.info("sshpass process started in terminal for ssh-copy-id with saved password")
        
        # Note: We don't clean up the temp_script_path or pw_file_path here because the SSH process is still running
        # The files will be cleaned up by the system when the process exits
        
        return True, "sshpass process started in terminal - operation should complete automatically"
        
    except Exception as e:
        logger.error(f"Failed to run ssh-copy-id with saved password: {e}")
        return False, f"Saved password execution failed: {str(e)}"


def _run_ssh_copy_id_with_terminal(ssh_cmd, temp_script_path, terminal_widget):
    """Run ssh-copy-id using terminal widget for interactive password prompts"""
    try:
        if Vte is None or GLib is None:
            logger.error("Vte or GLib not available, falling back to subprocess")
            return _run_ssh_copy_id_with_subprocess(ssh_cmd, temp_script_path)
        
        # Get SSH environment with askpass for passphrase handling
        from .askpass_utils import get_ssh_env_with_askpass
        env = get_ssh_env_with_askpass("force")
        
        # Convert env dict to list of strings for Vte.spawn_async
        env_list = []
        for key, value in env.items():
            env_list.append(f"{key}={value}")
        
        # Start the SSH process in the terminal
        pid = terminal_widget.vte.spawn_async(
            Vte.PtyFlags.DEFAULT,
            os.path.expanduser('~') or '/',
            ssh_cmd,
            env_list,
            GLib.SpawnFlags.DEFAULT,
            None,  # Child setup function
            None,  # Child setup data
            -1,    # Timeout (-1 = default)
            None,  # Cancellable
            None,  # Callback
            ()     # User data - empty tuple for Flatpak VTE compatibility
        )
        
        if pid == 0:
            return False, "Failed to start SSH process in terminal"
        
        # The terminal widget will handle the interactive session
        # We can't easily wait for completion here since it's interactive
        # Instead, we'll let the user see the output and determine success
        logger.info("SSH process started in terminal for interactive ssh-copy-id")
        return True, "SSH process started in terminal - complete the operation in the terminal"
        
        # Note: We don't clean up the temp_script_path here because the SSH process is still running
        # The script will be cleaned up by the system when the process exits
        
    except Exception as e:
        logger.error(f"Failed to run ssh-copy-id with terminal: {e}")
        return False, f"Terminal execution failed: {str(e)}"


def _run_ssh_copy_id_with_subprocess(ssh_cmd, temp_script_path):
    """Run ssh-copy-id using subprocess (fallback method)"""
    try:
        # Execute the command
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0:
            logger.info("Custom ssh-copy-id: Successfully copied public key")
            return True, "Public key successfully installed on the server"
        else:
            error_msg = result.stderr.strip() if result.stderr else "Unknown error"
            logger.error(f"Custom ssh-copy-id: Failed to copy key: {error_msg}")
            return False, f"Failed to copy public key: {error_msg}"
            
    except subprocess.TimeoutExpired:
        logger.error("Custom ssh-copy-id: Timeout during key copy")
        return False, "Timeout during key copy operation"
    except Exception as e:
        logger.error(f"Custom ssh-copy-id: Subprocess execution failed: {e}")
        return False, f"Subprocess execution failed: {str(e)}"
    finally:
        # Clean up temporary script file after subprocess completes
        try:
            os.unlink(temp_script_path)
        except Exception:
            pass
