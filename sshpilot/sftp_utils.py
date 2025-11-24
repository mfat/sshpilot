"""
SFTP utilities and dialogs for mounting and opening remote directories
"""

import os
import logging
import shutil
import subprocess
import threading
from typing import Optional, Tuple, Callable, Any

from gi.repository import Gtk, Adw, Gio, GLib, Gdk

from .platform_utils import is_flatpak, is_macos

logger = logging.getLogger(__name__)


def _is_password_auth_enabled(connection: Any = None) -> bool:
    """Check if password authentication is enabled/required for this connection.
    
    Returns True only if password auth is explicitly required or preferred:
    - auth_method == 1 (password auth explicitly selected)
    - pubkey_auth_no == True (pubkey disabled, password required)
    - preferred_authentications contains 'password' as the primary/preferred method
    
    Returns False for:
    - auth_method == 0 (key-based auth) with pubkey enabled
    - Combined auth scenarios (key-based with password fallback)
    - All key_select_mode values (0=try all, 1=specific key with IdentitiesOnly, 2=specific key without IdentitiesOnly)
    """
    if connection is None:
        return False
    
    try:
        # Check auth_method (1 = password, 0 = key-based)
        # This is the PRIMARY indicator - if it's 0, it's key-based auth, period
        auth_method = int(getattr(connection, "auth_method", 0) or 0)
        
        # If auth_method is 0 (key-based), don't show password prompt
        # Even if password is in PreferredAuthentications, it's just a fallback
        if auth_method == 0:
            logger.debug("Password auth disabled: auth_method == 0 (key-based auth)")
            return False
        
        # If auth_method is 1, password auth is explicitly selected
        if auth_method == 1:
            logger.debug("Password auth enabled: auth_method == 1")
            return True
        
        # Check if pubkey auth is disabled (forces password auth)
        if getattr(connection, "pubkey_auth_no", False):
            logger.debug("Password auth enabled: pubkey_auth_no == True")
            return True
        
        # Check preferred_authentications - only if password is the primary/preferred method
        # If publickey comes before password, it's key-based with password fallback - don't show prompt
        preferred_auth = getattr(connection, "preferred_authentications", None)
        if preferred_auth:
            auth_list = []
            if isinstance(preferred_auth, (list, tuple)):
                auth_list = [str(a).lower() for a in preferred_auth]
            elif isinstance(preferred_auth, str):
                auth_list = [a.strip().lower() for a in preferred_auth.split(',')]
            
            if auth_list:
                # Only return True if password is the first/preferred method
                if auth_list[0] == "password":
                    return True
                
                # If publickey comes before password, it's key-based auth (password is just fallback)
                password_idx = auth_list.index("password") if "password" in auth_list else -1
                publickey_idx = auth_list.index("publickey") if "publickey" in auth_list else -1
                
                # If publickey is not in the list at all and password is, password might be required
                if publickey_idx == -1 and password_idx >= 0:
                    return True
                
                # If publickey comes before password, it's key-based auth - don't show prompt
                if publickey_idx >= 0 and password_idx >= 0 and publickey_idx < password_idx:
                    return False
    except Exception as exc:
        logger.debug(f"Error checking password auth status: {exc}")
    
    # Default: key-based auth (auth_method == 0) - don't show password prompt
    return False


def _show_password_dialog_for_mount(
    user: str,
    host: str,
    connection: Any = None,
    parent_window=None,
) -> Optional[str]:
    """Show password dialog for external file manager mount.
    
    Returns the password if user provided it, None if cancelled.
    Uses GLib main loop to handle dialog interaction properly.
    """
    password_result = [None]  # Use list to allow modification in nested function
    main_loop = GLib.MainLoop()
    
    # Get display name
    nickname = getattr(connection, 'nickname', None) if connection else None
    display_name = nickname or f"{user}@{host}"
    
    # Create password dialog
    dialog = Adw.MessageDialog(
        transient_for=parent_window,
        modal=True,
        heading="Password Required",
        body=f"Please enter your password for {display_name}:",
    )
    
    # Add password entry
    password_entry = Gtk.PasswordEntry()
    password_entry.set_property("placeholder-text", "Password")
    password_entry.set_margin_top(12)
    password_entry.set_margin_bottom(12)
    password_entry.set_margin_start(12)
    password_entry.set_margin_end(12)
    
    # Handle Enter key to activate default response
    key_controller = Gtk.EventControllerKey()
    def on_key_pressed(_controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            dialog.respond("connect")
            return True
        return False
    key_controller.connect("key-pressed", on_key_pressed)
    password_entry.add_controller(key_controller)
    
    # Add entry to dialog's extra child area
    dialog.set_extra_child(password_entry)
    
    # Add responses
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("connect", "Connect")
    dialog.set_default_response("connect")
    dialog.set_close_response("cancel")
    
    # Focus password entry when dialog is shown
    def on_dialog_shown(_dialog):
        password_entry.grab_focus()
    dialog.connect("notify::visible", lambda d, _: on_dialog_shown(d) if d.get_visible() else None)
    
    def on_response(_dialog, response: str) -> None:
        if response == "connect":
            entered_password = password_entry.get_text()
            if entered_password:
                password_result[0] = entered_password
            else:
                password_result[0] = None  # Empty password treated as cancel
        else:
            password_result[0] = None  # User cancelled
        dialog.destroy()
        main_loop.quit()
    
    dialog.connect("response", on_response)
    dialog.present()
    
    # Run main loop to wait for dialog response
    # This blocks until the dialog is closed
    main_loop.run()
    
    return password_result[0]


class PasswordMountOperation(Gio.MountOperation):
    """Custom MountOperation that automatically provides passwords from keyring"""
    
    def __init__(self, password: Optional[str] = None, username: Optional[str] = None):
        super().__init__()
        self._password = password
        self._username = username
        self._password_provided = False
        
        # Always connect to ask-password signal to handle password requests
        self.connect("ask-password", self._on_ask_password)
    
    def _on_ask_password(self, op, message: str, default_user: str, default_domain: str, flags: Gio.AskPasswordFlags):
        """Handle password requests from GVFS via signal"""
        if self._password and not self._password_provided:
            logger.debug("PasswordMountOperation: Providing saved password for user %s", default_user)
            self.set_password(self._password)
            # Use provided username if available, otherwise use default_user from mount operation
            username_to_use = self._username if self._username else default_user
            self.set_username(username_to_use)
            self.reply(Gio.MountOperationResult.HANDLED)
            self._password_provided = True
        else:
            # No password available - this shouldn't happen if we showed dialog before mounting
            # But if it does, reply UNHANDLED so gvfs can show its own prompt
            logger.warning("PasswordMountOperation: No password available when gvfs requested it")
            self.reply(Gio.MountOperationResult.UNHANDLED)


def open_remote_in_file_manager(
    user: str,
    host: str,
    port: Optional[int] = None,
    path: Optional[str] = None,
    error_callback: Optional[Callable] = None,
    parent_window=None,
    connection: Any = None,
    connection_manager: Any = None,
    ssh_config: Optional[dict] = None,
) -> Tuple[bool, Optional[str]]:
    """Open remote server in file manager using SFTP URI with asynchronous verification"""

    # Build sftp URI
    port_part = f":{port}" if port else ""
    
    if should_use_in_app_file_manager():
        # For in-app file manager, use the specified path
        p = path or "~"
        uri = f"sftp://{user}@{host}{port_part}{p}"
    else:
        # For external file managers, default to root but honor explicit paths
        requested_path = path or "/"
        if requested_path not in ("/", "") and not requested_path.startswith(("/", "~")):
            requested_path = f"/{requested_path.lstrip('/')}"
        uri = f"sftp://{user}@{host}{port_part}{requested_path}"

    logger.info(f"Opening SFTP URI: {uri}")

    if should_use_in_app_file_manager():
        logger.info("Using in-app file manager window for remote browsing")

        try:
            from .file_manager_window import launch_file_manager_window

            launch_file_manager_window(
                host=host,
                username=user,
                port=port or 22,
                path=path or "~",
                parent=parent_window,
                transient_for_parent=False,
                connection=connection,
                connection_manager=connection_manager,
                ssh_config=ssh_config,
            )
        except Exception as exc:
            logger.exception("Failed to launch in-app file manager: %s", exc)
            if error_callback:
                error_callback(str(exc))
            return False, str(exc)
        return True, None

    # Create progress dialog and start verification asynchronously
    progress_dialog = MountProgressDialog(user, host, parent_window)
    progress_dialog.present()
    progress_dialog.start_progress_updates()

    # Check if we have connection info and can get password - if so, skip verification
    has_password = False
    dialog_password = None  # Store password from dialog if needed
    
    if connection_manager is not None:
        # Try to get password using the same logic as mount
        lookup_hosts = []
        if connection is not None:
            hostname = getattr(connection, "hostname", None)
            host_attr = getattr(connection, "host", None)
            nickname = getattr(connection, "nickname", None)
            
            if hostname:
                lookup_hosts.append(hostname)
            if host_attr and host_attr not in lookup_hosts:
                lookup_hosts.append(host_attr)
            if nickname and nickname not in lookup_hosts:
                lookup_hosts.append(nickname)
        
        if not lookup_hosts:
            lookup_hosts = [host]
        
        lookup_user = user
        if connection is not None:
            lookup_user = getattr(connection, "username", None) or user
        
        for lookup_host in lookup_hosts:
            try:
                retrieved = connection_manager.get_password(lookup_host, lookup_user)
                if retrieved:
                    logger.debug("External file manager: Found password, skipping SSH verification")
                    has_password = True
                    break
            except Exception:
                pass

    # If no password found and password auth is enabled, show password dialog before verification
    if not has_password and connection_manager is not None:
        if _is_password_auth_enabled(connection):
            logger.debug("External file manager: No password found, password auth enabled, showing password dialog before verification")
            dialog_password = _show_password_dialog_for_mount(user, host, connection, progress_dialog)
            if dialog_password:
                logger.debug("External file manager: Password provided via dialog")
                has_password = True
            else:
                # User cancelled password dialog
                logger.info("User cancelled password entry for external file manager")
                progress_dialog.update_progress(0.0, "Password entry cancelled")
                progress_dialog.show_error("Password entry cancelled")
                GLib.timeout_add(1500, lambda: GLib.idle_add(progress_dialog.close))
                if error_callback:
                    error_callback("Password entry cancelled")
                return False, "Password entry cancelled"
        else:
            logger.debug("External file manager: No password found, but password auth not enabled, proceeding with key-based auth")
            # No password needed, proceed with key-based authentication

    # Skip verification for localhost or if we have password
    if host in ("localhost", "127.0.0.1") or has_password:
        if has_password:
            logger.info("Password available, skipping SSH verification")
        else:
            logger.info("Localhost detected, skipping SSH verification")
        progress_dialog.update_progress(0.3, "Mounting...")
        if is_flatpak():
            _open_sftp_flatpak_compatible(
                uri, user, host, port, error_callback, progress_dialog
            )
        else:
            _mount_and_open_sftp(uri, user, host, error_callback, progress_dialog, connection, connection_manager, provided_password=dialog_password)
        return True, None

    progress_dialog.update_progress(0.05, "Verifying SSH connection...")

    def _on_verify_complete(success: bool):
        if progress_dialog.is_cancelled:
            return
        if not success:
            error_msg = "SSH connection failed - check credentials and network connectivity"
            logger.error(f"SSH verification failed for {user}@{host}")
            progress_dialog.update_progress(0.0, "SSH connection failed")
            progress_dialog.show_error(error_msg)
            GLib.timeout_add(1500, lambda: GLib.idle_add(progress_dialog.close))
            if error_callback:
                error_callback(error_msg)
            return

        logger.info(f"SSH connection verified for {user}@{host}")
        progress_dialog.update_progress(0.3, "SSH verified, mounting...")
        if is_flatpak():
            _open_sftp_flatpak_compatible(
                uri, user, host, port, error_callback, progress_dialog
            )
        else:
            _mount_and_open_sftp(uri, user, host, error_callback, progress_dialog, connection, connection_manager, provided_password=dialog_password)

    _verify_ssh_connection_async(user, host, port, _on_verify_complete)

    return True, None


def should_use_in_app_file_manager() -> bool:
    """Return ``True`` when the libadwaita based file manager should be used."""

    return _should_use_in_app_file_manager()


def _should_use_in_app_file_manager() -> bool:
    """Return ``True`` when the libadwaita based file manager should be used."""

    if os.environ.get("SSHPILOT_FORCE_IN_APP_FILE_MANAGER") == "1":
        return True
    try:
        app = Adw.Application.get_default()
        config = getattr(app, 'config', None) if app else None
        if config and bool(config.get_setting('file_manager.force_internal', False)):
            return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to read file manager preference: %s", exc)
    if is_flatpak():
        return True
    if is_macos():
        return True
    if os.environ.get("SSHPILOT_DISABLE_GVFS") == "1":
        return True
    return not _gvfs_supports_sftp()


def _gvfs_supports_sftp() -> bool:
    """Heuristic detection of whether GVFS/GIO can handle SFTP mounts."""

    # ``gio`` is required for the ``gio mount`` helpers used by the rest of the
    # module.  If it is missing we assume GVFS support is not present.
    if shutil.which("gio") is None:
        logger.debug("gio binary missing – assuming GVFS unavailable")
        return False

    try:
        monitor = Gio.VolumeMonitor.get()
        if monitor is None:
            return False
        # Attempt to enumerate mounts which requires GVFS support.  We do not
        # care about the result, only that no exception is raised.
        monitor.get_mounts()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("GVFS detection failed: %s", exc)
        return False


def _mount_and_open_sftp(
    uri: str,
    user: str,
    host: str,
    error_callback=None,
    progress_dialog=None,
    connection: Any = None,
    connection_manager: Any = None,
    provided_password: Optional[str] = None,
):
    """Mount SFTP location and open in file manager"""
    try:
        logger.info(f"Mounting SFTP location: {uri}")

        # Create progress dialog if not provided
        if progress_dialog is None:
            progress_dialog = _create_mount_progress_dialog(user, host)
            progress_dialog.present()

        # Try to retrieve password using the same logic as built-in file manager
        password = provided_password  # Use provided password if available
        logger.debug("External file manager: _mount_and_open_sftp called with connection_manager=%s, connection=%s, provided_password=%s", 
                    "present" if connection_manager is not None else "None",
                    "present" if connection is not None else "None",
                    "present" if provided_password else "None")
        if not password and connection_manager is not None:
            # Try multiple host identifiers to match storage logic
            lookup_hosts = []
            if connection is not None:
                hostname = getattr(connection, "hostname", None)
                host_attr = getattr(connection, "host", None)
                nickname = getattr(connection, "nickname", None)
                
                # Add in storage priority order: hostname -> host -> nickname
                if hostname:
                    lookup_hosts.append(hostname)
                if host_attr and host_attr not in lookup_hosts:
                    lookup_hosts.append(host_attr)
                if nickname and nickname not in lookup_hosts:
                    lookup_hosts.append(nickname)
            
            # Fallback to provided host if no connection or no identifiers found
            if not lookup_hosts:
                lookup_hosts = [host]
            
            lookup_user = user
            if connection is not None:
                lookup_user = getattr(connection, "username", None) or user
            
            logger.debug(
                "External file manager: Attempting password lookup for %s@%s (trying identifiers: %s)",
                lookup_user,
                host,
                lookup_hosts
            )
            
            # Try each identifier until we find a password
            for lookup_host in lookup_hosts:
                try:
                    retrieved = connection_manager.get_password(lookup_host, lookup_user)
                    if retrieved:
                        logger.debug(
                            "External file manager: Password found for %s@%s using identifier '%s'",
                            lookup_user,
                            lookup_host,
                            lookup_host
                        )
                        password = retrieved
                        break
                    else:
                        logger.debug(
                            "External file manager: No password found for %s@%s using identifier '%s'",
                            lookup_user,
                            lookup_host,
                            lookup_host
                        )
                except Exception as exc:
                    logger.debug(
                        "Password lookup failed for %s@%s (identifier '%s'): %s",
                        lookup_user,
                        lookup_host,
                        lookup_host,
                        exc
                    )

        # If no password found and password auth is enabled, show password dialog before mounting
        if not password and connection_manager is not None:
            if _is_password_auth_enabled(connection):
                logger.debug("External file manager: No password found, password auth enabled, showing password dialog")
                password = _show_password_dialog_for_mount(user, host, connection, progress_dialog)
                if not password:
                    # User cancelled password dialog
                    logger.info("User cancelled password entry for external file manager")
                    progress_dialog.update_progress(0.0, "Password entry cancelled")
                    progress_dialog.show_error("Password entry cancelled")
            else:
                logger.debug("External file manager: No password found, but password auth not enabled, proceeding with key-based auth")
                # No password needed - proceed with mount using key-based authentication
                password = None

        gfile = Gio.File.new_for_uri(uri)
        # Use custom MountOperation with password
        lookup_user = user
        if connection is not None:
            lookup_user = getattr(connection, "username", None) or user
        
        if password:
            logger.debug("External file manager: Creating PasswordMountOperation with password for %s@%s", lookup_user, host)
            op = PasswordMountOperation(password, lookup_user)
        else:
            logger.warning("External file manager: No password available for mount operation - this may cause terminal prompts")
            op = Gio.MountOperation()

        def on_mounted(source, res, data=None):
            try:
                source.mount_enclosing_volume_finish(res)
                logger.info(
                    f"SFTP mount successful for {user}@{host}, opening file manager..."
                )

                # Update progress dialog
                progress_dialog.update_progress(
                    1.0, "Mount successful! Opening file manager..."
                )

                # Now it's mounted → open in the default file manager
                Gio.AppInfo.launch_default_for_uri(uri, None)
                logger.info(
                    f"File manager launched successfully for {user}@{host}"
                )

                # Close progress dialog after a short delay
                GLib.timeout_add(1000, lambda: GLib.idle_add(progress_dialog.close))

            except GLib.Error as e:
                # Check if the error is "already mounted" - this is actually a success case
                if "already mounted" in e.message.lower():
                    logger.info(
                        f"SFTP location already mounted for {user}@{host}, opening file manager..."
                    )

                    # Update progress dialog
                    progress_dialog.update_progress(
                        1.0, "Location already mounted! Opening file manager..."
                    )

                    # Open in the default file manager
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                    logger.info(
                        f"File manager launched successfully for {user}@{host}"
                    )

                    # Close progress dialog after a short delay
                    GLib.timeout_add(
                        1000, lambda: GLib.idle_add(progress_dialog.close)
                    )
                else:
                    error_msg = f"Could not mount {uri}: {e.message}"
                    logger.error(
                        f"Mount failed for {user}@{host}: {error_msg}"
                    )
                    progress_dialog.update_progress(
                        0.0, f"Mount failed: {e.message}"
                    )
                    progress_dialog.show_error(error_msg)

                    # Try Flatpak-compatible methods as fallback
                    if is_flatpak():
                        logger.info("Falling back to Flatpak-compatible methods")
                        GLib.idle_add(progress_dialog.close)
                        success, msg = _try_flatpak_compatible_mount(
                            uri, user, host, None, error_callback
                        )
                        if not success and error_callback:
                            error_callback(msg)
                    else:
                        GLib.timeout_add(
                            1500, lambda: GLib.idle_add(progress_dialog.close)
                        )
                        if error_callback:
                            error_callback(error_msg)
            except Exception as e:
                error_msg = f"Unexpected error during mount: {str(e)}"
                logger.error(f"Mount error for {user}@{host}: {e}")
                progress_dialog.update_progress(0.0, f"Error: {str(e)}")
                progress_dialog.show_error(error_msg)
                GLib.timeout_add(1500, lambda: GLib.idle_add(progress_dialog.close))
                if error_callback:
                    error_callback(error_msg)

        # Start progress updates if not already running
        if not getattr(progress_dialog, "progress_timer", None):
            progress_dialog.start_progress_updates()

        gfile.mount_enclosing_volume(
            Gio.MountMountFlags.NONE,
            op,
            None,  # cancellable
            on_mounted,
            None,
        )

        logger.info(f"Mount operation started for {user}@{host}")
        return True, None

    except Exception as e:
        error_msg = f"Failed to start mount operation: {str(e)}"
        logger.error(f"Mount operation failed for {user}@{host}: {e}")
        GLib.timeout_add(1500, lambda: GLib.idle_add(progress_dialog.close))

        # Try Flatpak-compatible methods as fallback
        if is_flatpak():
            logger.info(
                "Primary mount failed, trying Flatpak-compatible methods"
            )
            return _try_flatpak_compatible_mount(
                uri, user, host, None, error_callback
            )

        if error_callback:
            error_callback(error_msg)
        return False, error_msg


def _verify_ssh_connection(user: str, host: str, port: Optional[int]) -> bool:
    """Verify SSH connection without full mount"""
    # Local connections are considered valid without verification
    if host in ("localhost", "127.0.0.1"):
        return True

    ssh_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]

    # Only disable interactive prompts if no askpass is available
    if not os.environ.get("SSH_ASKPASS"):
        ssh_cmd.extend(["-o", "BatchMode=yes"])

    if port:
        ssh_cmd.extend(["-p", str(port)])
    ssh_cmd.extend([f"{user}@{host}", "echo", "READY"])

    try:
        result = subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def _verify_ssh_connection_async(
    user: str, host: str, port: Optional[int], callback: Callable[[bool], None]
) -> None:
    """Verify SSH connection on a background thread and invoke callback with the result"""

    def worker():
        success = _verify_ssh_connection(user, host, port)
        GLib.idle_add(callback, success)

    threading.Thread(target=worker, daemon=True).start()


def _open_sftp_flatpak_compatible(
    uri: str,
    user: str,
    host: str,
    port: Optional[int],
    error_callback: Optional[Callable],
    progress_dialog=None,
    parent_window=None,
) -> Tuple[bool, Optional[str]]:
    """Open SFTP using Flatpak-compatible methods with proper portal usage"""

    # Reuse existing progress dialog if provided
    if progress_dialog is None:
        progress_dialog = MountProgressDialog(user, host, parent_window)
        progress_dialog.present()
    if not getattr(progress_dialog, "progress_timer", None):
        progress_dialog.start_progress_updates()

    # Method 1: Use XDG Desktop Portal File Chooser to access GVFS mounts
    try:
        progress_dialog.update_progress(0.2, "Trying portal file access...")
        success = _try_portal_file_access(uri, user, host)
        if success:
            progress_dialog.update_progress(
                1.0, "Success! Opening file manager..."
            )
            GLib.timeout_add(1000, lambda: GLib.idle_add(progress_dialog.close))
            return True, None
    except Exception as e:
        logger.warning(f"Portal file access failed: {e}")

    # Method 2: Try to launch external file manager that can handle SFTP
    try:
        progress_dialog.update_progress(0.4, "Trying external file managers...")
        success = _try_external_file_managers(uri, user, host)
        if success:
            progress_dialog.update_progress(
                1.0, "Success! File manager opened..."
            )
            GLib.timeout_add(1000, lambda: GLib.idle_add(progress_dialog.close))
            return True, None
    except Exception as e:
        logger.warning(f"External file managers failed: {e}")

    # Method 3: Use host's GVFS if accessible
    try:
        progress_dialog.update_progress(0.6, "Checking host GVFS mounts...")
        success = _try_host_gvfs_access(uri, user, host, port)
        if success:
            progress_dialog.update_progress(
                1.0, "Success! Found existing mount..."
            )
            GLib.timeout_add(1000, lambda: GLib.idle_add(progress_dialog.close))
            return True, None
    except Exception as e:
        logger.warning(f"Host GVFS access failed: {e}")

    # Method 4: Show connection dialog for manual setup
    progress_dialog.update_progress(0.8, "Preparing manual connection options...")
    success = _show_manual_connection_dialog(user, host, port, uri)
    if success:
        GLib.idle_add(progress_dialog.close)
        return True, "Manual connection dialog opened"

    # All methods failed
    error_msg = (
        "Could not open SFTP connection - try mounting the location manually first"
    )
    progress_dialog.show_error(error_msg)
    GLib.timeout_add(1500, lambda: GLib.idle_add(progress_dialog.close))
    if error_callback:
        GLib.idle_add(error_callback, error_msg)
    return False, error_msg


def _try_portal_file_access(uri: str, user: str, host: str) -> bool:
    """Try to access SFTP location via XDG Desktop Portal - skip file chooser"""

    # Skip the file chooser dialog approach - it's not what we want
    # Instead, try to use the portal to trigger a mount and then open directly
    try:
        # Try to mount via D-Bus portal interface directly
        return _try_dbus_gvfs_mount(uri, user, host)
    except Exception as e:
        logger.warning(f"Portal D-Bus mount failed: {e}")
        return False


def _try_dbus_gvfs_mount(uri: str, user: str, host: str) -> bool:
    """Try to mount GVFS via gio command and open directly"""

    try:
        if (
            subprocess.run(
                ["which", "flatpak-spawn"], capture_output=True
            ).returncode
            == 0
        ):
            # Check if gio is available on host (it should be)
            check_cmd = ["flatpak-spawn", "--host", "which", "gio"]
            if subprocess.run(check_cmd, capture_output=True).returncode == 0:
                logger.info(f"Using gio mount for {uri}")

                # Mount using host's gio mount
                mount_cmd = ["flatpak-spawn", "--host", "gio", "mount", uri]
                result = subprocess.run(
                    mount_cmd, capture_output=True, text=True, timeout=30
                )

                logger.info(
                    "gio mount result: returncode=%s, stdout=%s, stderr=%s",
                    result.returncode,
                    result.stdout,
                    result.stderr,
                )

                if (
                    result.returncode == 0
                    or "already mounted" in result.stderr.lower()
                    or "Operation not supported" not in result.stderr
                ):
                    logger.info(
                        "gio mount successful or location already accessible"
                    )

                    # Give it a moment for the mount to be ready
                    import time

                    time.sleep(1)

                    # Find the actual mount point and open that instead of the URI
                    mount_point = _find_gvfs_mount_point(user, host)
                    if mount_point:
                        open_cmd = [
                            "flatpak-spawn",
                            "--host",
                            "xdg-open",
                            mount_point,
                        ]
                        subprocess.Popen(
                            open_cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        logger.info(
                            f"File manager opened at mount point: {mount_point}"
                        )
                        return True
                    else:
                        # Try opening the URI directly with specific file managers
                        logger.info(
                            "Mount point not found, trying file managers with URI"
                        )
                        return _try_specific_file_managers_with_uri(uri)
                else:
                    logger.warning(f"gio mount failed: {result.stderr}")
                    # Still try file managers in case they can handle it
                    return _try_specific_file_managers_with_uri(uri)
            else:
                logger.warning("gio not available on host system")
                return _try_specific_file_managers_with_uri(uri)

        return False

    except Exception as e:
        logger.warning(f"gio mount failed: {e}")
        return False


def _find_gvfs_mount_point(user: str, host: str) -> Optional[str]:
    """Find the actual GVFS mount point for the SFTP connection"""

    gvfs_paths = [
        f"/run/user/{os.getuid()}/gvfs",
        f"/var/run/user/{os.getuid()}/gvfs",
    ]

    for gvfs_path in gvfs_paths:
        try:
            if os.path.exists(gvfs_path):
                for mount_dir in os.listdir(gvfs_path):
                    # Look for SFTP mount matching our host
                    if f"sftp:host={host}" in mount_dir and f"user={user}" in mount_dir:
                        mount_point = os.path.join(gvfs_path, mount_dir)
                        logger.info(f"Found GVFS mount point: {mount_point}")
                        return mount_point
        except Exception as e:
            logger.debug(f"Could not check GVFS path {gvfs_path}: {e}")

    return None


def _try_specific_file_managers_with_uri(uri: str) -> bool:
    """Try specific file managers that handle SFTP URIs properly"""

    # File managers that are known to handle SFTP URIs well
    managers = [
        ["flatpak-spawn", "--host", "nautilus", uri],
        ["flatpak-spawn", "--host", "thunar", uri],
        ["flatpak-spawn", "--host", "dolphin", uri],
        ["flatpak-spawn", "--host", "nemo", uri],
    ]

    for cmd in managers:
        try:
            # Check if the file manager exists on host
            check_cmd = ["flatpak-spawn", "--host", "which", cmd[2]]
            if subprocess.run(check_cmd, capture_output=True).returncode == 0:
                logger.info(f"Trying {cmd[2]} with SFTP URI")
                subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                logger.info(f"Launched {cmd[2]} via flatpak-spawn")
                return True
        except Exception as e:
            logger.debug(f"Failed to launch {cmd[2]}: {e}")

    return False


def _try_external_file_managers(uri: str, user: str, host: str) -> bool:
    """Try launching external file managers that handle SFTP"""

    # File managers with their specific SFTP handling
    managers = [
        ("nautilus", [uri]),  # Usually handles SFTP URIs well
        ("thunar", [uri]),  # Good SFTP support
        ("dolphin", [uri]),  # KDE file manager
        ("nemo", [uri]),  # Cinnamon file manager
        ("pcmanfm", [uri]),  # Lightweight option
    ]

    for manager, cmd in managers:
        try:
            # Use flatpak-spawn to run on the host if available
            if (
                subprocess.run(
                    ["which", "flatpak-spawn"], capture_output=True
                ).returncode
                == 0
            ):
                # Check if manager exists on host
                check_cmd = ["flatpak-spawn", "--host", "which", manager]
                if subprocess.run(check_cmd, capture_output=True).returncode == 0:
                    spawn_cmd = ["flatpak-spawn", "--host", manager, uri]
                    subprocess.Popen(
                        spawn_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    logger.info(
                        f"Launched {manager} via flatpak-spawn with URI"
                    )
                    return True

            # Try direct launch as fallback
            elif subprocess.run(["which", manager], capture_output=True).returncode == 0:
                subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                logger.info(f"Launched {manager} directly with URI")
                return True

        except Exception as e:
            logger.debug(f"Failed to launch {manager}: {e}")
            continue

    # If no file manager worked with URI, try a different approach
    logger.warning("No file manager could handle SFTP URI directly")
    return _try_alternative_approaches(uri, user, host)


def _try_alternative_approaches(uri: str, user: str, host: str) -> bool:
    """Try alternative approaches when direct URI opening fails"""

    # Method 1: Try opening network locations in file managers
    network_locations = [
        "network:///",
        "sftp://",
        f"sftp://{host}/",
    ]

    for location in network_locations:
        try:
            if (
                subprocess.run(
                    ["which", "flatpak-spawn"], capture_output=True
                ).returncode
                == 0
            ):
                cmd = ["flatpak-spawn", "--host", "nautilus", location]
                subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                logger.info(f"Opened network location: {location}")
                return True
        except Exception as e:
            logger.debug(f"Failed to open {location}: {e}")

    # Method 2: Try using gio mount command
    try:
        if (
            subprocess.run(
                ["which", "flatpak-spawn"], capture_output=True
            ).returncode
            == 0
        ):
            # Check if gio is available
            check_cmd = ["flatpak-spawn", "--host", "which", "gio"]
            if subprocess.run(check_cmd, capture_output=True).returncode == 0:
                # Use gio to mount
                mount_cmd = ["flatpak-spawn", "--host", "gio", "mount", uri]
                result = subprocess.run(
                    mount_cmd, capture_output=True, text=True, timeout=30
                )

                if result.returncode == 0:
                    # Try to open with nautilus after mounting
                    open_cmd = [
                        "flatpak-spawn",
                        "--host",
                        "nautilus",
                        uri,
                    ]
                    subprocess.Popen(
                        open_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    logger.info(
                        "Mounted with gio and opened with nautilus"
                    )
                    return True
    except Exception as e:
        logger.debug(f"gio mount failed: {e}")

    return False


def _try_host_gvfs_access(
    uri: str, user: str, host: str, port: Optional[int]
) -> bool:
    """Try accessing GVFS mounts on the host system"""

    # Check if we can access the host's GVFS mounts
    gvfs_paths = [
        f"/run/user/{os.getuid()}/gvfs",
        f"/var/run/user/{os.getuid()}/gvfs",
        f"{os.path.expanduser('~')}/.gvfs",
    ]

    for gvfs_path in gvfs_paths:
        if os.path.exists(gvfs_path):
            # Look for existing SFTP mount
            sftp_pattern = f"sftp:host={host}"
            try:
                for mount_dir in os.listdir(gvfs_path):
                    if sftp_pattern in mount_dir:
                        mount_path = os.path.join(gvfs_path, mount_dir)
                        # Open in file manager
                        subprocess.Popen(
                            ["xdg-open", mount_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        logger.info(
                            f"Opened existing GVFS mount: {mount_path}"
                        )
                        return True
            except Exception as e:
                logger.debug(
                    f"Could not access GVFS path {gvfs_path}: {e}"
                )
                continue

    return False


def _show_manual_connection_dialog(
    user: str, host: str, port: Optional[int], uri: str
) -> bool:
    """Show dialog with manual connection instructions"""

    dialog = SftpConnectionDialog(user, host, port, uri)
    dialog.present()
    return True


def _open_sftp_native(
    uri: str,
    user: str,
    host: str,
    error_callback: Optional[Callable],
    parent_window=None,
) -> Tuple[bool, Optional[str]]:
    """Native installation SFTP opening with GVFS"""

    # Try direct GVFS mount first
    try:
        success = _mount_and_open_sftp_native(
            uri, user, host, error_callback, parent_window
        )
        if success:
            return True, None
    except Exception as e:
        logger.warning(f"Native GVFS mount failed: {e}")

    # Fall back to Flatpak-compatible methods
    return _open_sftp_flatpak_compatible(
        uri, user, host, None, error_callback, parent_window=parent_window
    )


def _mount_and_open_sftp_native(
    uri: str,
    user: str,
    host: str,
    error_callback: Optional[Callable],
    parent_window=None,
) -> bool:
    """Original native GVFS mounting method"""

    logger.info(f"Mounting SFTP location: {uri}")

    progress_dialog = MountProgressDialog(user, host, parent_window)
    progress_dialog.present()

    gfile = Gio.File.new_for_uri(uri)
    op = Gio.MountOperation()

    def on_mounted(source, res, data=None):
        try:
            source.mount_enclosing_volume_finish(res)
            logger.info(f"SFTP mount successful for {user}@{host}")
            progress_dialog.update_progress(
                1.0, "Mount successful! Opening file manager..."
            )
            Gio.AppInfo.launch_default_for_uri(uri, None)
            GLib.timeout_add(1000, progress_dialog.close)

        except GLib.Error as e:
            if "already mounted" in e.message.lower():
                logger.info(
                    f"SFTP location already mounted for {user}@{host}"
                )
                progress_dialog.update_progress(
                    1.0, "Location already mounted! Opening file manager..."
                )
                Gio.AppInfo.launch_default_for_uri(uri, None)
                GLib.timeout_add(1000, progress_dialog.close)
            else:
                error_msg = f"Could not mount {uri}: {e.message}"
                logger.error(
                    f"Mount failed for {user}@{host}: {error_msg}"
                )
                progress_dialog.show_error(error_msg)
                if error_callback:
                    error_callback(error_msg)

    progress_dialog.start_progress_updates()
    gfile.mount_enclosing_volume(
        Gio.MountMountFlags.NONE, op, None, on_mounted, None
    )
    logger.info(f"Mount operation started for {user}@{host}")
    return True


def _try_flatpak_compatible_mount(
    uri: str,
    user: str,
    host: str,
    port: int | None,
    error_callback=None,
):
    """Try various methods to open SFTP in Flatpak environment"""

    # Method 1: Try direct URI launch (sometimes works if gvfs is available)
    try:
        logger.info("Trying direct URI launch...")
        Gio.AppInfo.launch_default_for_uri(uri, None)
        logger.info(f"Direct URI launch successful for {user}@{host}")
        return True, None
    except Exception as e:
        logger.warning(f"Direct URI launch failed: {e}")

    # Method 2: Try external file managers that handle SFTP
    external_managers = [
        ("nautilus", [uri]),  # GNOME Files
        ("thunar", [uri]),  # XFCE Thunar
        ("dolphin", [uri]),  # KDE Dolphin
        ("pcmanfm", [uri]),  # PCManFM
        ("nemo", [uri]),  # Nemo
    ]

    for manager, cmd in external_managers:
        try:
            # Check if the file manager exists
            if (
                subprocess.run(["which", manager], capture_output=True).returncode
                == 0
            ):
                logger.info(f"Trying external file manager: {manager}")
                subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                logger.info(
                    f"External file manager {manager} launched for {user}@{host}"
                )
                return True, None
        except Exception as e:
            logger.warning(f"Failed to launch {manager}: {e}")

    # Method 3: Try mounting via command line tools
    return _try_command_line_mount(user, host, port, error_callback)


def _try_command_line_mount(
    user: str, host: str, port: int | None, error_callback=None
):
    """Try mounting via command line utilities"""

    # Create a mount point in user's home directory
    mount_point = os.path.expanduser(f"~/sftp-{host}")

    try:
        # Create mount point if it doesn't exist
        os.makedirs(mount_point, exist_ok=True)

        # Try sshfs if available
        sshfs_cmd = ["sshfs"]
        if port:
            sshfs_cmd.extend(["-p", str(port)])

        sshfs_cmd.extend(
            [
                "-o",
                "reconnect",
                f"{user}@{host}:/",
                mount_point,
            ]
        )

        logger.info(f"Trying sshfs mount: {' '.join(sshfs_cmd)}")
        result = subprocess.run(
            sshfs_cmd, capture_output=True, text=True, timeout=30
        )

        if result.returncode == 0:
            logger.info(f"sshfs mount successful at {mount_point}")
            # Open the mount point in file manager
            try:
                subprocess.Popen(["xdg-open", mount_point])
                return True, None
            except Exception as e:
                logger.warning(f"Failed to open mount point: {e}")
                return True, (
                    f"Mounted at {mount_point} but couldn't open file manager"
                )
        else:
            logger.warning(f"sshfs failed: {result.stderr}")

    except subprocess.TimeoutExpired:
        logger.error("sshfs mount timeout")
    except Exception as e:
        logger.error(f"sshfs mount error: {e}")

    # Method 4: Fall back to terminal-based SFTP client
    return _launch_terminal_sftp(user, host, port, error_callback)


def _launch_terminal_sftp(
    user: str, host: str, port: int | None, error_callback=None
):
    """Launch terminal-based SFTP client as last resort"""

    terminals = ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"]

    sftp_cmd = ["sftp"]
    if port:
        sftp_cmd.extend(["-P", str(port)])
    sftp_cmd.append(f"{user}@{host}")

    for terminal in terminals:
        try:
            if (
                subprocess.run(["which", terminal], capture_output=True).returncode
                == 0
            ):
                logger.info(f"Launching terminal SFTP with {terminal}")

                if terminal == "gnome-terminal":
                    cmd = [terminal, "--", *sftp_cmd]
                elif terminal == "konsole":
                    cmd = [terminal, "-e", *sftp_cmd]
                elif terminal == "xfce4-terminal":
                    cmd = [terminal, "-e", " ".join(sftp_cmd)]
                else:
                    cmd = [terminal, "-e", " ".join(sftp_cmd)]

                subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                return True, f"Opened SFTP connection in {terminal}"

        except Exception as e:
            logger.warning(f"Failed to launch {terminal}: {e}")

    error_msg = (
        "Could not open SFTP connection - no compatible file manager or terminal found"
    )
    logger.error(error_msg)
    if error_callback:
        error_callback(error_msg)
        return False, error_msg


def _create_mount_progress_dialog(user: str, host: str):
    """Create a progress dialog for SFTP mount operation"""
    return MountProgressDialog(user, host)


class MountProgressDialog(Adw.Window):
    """Progress dialog for SFTP mount operations"""

    def __init__(self, user: str, host: str, parent_window=None):
        super().__init__()
        self.user = user
        self.host = host
        self.progress_value = 0.0
        self.is_cancelled = False
        self.progress_timer = None

        self.set_title("Mounting SFTP Connection")
        self.set_default_size(500, 200)
        self.set_resizable(False)
        self.set_modal(True)

        # Set as transient for parent window if provided
        if parent_window:
            self.set_transient_for(parent_window)

        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_start(20)
        main_box.set_margin_end(20)
        main_box.set_margin_top(20)
        main_box.set_margin_bottom(20)

        # Header
        header_label = Gtk.Label()
        header_label.set_markup(f"<b>Connecting to {user}@{host}</b>")
        main_box.append(header_label)

        # Progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_text("Initializing connection...")
        main_box.append(self.progress_bar)

        # Status label
        self.status_label = Gtk.Label()
        self.status_label.set_text("Preparing SFTP mount...")
        main_box.append(self.status_label)

        # Cancel button
        self.cancel_button = Gtk.Button.new_with_label("Cancel")
        self.cancel_button.connect("clicked", self._on_cancel)
        self.cancel_button.set_halign(Gtk.Align.CENTER)
        main_box.append(self.cancel_button)

        self.set_content(main_box)

    def _on_cancel(self, button):
        """Cancel mount operation"""
        self.is_cancelled = True
        if self.progress_timer:
            GLib.source_remove(self.progress_timer)
            self.progress_timer = None
        self.close()

    def start_progress_updates(self):
        """Start simulated progress updates"""
        self.progress_value = 0.0
        self.progress_timer = GLib.timeout_add(100, self._update_progress_simulation)

    def _update_progress_simulation(self):
        """Simulate mounting progress"""
        if self.is_cancelled:
            return False

        self.progress_value += 0.02
        if self.progress_value >= 0.9:
            self.progress_value = 0.9

        self.update_progress(
            self.progress_value, "Establishing SFTP connection..."
        )
        return True

    def update_progress(self, fraction: float, text: str):
        """Update progress bar and status"""
        self.progress_bar.set_fraction(fraction)
        self.progress_bar.set_text(text)
        self.status_label.set_text(text)

    def show_error(self, error_text: str):
        """Show error state"""
        self.progress_bar.add_css_class("error")
        self.cancel_button.set_label("Close")

    def close(self, widget=None):
        """Close the dialog"""
        if self.progress_timer:
            GLib.source_remove(self.progress_timer)
            self.progress_timer = None
        self.destroy()


class SftpConnectionDialog(Adw.Window):
    """Dialog showing manual connection options for SFTP"""

    def __init__(self, user: str, host: str, port: Optional[int], uri: str):
        super().__init__()
        self.user = user
        self.host = host
        self.port = port
        self.uri = uri

        self.set_title("SFTP Connection")
        self.set_default_size(600, 400)
        self.set_modal(True)

        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        main_box.set_margin_start(24)
        main_box.set_margin_end(24)
        main_box.set_margin_top(24)
        main_box.set_margin_bottom(24)

        # Header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)

        icon = Gtk.Image.new_from_icon_name("folder-remote-symbolic")
        icon.set_pixel_size(48)
        header_box.append(icon)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_label = Gtk.Label()
        title_label.set_markup("<b>SFTP Connection Options</b>")
        title_label.set_halign(Gtk.Align.START)
        title_box.append(title_label)

        subtitle_label = Gtk.Label()
        subtitle_label.set_text(f"Connect to {user}@{host}")
        subtitle_label.add_css_class("dim-label")
        subtitle_label.set_halign(Gtk.Align.START)
        title_box.append(subtitle_label)

        header_box.append(title_box)
        main_box.append(header_box)

        # Instructions
        instructions = Gtk.Label()
        instructions.set_markup(
            "Due to Flatpak security restrictions, direct SFTP mounting is limited.\n"
            "Please choose one of the following options:"
        )
        instructions.set_wrap(True)
        instructions.set_halign(Gtk.Align.START)
        main_box.append(instructions)

        # Options list
        options_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # Option 1: File Manager
        option1_box = self._create_option_box(
            "Open in File Manager",
            "Try opening the SFTP location directly in your system's file manager",
            "folder-symbolic",
            lambda: self._try_file_manager(),
        )
        options_box.append(option1_box)

        # Option 2: Copy URI
        option2_box = self._create_option_box(
            "Copy SFTP URI",
            f"Copy the SFTP URI to clipboard: {uri}",
            "edit-copy-symbolic",
            lambda: self._copy_uri(),
        )
        options_box.append(option2_box)

        # Option 3: Terminal
        option3_box = self._create_option_box(
            "Open in Terminal",
            "Open an SFTP connection in terminal",
            "utilities-terminal-symbolic",
            lambda: self._open_terminal(),
        )
        options_box.append(option3_box)

        main_box.append(options_box)

        # Close button
        close_button = Gtk.Button.new_with_label("Close")
        close_button.connect("clicked", lambda b: self.close())
        close_button.set_halign(Gtk.Align.END)
        main_box.append(close_button)

        self.set_content(main_box)

    def _create_option_box(
        self, title: str, description: str, icon_name: str, callback: Callable
    ) -> Gtk.Box:
        """Create an option box with icon, text, and button"""

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class("card")
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(24)
        box.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        title_label = Gtk.Label()
        title_label.set_markup(f"<b>{title}</b>")
        title_label.set_halign(Gtk.Align.START)
        text_box.append(title_label)

        desc_label = Gtk.Label()
        desc_label.set_text(description)
        desc_label.set_halign(Gtk.Align.START)
        desc_label.add_css_class("dim-label")
        desc_label.set_wrap(True)
        text_box.append(desc_label)

        box.append(text_box)

        button = Gtk.Button.new_with_label("Try")
        button.connect("clicked", lambda b: callback())
        box.append(button)

        return box

    def _try_file_manager(self):
        """Try opening in file manager"""
        try:
            Gio.AppInfo.launch_default_for_uri(self.uri, None)
        except Exception:
            # Fall back to xdg-open
            subprocess.Popen(
                ["xdg-open", self.uri],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _copy_uri(self):
        """Copy URI to clipboard"""
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.uri, -1)

    def _open_terminal(self):
        """Open SFTP in terminal"""
        sftp_cmd = ["sftp"]
        if self.port:
            sftp_cmd.extend(["-P", str(self.port)])
        sftp_cmd.append(f"{self.user}@{self.host}")

        terminals = ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"]

        for terminal in terminals:
            try:
                if (
                    subprocess.run(["which", terminal], capture_output=True).returncode
                    == 0
                ):
                    if terminal == "gnome-terminal":
                        cmd = [terminal, "--", *sftp_cmd]
                    elif terminal == "konsole":
                        cmd = [terminal, "-e", *sftp_cmd]
                    else:
                        cmd = [terminal, "-e", " ".join(sftp_cmd)]

                    subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    break
            except Exception:
                continue

