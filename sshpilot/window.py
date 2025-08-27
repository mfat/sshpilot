"""
Main Window for sshPilot
Primary UI with connection list, tabs, and terminal management
"""

import os
import logging
import math
import time
from typing import Optional, Dict, Any, List, Tuple, Callable

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
try:
    gi.require_version('Vte', '3.91')
    from gi.repository import Vte
    _HAS_VTE = True
except Exception:
    _HAS_VTE = False

from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk, Pango
import subprocess
import threading

# Feature detection for libadwaita versions across distros
HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')
HAS_TIMED_ANIMATION = hasattr(Adw, 'TimedAnimation')

from gettext import gettext as _

from .connection_manager import ConnectionManager, Connection
from .terminal import TerminalWidget
from .config import Config
from .key_manager import KeyManager, SSHKey
from .port_forwarding_ui import PortForwardingRules
from .connection_dialog import ConnectionDialog
from .askpass_utils import ensure_askpass_script, get_ssh_env_with_askpass_for_password

logger = logging.getLogger(__name__)


def is_running_in_flatpak() -> bool:
    """Check if running inside Flatpak sandbox"""
    return os.path.exists("/.flatpak-info") or os.environ.get("FLATPAK_ID") is not None

def open_remote_in_file_manager(user: str, host: str, port: Optional[int] = None,
                               path: Optional[str] = None, error_callback: Optional[Callable] = None,
                               parent_window=None) -> Tuple[bool, Optional[str]]:
    """Open remote server in file manager using SFTP URI"""

    # Build sftp URI
    p = path or "/"
    port_part = f":{port}" if port else ""
    uri = f"sftp://{user}@{host}{port_part}{p}"

    logger.info(f"Opening SFTP URI: {uri}")

    # For Flatpak environments, use compatible methods immediately
    if is_running_in_flatpak():
        logger.info("Running in Flatpak, using compatible methods")
        return _open_sftp_flatpak_compatible(uri, user, host, port, error_callback, parent_window)

    # For non-Flatpak environments, perform synchronous verification and mounting
    logger.info("Running in native environment, performing synchronous operations")
    
    # Create progress dialog
    progress_dialog = MountProgressDialog(user, host, parent_window)
    progress_dialog.present()
    progress_dialog.start_progress_updates()
    progress_dialog.update_progress(0.05, "Verifying SSH connection...")

    try:
        # Verify SSH connection synchronously
        ssh_verified = _verify_ssh_connection(user, host, port)
        if not ssh_verified:
            error_msg = "SSH connection failed - check credentials and network connectivity"
            logger.error(f"SSH verification failed for {user}@{host}")
            progress_dialog.update_progress(0.0, "SSH connection failed")
            progress_dialog.show_error(error_msg)
            GLib.timeout_add(1500, progress_dialog.close)
            if error_callback:
                error_callback(error_msg)
            return False, error_msg

        logger.info(f"SSH connection verified for {user}@{host}")
        progress_dialog.update_progress(0.3, "SSH verified, mounting...")
        
        # Perform synchronous mount and open
        success, error_msg = _mount_and_open_sftp_sync(uri, user, host, progress_dialog)
        
        if success:
            progress_dialog.update_progress(1.0, "Success! File manager opened...")
            GLib.timeout_add(1000, progress_dialog.close)
            return True, None
        else:
            progress_dialog.show_error(error_msg or "Failed to mount SFTP location")
            GLib.timeout_add(1500, progress_dialog.close)
            if error_callback:
                error_callback(error_msg)
            return False, error_msg
            
    except Exception as e:
        error_msg = f"Failed to open file manager: {str(e)}"
        logger.error(f"Error opening file manager for {user}@{host}: {e}")
        progress_dialog.show_error(error_msg)
        GLib.timeout_add(1500, progress_dialog.close)
        if error_callback:
            error_callback(error_msg)
        return False, error_msg

def _mount_and_open_sftp(uri: str, user: str, host: str, error_callback=None, progress_dialog=None):
    """Mount SFTP location and open in file manager"""
    try:
        logger.info(f"Mounting SFTP location: {uri}")

        # Create progress dialog if not provided
        if progress_dialog is None:
            progress_dialog = _create_mount_progress_dialog(user, host)
            progress_dialog.present()

        gfile = Gio.File.new_for_uri(uri)
        op = Gio.MountOperation()

        def on_mounted(source, res, data=None):
            try:
                source.mount_enclosing_volume_finish(res)
                logger.info(f"SFTP mount successful for {user}@{host}, opening file manager...")

                # Update progress dialog
                progress_dialog.update_progress(1.0, "Mount successful! Opening file manager...")

                # Now it's mounted → open in the default file manager
                Gio.AppInfo.launch_default_for_uri(uri, None)
                logger.info(f"File manager launched successfully for {user}@{host}")

                # Close progress dialog after a short delay
                GLib.timeout_add(1000, progress_dialog.close)

            except GLib.Error as e:
                # Check if the error is "already mounted" - this is actually a success case
                if "already mounted" in e.message.lower():
                    logger.info(f"SFTP location already mounted for {user}@{host}, opening file manager...")

                    # Update progress dialog
                    progress_dialog.update_progress(1.0, "Location already mounted! Opening file manager...")

                    # Open in the default file manager
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                    logger.info(f"File manager launched successfully for {user}@{host}")

                    # Close progress dialog after a short delay
                    GLib.timeout_add(1000, progress_dialog.close)
                else:
                    error_msg = f"Could not mount {uri}: {e.message}"
                    logger.error(f"Mount failed for {user}@{host}: {error_msg}")
                    progress_dialog.update_progress(0.0, f"Mount failed: {e.message}")
                    progress_dialog.show_error(error_msg)

                    # Try Flatpak-compatible methods as fallback
                    if is_running_in_flatpak():
                        logger.info("Falling back to Flatpak-compatible methods")
                        progress_dialog.close()
                        success, msg = _try_flatpak_compatible_mount(uri, user, host, None, error_callback)
                        if not success and error_callback:
                            error_callback(msg)
                    elif error_callback:
                        error_callback(error_msg)
            except Exception as e:
                error_msg = f"Unexpected error during mount: {str(e)}"
                logger.error(f"Mount error for {user}@{host}: {e}")
                progress_dialog.update_progress(0.0, f"Error: {str(e)}")
                progress_dialog.show_error(error_msg)
                if error_callback:
                    error_callback(error_msg)

        # Start progress updates if not already running
        if not getattr(progress_dialog, 'progress_timer', None):
            progress_dialog.start_progress_updates()

        gfile.mount_enclosing_volume(
            Gio.MountMountFlags.NONE,
            op,
            None,  # cancellable
            on_mounted,
            None
        )

        logger.info(f"Mount operation started for {user}@{host}")
        return True, None
        
    except Exception as e:
        error_msg = f"Failed to start mount operation: {str(e)}"
        logger.error(f"Mount operation failed for {user}@{host}: {e}")
        
        # Try Flatpak-compatible methods as fallback
        if is_running_in_flatpak():
            logger.info("Primary mount failed, trying Flatpak-compatible methods")
            return _try_flatpak_compatible_mount(uri, user, host, None, error_callback)
        
        return False, error_msg

def _mount_and_open_sftp_sync(uri: str, user: str, host: str, progress_dialog=None) -> Tuple[bool, Optional[str]]:
    """Mount SFTP location and open in file manager synchronously"""
    
    result = {"success": False, "error": None, "completed": False}
    
    def on_mounted(source, res, data=None):
        try:
            source.mount_enclosing_volume_finish(res)
            logger.info(f"SFTP mount successful, opening file manager...")

            # Update progress dialog
            if progress_dialog:
                progress_dialog.update_progress(1.0, "Mount successful! Opening file manager...")

            # Now it's mounted → open in the default file manager
            Gio.AppInfo.launch_default_for_uri(uri, None)
            logger.info(f"File manager launched successfully")

            result["success"] = True
            result["completed"] = True

        except GLib.Error as e:
            # Check if the error is "already mounted" - this is actually a success case
            if "already mounted" in e.message.lower():
                logger.info(f"SFTP location already mounted, opening file manager...")

                # Update progress dialog
                if progress_dialog:
                    progress_dialog.update_progress(1.0, "Location already mounted! Opening file manager...")

                # Open in the default file manager
                try:
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                    logger.info(f"File manager launched successfully")
                    result["success"] = True
                except Exception as launch_error:
                    result["error"] = f"Failed to launch file manager: {str(launch_error)}"
            else:
                error_msg = f"Could not mount {uri}: {e.message}"
                logger.error(f"Mount failed: {error_msg}")
                result["error"] = error_msg
            
            result["completed"] = True
            
        except Exception as e:
            error_msg = f"Unexpected error during mount: {str(e)}"
            logger.error(f"Mount error: {e}")
            result["error"] = error_msg
            result["completed"] = True

    try:
        gfile = Gio.File.new_for_uri(uri)
        op = Gio.MountOperation()

        # Start the async mount operation
        gfile.mount_enclosing_volume(
            Gio.MountMountFlags.NONE,
            op,
            None,  # cancellable
            on_mounted,
            None
        )

        # Wait for completion with timeout
        timeout = 30  # 30 seconds timeout
        start_time = time.time()
        
        while not result["completed"] and (time.time() - start_time) < timeout:
            # Process GTK events to allow the callback to execute
            while Gtk.events_pending():
                Gtk.main_iteration()
            time.sleep(0.1)
        
        if not result["completed"]:
            return False, "Mount operation timed out"
        
        return result["success"], result["error"]
        
    except Exception as e:
        error_msg = f"Failed to start mount operation: {str(e)}"
        logger.error(f"Mount operation failed: {e}")
        return False, error_msg

def _verify_ssh_connection(user: str, host: str, port: Optional[int]) -> bool:
    """Verify SSH connection without full mount"""
    ssh_cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", 
               "-o", "StrictHostKeyChecking=accept-new"]
    if port:
        ssh_cmd.extend(["-p", str(port)])
    ssh_cmd.extend([f"{user}@{host}", "echo", "READY"])
    
    try:
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False

def _verify_ssh_connection_async(user: str, host: str, port: Optional[int], callback: Callable[[bool], None]) -> None:
    """Verify SSH connection on a background thread and invoke callback with the result"""

    def worker():
        success = _verify_ssh_connection(user, host, port)
        GLib.idle_add(callback, success)

    threading.Thread(target=worker, daemon=True).start()

def _open_sftp_flatpak_compatible(uri: str, user: str, host: str, port: Optional[int], 
                                 error_callback: Optional[Callable], parent_window=None) -> Tuple[bool, Optional[str]]:
    """Open SFTP using Flatpak-compatible methods with proper portal usage"""
    
    # Create and show progress dialog immediately
    progress_dialog = MountProgressDialog(user, host, parent_window)
    progress_dialog.present()
    progress_dialog.start_progress_updates()
    
    # Method 1: Use XDG Desktop Portal File Chooser to access GVFS mounts
    try:
        progress_dialog.update_progress(0.2, "Trying portal file access...")
        success = _try_portal_file_access(uri, user, host)
        if success:
            progress_dialog.update_progress(1.0, "Success! Opening file manager...")
            GLib.timeout_add(1000, progress_dialog.close)
            return True, None
    except Exception as e:
        logger.warning(f"Portal file access failed: {e}")
    
    # Method 2: Try to launch external file manager that can handle SFTP
    try:
        progress_dialog.update_progress(0.4, "Trying external file managers...")
        success = _try_external_file_managers(uri, user, host)
        if success:
            progress_dialog.update_progress(1.0, "Success! File manager opened...")
            GLib.timeout_add(1000, progress_dialog.close)
            return True, None
    except Exception as e:
        logger.warning(f"External file managers failed: {e}")
    
    # Method 3: Use host's GVFS if accessible
    try:
        progress_dialog.update_progress(0.6, "Checking host GVFS mounts...")
        success = _try_host_gvfs_access(uri, user, host, port)
        if success:
            progress_dialog.update_progress(1.0, "Success! Found existing mount...")
            GLib.timeout_add(1000, progress_dialog.close)
            return True, None
    except Exception as e:
        logger.warning(f"Host GVFS access failed: {e}")
    
    # Method 4: Show connection dialog for manual setup
    progress_dialog.update_progress(0.8, "Preparing manual connection options...")
    success = _show_manual_connection_dialog(user, host, port, uri)
    if success:
        progress_dialog.close()
        return True, "Manual connection dialog opened"
    
    # All methods failed
    error_msg = "Could not open SFTP connection - try mounting the location manually first"
    progress_dialog.show_error(error_msg)
    if error_callback:
        error_callback(error_msg)
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
        if subprocess.run(["which", "flatpak-spawn"], capture_output=True).returncode == 0:
            # Check if gio is available on host (it should be)
            check_cmd = ["flatpak-spawn", "--host", "which", "gio"]
            if subprocess.run(check_cmd, capture_output=True).returncode == 0:
                logger.info(f"Using gio mount for {uri}")
                
                # Mount using host's gio mount
                mount_cmd = ["flatpak-spawn", "--host", "gio", "mount", uri]
                result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
                
                logger.info(f"gio mount result: returncode={result.returncode}, stdout={result.stdout}, stderr={result.stderr}")
                
                if result.returncode == 0 or "already mounted" in result.stderr.lower() or "Operation not supported" not in result.stderr:
                    logger.info(f"gio mount successful or location already accessible")
                    
                    # Give it a moment for the mount to be ready
                    import time
                    time.sleep(1)
                    
                    # Find the actual mount point and open that instead of the URI
                    mount_point = _find_gvfs_mount_point(user, host)
                    if mount_point:
                        open_cmd = ["flatpak-spawn", "--host", "xdg-open", mount_point]
                        subprocess.Popen(open_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        logger.info(f"File manager opened at mount point: {mount_point}")
                        return True
                    else:
                        # Try opening the URI directly with specific file managers
                        logger.info("Mount point not found, trying file managers with URI")
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
        f"/var/run/user/{os.getuid()}/gvfs"
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
        ["flatpak-spawn", "--host", "nemo", uri]
    ]
    
    for cmd in managers:
        try:
            # Check if the file manager exists on host
            check_cmd = ["flatpak-spawn", "--host", "which", cmd[2]]
            if subprocess.run(check_cmd, capture_output=True).returncode == 0:
                logger.info(f"Trying {cmd[2]} with SFTP URI")
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        ("thunar", [uri]),    # Good SFTP support
        ("dolphin", [uri]),   # KDE file manager
        ("nemo", [uri]),      # Cinnamon file manager
        ("pcmanfm", [uri]),   # Lightweight option
    ]
    
    for manager, cmd in managers:
        try:
            # Use flatpak-spawn to run on the host if available
            if subprocess.run(["which", "flatpak-spawn"], capture_output=True).returncode == 0:
                # Check if manager exists on host
                check_cmd = ["flatpak-spawn", "--host", "which", manager]
                if subprocess.run(check_cmd, capture_output=True).returncode == 0:
                    spawn_cmd = ["flatpak-spawn", "--host", manager, uri]
                    subprocess.Popen(spawn_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    logger.info(f"Launched {manager} via flatpak-spawn with URI")
                    return True
            
            # Try direct launch as fallback
            elif subprocess.run(["which", manager], capture_output=True).returncode == 0:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        f"sftp://{host}/"
    ]
    
    for location in network_locations:
        try:
            if subprocess.run(["which", "flatpak-spawn"], capture_output=True).returncode == 0:
                cmd = ["flatpak-spawn", "--host", "nautilus", location]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info(f"Opened network location: {location}")
                return True
        except Exception as e:
            logger.debug(f"Failed to open {location}: {e}")
    
    # Method 2: Try using gio mount command
    try:
        if subprocess.run(["which", "flatpak-spawn"], capture_output=True).returncode == 0:
            # Check if gio is available
            check_cmd = ["flatpak-spawn", "--host", "which", "gio"]
            if subprocess.run(check_cmd, capture_output=True).returncode == 0:
                # Use gio to mount
                mount_cmd = ["flatpak-spawn", "--host", "gio", "mount", uri]
                result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
                
                if result.returncode == 0:
                    # Try to open with nautilus after mounting
                    open_cmd = ["flatpak-spawn", "--host", "nautilus", uri]
                    subprocess.Popen(open_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    logger.info(f"Mounted with gio and opened with nautilus")
                    return True
    except Exception as e:
        logger.debug(f"gio mount failed: {e}")
    
    return False

def _try_host_gvfs_access(uri: str, user: str, host: str, port: Optional[int]) -> bool:
    """Try accessing GVFS mounts on the host system"""
    
    # Check if we can access the host's GVFS mounts
    gvfs_paths = [
        f"/run/user/{os.getuid()}/gvfs",
        f"/var/run/user/{os.getuid()}/gvfs",
        f"{os.path.expanduser('~')}/.gvfs"
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
                        subprocess.Popen(["xdg-open", mount_path], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        logger.info(f"Opened existing GVFS mount: {mount_path}")
                        return True
            except Exception as e:
                logger.debug(f"Could not access GVFS path {gvfs_path}: {e}")
                continue
    
    return False

def _show_manual_connection_dialog(user: str, host: str, port: Optional[int], uri: str) -> bool:
    """Show dialog with manual connection instructions"""
    
    dialog = SftpConnectionDialog(user, host, port, uri)
    dialog.present()
    return True

def _open_sftp_native(uri: str, user: str, host: str, error_callback: Optional[Callable], parent_window=None) -> Tuple[bool, Optional[str]]:
    """Native installation SFTP opening with GVFS"""
    
    # Try direct GVFS mount first
    try:
        success = _mount_and_open_sftp_native(uri, user, host, error_callback, parent_window)
        if success:
            return True, None
    except Exception as e:
        logger.warning(f"Native GVFS mount failed: {e}")
    
    # Fall back to Flatpak-compatible methods
    return _open_sftp_flatpak_compatible(uri, user, host, None, error_callback, parent_window)

def _mount_and_open_sftp_native(uri: str, user: str, host: str, error_callback: Optional[Callable], parent_window=None) -> bool:
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
            progress_dialog.update_progress(1.0, "Mount successful! Opening file manager...")
            Gio.AppInfo.launch_default_for_uri(uri, None)
            GLib.timeout_add(1000, progress_dialog.close)
            
        except GLib.Error as e:
            if "already mounted" in e.message.lower():
                logger.info(f"SFTP location already mounted for {user}@{host}")
                progress_dialog.update_progress(1.0, "Location already mounted! Opening file manager...")
                Gio.AppInfo.launch_default_for_uri(uri, None)
                GLib.timeout_add(1000, progress_dialog.close)
            else:
                error_msg = f"Could not mount {uri}: {e.message}"
                logger.error(f"Mount failed for {user}@{host}: {error_msg}")
                progress_dialog.show_error(error_msg)
                if error_callback:
                    error_callback(error_msg)
    
    progress_dialog.start_progress_updates()
    gfile.mount_enclosing_volume(Gio.MountMountFlags.NONE, op, None, on_mounted, None)
    logger.info(f"Mount operation started for {user}@{host}")
    return True

def _try_flatpak_compatible_mount(uri: str, user: str, host: str, port: int|None, error_callback=None):
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
        ("thunar", [uri]),    # XFCE Thunar
        ("dolphin", [uri]),   # KDE Dolphin
        ("pcmanfm", [uri]),   # PCManFM
        ("nemo", [uri]),      # Nemo
    ]
    
    for manager, cmd in external_managers:
        try:
            # Check if the file manager exists
            if subprocess.run(["which", manager], capture_output=True).returncode == 0:
                logger.info(f"Trying external file manager: {manager}")
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info(f"External file manager {manager} launched for {user}@{host}")
                return True, None
        except Exception as e:
            logger.warning(f"Failed to launch {manager}: {e}")
    
    # Method 3: Try mounting via command line tools
    return _try_command_line_mount(user, host, port, error_callback)

def _try_command_line_mount(user: str, host: str, port: int|None, error_callback=None):
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
        
        sshfs_cmd.extend([
            "-o", "reconnect,ServerAliveInterval=15,ServerAliveCountMax=3",
            f"{user}@{host}:/",
            mount_point
        ])
        
        logger.info(f"Trying sshfs mount: {' '.join(sshfs_cmd)}")
        result = subprocess.run(sshfs_cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            logger.info(f"sshfs mount successful at {mount_point}")
            # Open the mount point in file manager
            try:
                subprocess.Popen(["xdg-open", mount_point])
                return True, None
            except Exception as e:
                logger.warning(f"Failed to open mount point: {e}")
                return True, f"Mounted at {mount_point} but couldn't open file manager"
        else:
            logger.warning(f"sshfs failed: {result.stderr}")
            
    except subprocess.TimeoutExpired:
        logger.error("sshfs mount timeout")
    except Exception as e:
        logger.error(f"sshfs mount error: {e}")
    
    # Method 4: Fall back to terminal-based SFTP client
    return _launch_terminal_sftp(user, host, port, error_callback)

def _launch_terminal_sftp(user: str, host: str, port: int|None, error_callback=None):
    """Launch terminal-based SFTP client as last resort"""
    
    terminals = ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"]
    
    sftp_cmd = ["sftp"]
    if port:
        sftp_cmd.extend(["-P", str(port)])
    sftp_cmd.append(f"{user}@{host}")
    
    for terminal in terminals:
        try:
            if subprocess.run(["which", terminal], capture_output=True).returncode == 0:
                logger.info(f"Launching terminal SFTP with {terminal}")
                
                if terminal == "gnome-terminal":
                    cmd = [terminal, "--", *sftp_cmd]
                elif terminal == "konsole":
                    cmd = [terminal, "-e", *sftp_cmd]
                elif terminal == "xfce4-terminal":
                    cmd = [terminal, "-e", " ".join(sftp_cmd)]
                else:
                    cmd = [terminal, "-e", " ".join(sftp_cmd)]
                
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True, f"Opened SFTP connection in {terminal}"
                
        except Exception as e:
            logger.warning(f"Failed to launch {terminal}: {e}")
    
    error_msg = "Could not open SFTP connection - no compatible file manager or terminal found"
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
        self.cancel_button.connect('clicked', self._on_cancel)
        self.cancel_button.set_halign(Gtk.Align.CENTER)
        main_box.append(self.cancel_button)
        
        self.set_content(main_box)
    
    def _on_cancel(self, button):
        """Cancel mount operation"""
        self.is_cancelled = True
        if self.progress_timer:
            GLib.source_remove(self.progress_timer)
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
            
        self.update_progress(self.progress_value, "Establishing SFTP connection...")
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
            lambda: self._try_file_manager()
        )
        options_box.append(option1_box)
        
        # Option 2: Copy URI
        option2_box = self._create_option_box(
            "Copy SFTP URI",
            f"Copy the SFTP URI to clipboard: {uri}",
            "edit-copy-symbolic", 
            lambda: self._copy_uri()
        )
        options_box.append(option2_box)
        
        # Option 3: Terminal
        option3_box = self._create_option_box(
            "Open in Terminal",
            "Open an SFTP connection in terminal",
            "utilities-terminal-symbolic",
            lambda: self._open_terminal()
        )
        options_box.append(option3_box)
        
        main_box.append(options_box)
        
        # Close button
        close_button = Gtk.Button.new_with_label("Close")
        close_button.connect('clicked', lambda b: self.close())
        close_button.set_halign(Gtk.Align.END)
        main_box.append(close_button)
        
        self.set_content(main_box)
        
    def _create_option_box(self, title: str, description: str, icon_name: str, callback: Callable) -> Gtk.Box:
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
        button.connect('clicked', lambda b: callback())
        box.append(button)
        
        return box
    
    def _try_file_manager(self):
        """Try opening in file manager"""
        try:
            Gio.AppInfo.launch_default_for_uri(self.uri, None)
        except Exception:
            # Fall back to xdg-open
            subprocess.Popen(["xdg-open", self.uri], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
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
                if subprocess.run(["which", terminal], capture_output=True).returncode == 0:
                    if terminal == "gnome-terminal":
                        cmd = [terminal, "--", *sftp_cmd]
                    elif terminal == "konsole":
                        cmd = [terminal, "-e", *sftp_cmd] 
                    else:
                        cmd = [terminal, "-e", " ".join(sftp_cmd)]
                    
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
            except Exception:
                continue


# =========================
# SSH Copy-ID Full Window
# =========================
class SshCopyIdWindow(Adw.Window):
    """
    Full Adwaita-styled window for installing a public key on a server.
    - Shows selected server nickname
    - Two modes:
        1) Use existing key (DropDown)
        2) Generate new key (embedded key-generator form)
    - Pressing OK triggers:
        - Either copy selected existing key
        - Or generate a new key, then copy it
    - Uses your existing terminal flow:
        parent._show_ssh_copy_id_terminal_using_main_widget(connection, ssh_key)
    """

    def __init__(self, parent, connection, key_manager, connection_manager):
        logger.info("SshCopyIdWindow: Initializing window")
        logger.debug(f"SshCopyIdWindow: Constructor called with connection: {getattr(connection, 'nickname', 'unknown')}")
        logger.debug(f"SshCopyIdWindow: Connection object type: {type(connection)}")
        logger.debug(f"SshCopyIdWindow: Key manager type: {type(key_manager)}")
        logger.debug(f"SshCopyIdWindow: Connection manager type: {type(connection_manager)}")
        
        try:
            super().__init__(transient_for=parent, modal=False)
            self.set_title("Install Public Key on Server")
            self.set_resizable(False)
            logger.debug("SshCopyIdWindow: Base window initialized")

            self._parent = parent
            self._conn = connection
            self._km = key_manager
            self._cm = connection_manager
            logger.debug("SshCopyIdWindow: Instance variables set")
            
            logger.info(f"SshCopyIdWindow: Window initialized for connection {getattr(connection, 'nickname', 'unknown')}")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to initialize window: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
            raise

        # ---------- Outer layout ----------
        logger.info("SshCopyIdWindow: Creating outer layout")
        try:
            tv = Adw.ToolbarView()
            self.set_content(tv)
            
            # ---------- Header Bar ----------
            logger.info("SshCopyIdWindow: Creating header bar")
            hb = Adw.HeaderBar()
            tv.add_top_bar(hb)

            # Cancel button
            btn_cancel = Gtk.Button(label="Cancel")
            btn_cancel.connect("clicked", self._on_close_clicked)
            hb.pack_start(btn_cancel)

            self.btn_ok = Gtk.Button(label="OK")
            self.btn_ok.add_css_class("suggested-action")
            self.btn_ok.connect("clicked", self._on_ok_clicked)
            hb.pack_end(self.btn_ok)
            logger.info("SshCopyIdWindow: Header bar created successfully")

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content.set_margin_top(18); content.set_margin_bottom(18)
            content.set_margin_start(18); content.set_margin_end(18)
            tv.set_content(content)

            # ---------- Intro text ----------
            server_name = getattr(self._conn, "nickname", None) or \
                          f"{getattr(self._conn, 'username', 'user')}@{getattr(self._conn, 'host', 'host')}"
            
            # Create a simple label instead of StatusPage for normal font size
            intro_label = Gtk.Label()
            intro_label.set_markup(f'Copy your public key to "{server_name}".')
            intro_label.set_halign(Gtk.Align.CENTER)
            intro_label.set_margin_bottom(12)
            content.append(intro_label)
            logger.info(f"SshCopyIdWindow: Intro text created for server: {server_name}")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to create outer layout: {e}")
            raise

        # ---------- Options group ----------
        logger.info("SshCopyIdWindow: Creating options group")
        try:
            group = Adw.PreferencesGroup(title="")

            # Radio option 1: Use existing key (using CheckButton with group for radio behavior)
            self.radio_existing = Gtk.CheckButton(label="Copy existing key")
            self.radio_generate = Gtk.CheckButton(label="Generate new key")

            # Make them behave like radio buttons (GTK4)
            self.radio_generate.set_group(self.radio_existing)
            self.radio_existing.set_active(True)
            logger.info("SshCopyIdWindow: Radio buttons created successfully")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to create radio buttons: {e}")
            raise

        # Existing key row with dropdown
        logger.info("SshCopyIdWindow: Creating existing key dropdown")
        try:
            existing_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            existing_box.set_margin_start(12)
            existing_box.set_margin_bottom(6)
            self.dropdown_existing = Gtk.DropDown()
            existing_box.append(Gtk.Label(label="Select key:", xalign=0))
            existing_box.append(self.dropdown_existing)

            # Fill dropdown with discovered keys
            self._reload_existing_keys()
            logger.info("SshCopyIdWindow: Existing key dropdown created successfully")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to create existing key dropdown: {e}")
            raise

        # Generate form (embedded)
        logger.info("SshCopyIdWindow: Creating key generation form")
        try:
            self.generate_revealer = Gtk.Revealer()
            self.generate_revealer.set_reveal_child(False)
            self.generate_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)

            gen_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            gen_box.set_margin_start(12)
            gen_box.set_margin_top(6)

            # Key name
            self.row_key_name = Adw.EntryRow()
            self.row_key_name.set_title("Key file name")
            self.row_key_name.set_text("id_ed25519")
            gen_box.append(self.row_key_name)

            # Key type
            self.type_row = Adw.ComboRow()
            self.type_row.set_title("Key type")
            self._types_model = Gtk.StringList.new(["ed25519", "rsa"])
            self.type_row.set_model(self._types_model)
            self.type_row.set_selected(0)
            gen_box.append(self.type_row)

            # Passphrase toggle + entries
            self.row_pass_toggle = Adw.SwitchRow()
            self.row_pass_toggle.set_title("Encrypt with passphrase")
            gen_box.append(self.row_pass_toggle)

            pass_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self.pass1 = Gtk.PasswordEntry()
            self.pass1.set_property("placeholder-text", "Passphrase")
            self.pass2 = Gtk.PasswordEntry()
            self.pass2.set_property("placeholder-text", "Confirm passphrase")
            pass_box.append(self.pass1); pass_box.append(self.pass2)
            pass_box.set_visible(False)
            gen_box.append(pass_box)

            def _on_pass_toggle(*_):
                pass_box.set_visible(self.row_pass_toggle.get_active())
            self.row_pass_toggle.connect("notify::active", _on_pass_toggle)

            self.generate_revealer.set_child(gen_box)
            logger.info("SshCopyIdWindow: Key generation form created successfully")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to create key generation form: {e}")
            raise

        # Pack into PreferencesGroup
        logger.info("SshCopyIdWindow: Packing UI elements")
        try:
            # Row 1: Existing
            existing_row = Adw.ActionRow()
            existing_row.add_prefix(self.radio_existing)
            existing_row.add_suffix(existing_box)
            group.add(existing_row)

            # Row 2: Generate
            generate_row = Adw.ActionRow()
            generate_row.add_prefix(self.radio_generate)
            group.add(generate_row)
            # Embedded generator UI under row 2
            group.add(self.generate_revealer)

            content.append(group)

            # Radio change behavior
            self.radio_existing.connect("toggled", self._on_mode_toggled)
            self.radio_generate.connect("toggled", self._on_mode_toggled)

            logger.info("SshCopyIdWindow: UI elements packed successfully")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to pack UI elements: {e}")
            raise

        logger.info("SshCopyIdWindow: Window construction completed, presenting")
        self.present()

    # ---------- Helpers ----------

    def _on_mode_toggled(self, *_):
        # Reveal generator only when "Generate new key" is selected
        logger.info(f"SshCopyIdWindow: Mode toggled, generate active: {self.radio_generate.get_active()}")
        self.generate_revealer.set_reveal_child(self.radio_generate.get_active())

    def _reload_existing_keys(self):
        logger.info("SshCopyIdWindow: Reloading existing keys")
        logger.debug("SshCopyIdWindow: Calling key_manager.discover_keys()")
        try:
            keys = self._km.discover_keys()
            logger.info(f"SshCopyIdWindow: Discovered {len(keys)} keys")
            logger.debug(f"SshCopyIdWindow: Key discovery returned {len(keys)} keys")
            
            # Log details of each discovered key
            for i, key in enumerate(keys):
                logger.debug(f"SshCopyIdWindow: Key {i+1}: private_path='{key.private_path}', "
                           f"public_path='{key.public_path}', exists={os.path.exists(key.private_path)}")
            
            names = [os.path.basename(k.private_path) for k in keys] or ["No keys found"]
            logger.debug(f"SshCopyIdWindow: Key names for dropdown: {names}")
            
            dd = Gtk.DropDown.new_from_strings(names)
            if keys:
                dd.set_selected(0)
                logger.debug(f"SshCopyIdWindow: Selected first key in dropdown")
            
            self.dropdown_existing.set_model(dd.get_model())
            self.dropdown_existing.set_selected(dd.get_selected())
            # keep a cached list to resolve on OK
            self._existing_keys_cache = keys
            logger.info(f"SshCopyIdWindow: Dropdown populated with {len(names)} items")
            logger.debug(f"SshCopyIdWindow: Cached {len(keys)} keys for later use")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to load existing keys: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
            self._existing_keys_cache = []
            dd = Gtk.DropDown.new_from_strings(["Error loading keys"])
            self.dropdown_existing.set_model(dd.get_model())
            self.dropdown_existing.set_selected(0)

    def _info(self, title, body):
        try:
            md = Adw.MessageDialog(transient_for=self, modal=True, heading=title, body=body)
            md.add_response("ok", "OK")
            md.set_default_response("ok")
            md.set_close_response("ok")
            md.present()
        except Exception:
            pass

    def _error(self, title, body, detail=""):
        try:
            text = body + (f"\n\n{detail}" if detail else "")
            md = Adw.MessageDialog(transient_for=self, modal=True, heading=title, body=text)
            md.add_response("close", "Close")
            md.set_default_response("close")
            md.set_close_response("close")
            md.present()
        except Exception:
            logger.error("%s: %s | %s", title, body, detail)

    def _on_close_clicked(self, *_):
        logger.info("SshCopyIdWindow: Close button clicked")
        self.close()

    # ---------- OK (main action) ----------
    def _on_ok_clicked(self, *_):
        logger.info("SshCopyIdWindow: OK button clicked")
        logger.debug("SshCopyIdWindow: Starting main action processing")
        
        # Log current UI state
        existing_active = self.radio_existing.get_active()
        generate_active = self.radio_generate.get_active()
        logger.debug(f"SshCopyIdWindow: UI state - existing_active={existing_active}, generate_active={generate_active}")
        
        try:
            if self.radio_existing.get_active():
                logger.info("SshCopyIdWindow: Copying existing key")
                logger.debug("SshCopyIdWindow: Calling _do_copy_existing()")
                self._do_copy_existing()
            else:
                logger.info("SshCopyIdWindow: Generating new key and copying")
                logger.debug("SshCopyIdWindow: Calling _do_generate_and_copy()")
                self._do_generate_and_copy()
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Operation failed: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
            self._error("Operation failed", "Could not start the requested action.", str(e))

    # ---------- Mode: existing ----------
    def _do_copy_existing(self):
        logger.info("SshCopyIdWindow: Starting copy existing key operation")
        logger.debug("SshCopyIdWindow: Processing existing key selection")
        
        try:
            keys = getattr(self, "_existing_keys_cache", []) or []
            logger.info(f"SshCopyIdWindow: Found {len(keys)} cached keys")
            logger.debug(f"SshCopyIdWindow: Cached keys list length: {len(keys)}")
            
            if not keys:
                logger.debug("SshCopyIdWindow: No cached keys available")
                raise RuntimeError("No keys available in ~/.ssh")
            
            idx = self.dropdown_existing.get_selected()
            logger.info(f"SshCopyIdWindow: Selected key index: {idx}")
            logger.debug(f"SshCopyIdWindow: Dropdown selection index: {idx}")
            
            if idx < 0 or idx >= len(keys):
                logger.debug(f"SshCopyIdWindow: Invalid index {idx} for keys list of length {len(keys)}")
                raise RuntimeError("Please select a key to copy")
            
            ssh_key = keys[idx]
            logger.info(f"SshCopyIdWindow: Selected key: {ssh_key.private_path}")
            logger.debug(f"SshCopyIdWindow: Selected key details - private_path='{ssh_key.private_path}', "
                       f"public_path='{ssh_key.public_path}', exists={os.path.exists(ssh_key.private_path)}")
            
            # Launch your existing terminal ssh-copy-id flow
            logger.debug("SshCopyIdWindow: Calling _show_ssh_copy_id_terminal_using_main_widget()")
            self._parent._show_ssh_copy_id_terminal_using_main_widget(self._conn, ssh_key)
            logger.debug("SshCopyIdWindow: Terminal window launched, closing dialog")
            self.close()
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Copy existing failed: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
            self._error("Copy failed", "Could not copy the selected key to the server.", str(e))

    # ---------- Mode: generate ----------
    def _do_generate_and_copy(self):
        logger.info("SshCopyIdWindow: Starting generate and copy operation")
        logger.debug("SshCopyIdWindow: Processing key generation request")
        
        try:
            key_name = (self.row_key_name.get_text() or "").strip()
            logger.info(f"SshCopyIdWindow: Key name: '{key_name}'")
            logger.debug(f"SshCopyIdWindow: Raw key name from UI: '{self.row_key_name.get_text()}'")
            
            if not key_name:
                logger.debug("SshCopyIdWindow: Empty key name provided")
                raise ValueError("Enter a key file name (e.g. id_ed25519)")
            if "/" in key_name or key_name.startswith("."):
                logger.debug(f"SshCopyIdWindow: Invalid key name '{key_name}' - contains '/' or starts with '.'")
                raise ValueError("Key file name must not contain '/' or start with '.'")

            # Key type
            type_selection = self.type_row.get_selected()
            kt = "ed25519" if type_selection == 0 else "rsa"
            logger.info(f"SshCopyIdWindow: Key type: {kt}")
            logger.debug(f"SshCopyIdWindow: Type selection index: {type_selection}, resolved to: {kt}")

            passphrase = None
            passphrase_enabled = self.row_pass_toggle.get_active()
            logger.debug(f"SshCopyIdWindow: Passphrase toggle state: {passphrase_enabled}")
            
            if passphrase_enabled:
                p1 = self.pass1.get_text() or ""
                p2 = self.pass2.get_text() or ""
                logger.debug(f"SshCopyIdWindow: Passphrase lengths - p1: {len(p1)}, p2: {len(p2)}")
                if p1 != p2:
                    logger.debug("SshCopyIdWindow: Passphrases do not match")
                    raise ValueError("Passphrases do not match")
                passphrase = p1
                logger.info("SshCopyIdWindow: Passphrase enabled")
                logger.debug("SshCopyIdWindow: Passphrase validation successful")

            logger.info(f"SshCopyIdWindow: Calling key_manager.generate_key with name='{key_name}', type='{kt}'")
            logger.debug(f"SshCopyIdWindow: Key generation parameters - name='{key_name}', type='{kt}', "
                       f"size={3072 if kt == 'rsa' else 0}, passphrase={'<set>' if passphrase else 'None'}")
            
            new_key = self._km.generate_key(
                key_name=key_name,
                key_type=kt,
                key_size=3072 if kt == "rsa" else 0,
                comment=None,
                passphrase=passphrase,
            )
            
            if not new_key:
                logger.debug("SshCopyIdWindow: Key generation returned None")
                raise RuntimeError("Key generation failed. See logs for details.")

            logger.info(f"SshCopyIdWindow: Key generated successfully: {new_key.private_path}")
            logger.debug(f"SshCopyIdWindow: Generated key details - private_path='{new_key.private_path}', "
                       f"public_path='{new_key.public_path}'")
            
            # Ensure the key files are properly written and accessible
            import time
            logger.debug("SshCopyIdWindow: Waiting 0.5s for files to be written")
            time.sleep(0.5)  # Small delay to ensure files are written
            
            # Verify the key files exist and are accessible
            private_exists = os.path.exists(new_key.private_path)
            public_exists = os.path.exists(new_key.public_path)
            logger.debug(f"SshCopyIdWindow: File existence check - private: {private_exists}, public: {public_exists}")
            
            if not private_exists:
                logger.debug(f"SshCopyIdWindow: Private key file missing: {new_key.private_path}")
                raise RuntimeError(f"Private key file not found: {new_key.private_path}")
            if not public_exists:
                logger.debug(f"SshCopyIdWindow: Public key file missing: {new_key.public_path}")
                raise RuntimeError(f"Public key file not found: {new_key.public_path}")
            
            logger.info(f"SshCopyIdWindow: Key files verified, starting ssh-copy-id")
            logger.debug("SshCopyIdWindow: All key files verified successfully")
            
            # Run your terminal ssh-copy-id flow
            logger.debug("SshCopyIdWindow: Calling _show_ssh_copy_id_terminal_using_main_widget()")
            self._parent._show_ssh_copy_id_terminal_using_main_widget(self._conn, new_key)
            logger.debug("SshCopyIdWindow: Terminal window launched, closing dialog")
            self.close()

        except Exception as e:
            logger.error(f"SshCopyIdWindow: Generate and copy failed: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
            self._error("Generate & Copy failed",
                        "Could not generate a new key and copy it to the server.",
                        str(e))


class ConnectionRow(Gtk.ListBoxRow):
    """Row widget for connection list"""
    
    def __init__(self, connection: Connection):
        super().__init__()
        self.connection = connection
        
        # Create overlay for pulse effect
        overlay = Gtk.Overlay()
        self.set_child(overlay)
        
        # Create main content box
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        
        # Connection icon
        icon = Gtk.Image.new_from_icon_name('computer-symbolic')
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        content.append(icon)
        
        # Connection info
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        
        # Nickname label
        self.nickname_label = Gtk.Label()
        self.nickname_label.set_markup(f"<b>{connection.nickname}</b>")
        self.nickname_label.set_halign(Gtk.Align.START)
        info_box.append(self.nickname_label)
        
        # Host info label (may be hidden based on user setting)
        self.host_label = Gtk.Label()
        self.host_label.set_halign(Gtk.Align.START)
        self.host_label.add_css_class('dim-label')
        self._apply_host_label_text()
        info_box.append(self.host_label)
        
        content.append(info_box)
        
        # Port forwarding indicators (L/R/D)
        self.indicator_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.indicator_box.set_halign(Gtk.Align.CENTER)
        self.indicator_box.set_valign(Gtk.Align.CENTER)
        content.append(self.indicator_box)

        # Connection status indicator
        self.status_icon = Gtk.Image.new_from_icon_name('network-offline-symbolic')
        self.status_icon.set_pixel_size(16)  # GTK4 uses pixel size instead of IconSize
        content.append(self.status_icon)
        
        # Set content as the main child of overlay
        overlay.set_child(content)
        
        # Create pulse layer
        self._pulse = Gtk.Box()
        self._pulse.add_css_class("pulse-highlight")
        self._pulse.set_can_target(False)  # Don't intercept mouse events
        self._pulse.set_hexpand(True)
        self._pulse.set_vexpand(True)
        overlay.add_overlay(self._pulse)
        
        self.set_selectable(True)  # Make the row selectable for keyboard navigation
        
        # Update status
        self.update_status()
        # Update forwarding indicators
        self._update_forwarding_indicators()

    @staticmethod
    def _install_pf_css():
        try:
            # Install CSS for port forwarding indicator badges once per display
            display = Gdk.Display.get_default()
            if not display:
                return
            # Use an attribute on the display to avoid re-adding provider
            if getattr(display, '_pf_css_installed', False):
                return
            provider = Gtk.CssProvider()
            css = """
            .pf-indicator { /* kept for legacy, not used by circled glyphs */ }
            .pf-local { color: #E01B24; }
            .pf-remote { color: #2EC27E; }
            .pf-dynamic { color: #3584E4; }
            """
            provider.load_from_data(css.encode('utf-8'))
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            setattr(display, '_pf_css_installed', True)
        except Exception:
            pass



    def _update_forwarding_indicators(self):
        # Ensure CSS exists
        self._install_pf_css()
        # Clear previous indicators
        try:
            while self.indicator_box.get_first_child():
                self.indicator_box.remove(self.indicator_box.get_first_child())
        except Exception:
            return

        rules = getattr(self.connection, 'forwarding_rules', []) or []
        has_local = any(r.get('enabled', True) and r.get('type') == 'local' for r in rules)
        has_remote = any(r.get('enabled', True) and r.get('type') == 'remote' for r in rules)
        has_dynamic = any(r.get('enabled', True) and r.get('type') == 'dynamic' for r in rules)

        def make_badge(letter: str, cls: str):
            # Use Unicode precomposed circled letters for perfect centering
            circled_map = {
                'L': '\u24C1',  # Ⓛ
                'R': '\u24C7',  # Ⓡ
                'D': '\u24B9',  # Ⓓ
            }
            glyph = circled_map.get(letter, letter)
            lbl = Gtk.Label(label=glyph)
            lbl.add_css_class(cls)
            lbl.set_halign(Gtk.Align.CENTER)
            lbl.set_valign(Gtk.Align.CENTER)
            try:
                lbl.set_xalign(0.5)
                lbl.set_yalign(0.5)
            except Exception:
                pass
            return lbl

        if has_local:
            self.indicator_box.append(make_badge('L', 'pf-local'))
        if has_remote:
            self.indicator_box.append(make_badge('R', 'pf-remote'))
        if has_dynamic:
            self.indicator_box.append(make_badge('D', 'pf-dynamic'))

    def _apply_host_label_text(self):
        try:
            window = self.get_root()
            hide = bool(getattr(window, '_hide_hosts', False)) if window else False
        except Exception:
            hide = False
        if hide:
            self.host_label.set_text('••••••••••')
        else:
            self.host_label.set_text(f"{self.connection.username}@{self.connection.host}")

    def apply_hide_hosts(self, hide: bool):
        """Called by window when hide/show toggles."""
        self._apply_host_label_text()

    def update_status(self):
        """Update connection status display"""
        try:
            # Check if there's any active terminal for this connection
            window = self.get_root()
            has_active_terminal = False

            # Prefer multi-tab map if present; fallback to most-recent mapping
            if hasattr(window, 'connection_to_terminals') and self.connection in getattr(window, 'connection_to_terminals', {}):
                for t in window.connection_to_terminals.get(self.connection, []) or []:
                    if getattr(t, 'is_connected', False):
                        has_active_terminal = True
                        break
            elif hasattr(window, 'active_terminals') and self.connection in window.active_terminals:
                terminal = window.active_terminals[self.connection]
                # Check if the terminal is still valid and connected
                if terminal and hasattr(terminal, 'is_connected'):
                    has_active_terminal = terminal.is_connected
            
            # Update the connection's is_connected status
            self.connection.is_connected = has_active_terminal
            
            # Log the status update for debugging
            logger.debug(f"Updating status for {self.connection.nickname}: is_connected={has_active_terminal}")
            
            # Update the UI based on the connection status
            if has_active_terminal:
                self.status_icon.set_from_icon_name('network-idle-symbolic')
                self.status_icon.set_tooltip_text(f'Connected to {getattr(self.connection, "hname", "") or self.connection.host}')
                logger.debug(f"Set status icon to connected for {self.connection.nickname}")
            else:
                self.status_icon.set_from_icon_name('network-offline-symbolic')
                self.status_icon.set_tooltip_text('Disconnected')
                logger.debug(f"Set status icon to disconnected for {getattr(self.connection, 'nickname', 'connection')}")
                
            # Force a redraw to ensure the icon updates
            self.status_icon.queue_draw()
            
        except Exception as e:
            logger.error(f"Error updating status for {getattr(self.connection, 'nickname', 'connection')}: {e}")
    
    def update_display(self):
        """Update the display with current connection data"""
        # Update the labels with current connection data
        if hasattr(self.connection, 'nickname') and hasattr(self, 'nickname_label'):
            self.nickname_label.set_markup(f"<b>{self.connection.nickname}</b>")
        
        if hasattr(self.connection, 'username') and hasattr(self.connection, 'host') and hasattr(self, 'host_label'):
            port_text = f":{self.connection.port}" if hasattr(self.connection, 'port') and self.connection.port != 22 else ""
            self.host_label.set_text(f"{self.connection.username}@{self.connection.host}{port_text}")
        # Refresh forwarding indicators if rules changed
        self._update_forwarding_indicators()
        
        self.update_status()

    def show_error(self, message):
        """Show error message"""
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading='Error',
            body=message,
        )
        dialog.add_response('ok', 'OK')
        dialog.set_default_response('ok')
        dialog.present()

class WelcomePage(Gtk.Box):
    """Welcome page shown when no tabs are open"""
    
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.set_margin_start(48)
        self.set_margin_end(48)
        self.set_margin_top(48)
        self.set_margin_bottom(48)
        
        # Welcome icon
        try:
            texture = Gdk.Texture.new_from_resource('/io/github/mfat/sshpilot/sshpilot.svg')
            icon = Gtk.Image.new_from_paintable(texture)
            icon.set_pixel_size(128)
        except Exception:
            icon = Gtk.Image.new_from_icon_name('network-workgroup-symbolic')
            icon.set_icon_size(Gtk.IconSize.LARGE)
            icon.set_pixel_size(128)
        self.append(icon)
        
        # Welcome message
        message = Gtk.Label()
        message.set_text('Select a host from the list, double-click or press Enter to connect')
        message.set_halign(Gtk.Align.CENTER)
        message.add_css_class('dim-label')
        self.append(message)
        
        # Shortcuts box
        shortcuts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        shortcuts_box.set_halign(Gtk.Align.CENTER)
        
        shortcuts_title = Gtk.Label()
        shortcuts_title.set_markup('<b>Keyboard Shortcuts</b>')
        shortcuts_box.append(shortcuts_title)
        
        shortcuts = [
            ('Ctrl+N', 'New Connection'),
            ('Ctrl+Alt+N', 'Open New Connection Tab'),
            ('Ctrl+Enter', 'Open New Connection Tab'),
            ('F9', 'Toggle Sidebar'),
            ('Ctrl+L', 'Focus connection list to select server'),
            ('Ctrl+Shift+K', 'New SSH Key'),
            ('Alt+Right', 'Next Tab'),
            ('Alt+Left', 'Previous Tab'),
            ('Ctrl+F4', 'Close Tab'),
            ('Ctrl+,', 'Preferences'),
        ]
        
        for shortcut, description in shortcuts:
            shortcut_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            
            key_label = Gtk.Label()
            key_label.set_markup(f'<tt>{shortcut}</tt>')
            key_label.set_width_chars(15)
            key_label.set_halign(Gtk.Align.START)
            shortcut_box.append(key_label)
            
            desc_label = Gtk.Label()
            desc_label.set_text(description)
            desc_label.set_halign(Gtk.Align.START)
            shortcut_box.append(desc_label)
            
            shortcuts_box.append(shortcut_box)
        
        self.append(shortcuts_box)

class PreferencesWindow(Adw.PreferencesWindow):
    """Preferences dialog window"""
    
    def __init__(self, parent_window, config):
        super().__init__()
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.config = config
        
        # Set window properties
        self.set_title("Preferences")
        self.set_default_size(600, 500)
        
        # Initialize the preferences UI
        self.setup_preferences()

        # Save on close to persist advanced SSH settings
        self.connect('close-request', self.on_close_request)
    
    def setup_preferences(self):
        """Set up preferences UI with current values"""
        try:
            # Create Terminal preferences page
            terminal_page = Adw.PreferencesPage()
            terminal_page.set_title("Terminal")
            terminal_page.set_icon_name("utilities-terminal-symbolic")
            
            # Terminal appearance group
            appearance_group = Adw.PreferencesGroup()
            appearance_group.set_title("Appearance")
            
            # Font selection row
            self.font_row = Adw.ActionRow()
            self.font_row.set_title("Font")
            current_font = self.config.get_setting('terminal-font', 'Monospace 12')
            self.font_row.set_subtitle(current_font)
            
            font_button = Gtk.Button()
            font_button.set_label("Choose")
            font_button.connect('clicked', self.on_font_button_clicked)
            self.font_row.add_suffix(font_button)
            
            appearance_group.add(self.font_row)
            
            # Terminal color scheme
            self.color_scheme_row = Adw.ComboRow()
            self.color_scheme_row.set_title("Color Scheme")
            self.color_scheme_row.set_subtitle("Terminal color theme")
            
            color_schemes = Gtk.StringList()
            color_schemes.append("Default")
            color_schemes.append("Solarized Dark")
            color_schemes.append("Solarized Light")
            color_schemes.append("Monokai")
            color_schemes.append("Dracula")
            color_schemes.append("Nord")
            color_schemes.append("Gruvbox Dark")
            color_schemes.append("One Dark")
            color_schemes.append("Tomorrow Night")
            color_schemes.append("Material Dark")
            self.color_scheme_row.set_model(color_schemes)
            
            # Set current color scheme from config
            current_scheme_key = self.config.get_setting('terminal.theme', 'default')
            
            # Get the display name for the current scheme key
            theme_mapping = self.get_theme_name_mapping()
            reverse_mapping = {v: k for k, v in theme_mapping.items()}
            current_scheme_display = reverse_mapping.get(current_scheme_key, 'Default')
            
            # Find the index of the current scheme in the dropdown
            scheme_names = [
                "Default", "Solarized Dark", "Solarized Light",
                "Monokai", "Dracula", "Nord",
                "Gruvbox Dark", "One Dark", "Tomorrow Night", "Material Dark"
            ]
            try:
                current_index = scheme_names.index(current_scheme_display)
                self.color_scheme_row.set_selected(current_index)
            except ValueError:
                # If the saved scheme isn't found, default to the first option
                self.color_scheme_row.set_selected(0)
                # Also update the config to use the default value
                self.config.set_setting('terminal.theme', 'default')
            
            self.color_scheme_row.connect('notify::selected', self.on_color_scheme_changed)
            
            appearance_group.add(self.color_scheme_row)
            terminal_page.add(appearance_group)
            
            # Create Interface preferences page
            interface_page = Adw.PreferencesPage()
            interface_page.set_title("Interface")
            interface_page.set_icon_name("applications-graphics-symbolic")
            
            # Behavior group
            behavior_group = Adw.PreferencesGroup()
            behavior_group.set_title("Behavior")
            
            # Confirm before disconnecting
            self.confirm_disconnect_switch = Adw.SwitchRow()
            self.confirm_disconnect_switch.set_title("Confirm before disconnecting")
            self.confirm_disconnect_switch.set_subtitle("Show a confirmation dialog when disconnecting from a host")
            self.confirm_disconnect_switch.set_active(
                self.config.get_setting('confirm-disconnect', True)
            )
            self.confirm_disconnect_switch.connect('notify::active', self.on_confirm_disconnect_changed)
            behavior_group.add(self.confirm_disconnect_switch)
            
            interface_page.add(behavior_group)
            
            # Appearance group
            interface_appearance_group = Adw.PreferencesGroup()
            interface_appearance_group.set_title("Appearance")
            
            # Theme selection
            self.theme_row = Adw.ComboRow()
            self.theme_row.set_title("Application Theme")
            self.theme_row.set_subtitle("Choose light, dark, or follow system theme")
            
            themes = Gtk.StringList()
            themes.append("Follow System")
            themes.append("Light")
            themes.append("Dark")
            self.theme_row.set_model(themes)
            
            # Load saved theme preference
            saved_theme = self.config.get_setting('app-theme', 'default')
            theme_mapping = {'default': 0, 'light': 1, 'dark': 2}
            self.theme_row.set_selected(theme_mapping.get(saved_theme, 0))
            
            self.theme_row.connect('notify::selected', self.on_theme_changed)
            
            interface_appearance_group.add(self.theme_row)
            interface_page.add(interface_appearance_group)
            
            # Window group
            window_group = Adw.PreferencesGroup()
            window_group.set_title("Window")
            
            # Remember window size switch
            remember_size_switch = Adw.SwitchRow()
            remember_size_switch.set_title("Remember Window Size")
            remember_size_switch.set_subtitle("Restore window size on startup")
            remember_size_switch.set_active(True)
            
            # Auto focus terminal switch
            auto_focus_switch = Adw.SwitchRow()
            auto_focus_switch.set_title("Auto Focus Terminal")
            auto_focus_switch.set_subtitle("Focus terminal when connecting")
            auto_focus_switch.set_active(True)
            
            window_group.add(remember_size_switch)
            window_group.add(auto_focus_switch)
            interface_page.add(window_group)

            # Advanced SSH settings
            advanced_page = Adw.PreferencesPage()
            advanced_page.set_title("Advanced")
            advanced_page.set_icon_name("applications-system-symbolic")

            advanced_group = Adw.PreferencesGroup()
            advanced_group.set_title("SSH Settings")
            # Use custom options toggle
            self.apply_advanced_row = Adw.SwitchRow()
            self.apply_advanced_row.set_title("Use custom connection options")
            self.apply_advanced_row.set_subtitle("Enable and edit the options below")
            self.apply_advanced_row.set_active(bool(self.config.get_setting('ssh.apply_advanced', False)))
            advanced_group.add(self.apply_advanced_row)


            # Connect timeout
            self.connect_timeout_row = Adw.SpinRow.new_with_range(1, 120, 1)
            self.connect_timeout_row.set_title("Connect Timeout (s)")
            self.connect_timeout_row.set_value(self.config.get_setting('ssh.connection_timeout', 10))
            advanced_group.add(self.connect_timeout_row)

            # Connection attempts
            self.connection_attempts_row = Adw.SpinRow.new_with_range(1, 10, 1)
            self.connection_attempts_row.set_title("Connection Attempts")
            self.connection_attempts_row.set_value(self.config.get_setting('ssh.connection_attempts', 1))
            advanced_group.add(self.connection_attempts_row)

            # Keepalive interval
            self.keepalive_interval_row = Adw.SpinRow.new_with_range(0, 300, 5)
            self.keepalive_interval_row.set_title("ServerAlive Interval (s)")
            self.keepalive_interval_row.set_value(self.config.get_setting('ssh.keepalive_interval', 30))
            advanced_group.add(self.keepalive_interval_row)

            # Keepalive count max
            self.keepalive_count_row = Adw.SpinRow.new_with_range(1, 10, 1)
            self.keepalive_count_row.set_title("ServerAlive CountMax")
            self.keepalive_count_row.set_value(self.config.get_setting('ssh.keepalive_count_max', 3))
            advanced_group.add(self.keepalive_count_row)

            # Strict host key checking
            self.strict_host_row = Adw.ComboRow()
            self.strict_host_row.set_title("StrictHostKeyChecking")
            strict_model = Gtk.StringList()
            for item in ["accept-new", "yes", "no", "ask"]:
                strict_model.append(item)
            self.strict_host_row.set_model(strict_model)
            # Map current value
            current_strict = str(self.config.get_setting('ssh.strict_host_key_checking', 'accept-new'))
            try:
                idx = ["accept-new", "yes", "no", "ask"].index(current_strict)
            except ValueError:
                idx = 0
            self.strict_host_row.set_selected(idx)
            advanced_group.add(self.strict_host_row)

            # BatchMode (non-interactive)
            self.batch_mode_row = Adw.SwitchRow()
            self.batch_mode_row.set_title("BatchMode (disable prompts)")
            self.batch_mode_row.set_active(bool(self.config.get_setting('ssh.batch_mode', True)))
            advanced_group.add(self.batch_mode_row)

            # Compression
            self.compression_row = Adw.SwitchRow()
            self.compression_row.set_title("Enable Compression (-C)")
            self.compression_row.set_active(bool(self.config.get_setting('ssh.compression', True)))
            advanced_group.add(self.compression_row)

            # SSH verbosity (-v levels)
            self.verbosity_row = Adw.SpinRow.new_with_range(0, 3, 1)
            self.verbosity_row.set_title("SSH Verbosity (-v)")
            self.verbosity_row.set_value(int(self.config.get_setting('ssh.verbosity', 0)))
            advanced_group.add(self.verbosity_row)

            # Debug logging toggle
            self.debug_enabled_row = Adw.SwitchRow()
            self.debug_enabled_row.set_title("Enable SSH Debug Logging")
            self.debug_enabled_row.set_active(bool(self.config.get_setting('ssh.debug_enabled', False)))
            advanced_group.add(self.debug_enabled_row)

            # Reset button
            # Add spacing before reset button
            advanced_group.add(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            
            # Use Adw.ActionRow for proper spacing and layout
            reset_row = Adw.ActionRow()
            reset_row.set_title("Reset Advanced SSH Settings")
            reset_row.set_subtitle("Restore all advanced SSH settings to their default values")
            
            reset_btn = Gtk.Button.new_with_label("Reset")
            reset_btn.add_css_class('destructive-action')
            reset_btn.connect('clicked', self.on_reset_advanced_ssh)
            reset_row.add_suffix(reset_btn)
            
            advanced_group.add(reset_row)

            # Disable/enable advanced controls based on toggle
            def _sync_advanced_sensitivity(row=None, *_):
                enabled = bool(self.apply_advanced_row.get_active())
                for w in [self.connect_timeout_row, self.connection_attempts_row,
                          self.keepalive_interval_row, self.keepalive_count_row,
                          self.strict_host_row, self.batch_mode_row,
                          self.compression_row, self.verbosity_row,
                          self.debug_enabled_row]:
                    try:
                        w.set_sensitive(enabled)
                    except Exception:
                        pass
            _sync_advanced_sensitivity()
            self.apply_advanced_row.connect('notify::active', _sync_advanced_sensitivity)

            advanced_page.add(advanced_group)

            # Add pages to the preferences window
            self.add(terminal_page)
            self.add(interface_page)
            self.add(advanced_page)
            
            logger.info("Preferences window initialized")
        except Exception as e:
            logger.error(f"Failed to setup preferences: {e}")

    def on_close_request(self, *args):
        """Persist settings when the preferences window closes"""
        try:
            self.save_advanced_ssh_settings()
            # Ensure preferences are flushed to disk
            if hasattr(self.config, 'save_json_config'):
                self.config.save_json_config()
        except Exception:
            pass
        return False  # allow close
    
    def on_font_button_clicked(self, button):
        """Handle font button click"""
        logger.info("Font button clicked")
        
        # Create font chooser dialog
        font_dialog = Gtk.FontDialog()
        font_dialog.set_title("Choose Terminal Font (Monospace Recommended)")
        
        # Set current font (get from config or default)
        current_font = self.config.get_setting('terminal-font', 'Monospace 12')
        font_desc = Pango.FontDescription.from_string(current_font)
        
        def on_font_selected(dialog, result):
            try:
                font_desc = dialog.choose_font_finish(result)
                if font_desc:
                    font_string = font_desc.to_string()
                    self.font_row.set_subtitle(font_string)
                    logger.info(f"Font selected: {font_string}")
                    
                    # Save to config
                    self.config.set_setting('terminal-font', font_string)
                    
                    # Apply to all active terminals
                    self.apply_font_to_terminals(font_string)
                    
            except Exception as e:
                logger.warning(f"Font selection cancelled or failed: {e}")
        
        font_dialog.choose_font(self, None, None, on_font_selected)
    
    def apply_font_to_terminals(self, font_string):
        """Apply font to all active terminal widgets"""
        try:
            parent_window = self.get_transient_for()
            if parent_window and hasattr(parent_window, 'connection_to_terminals'):
                font_desc = Pango.FontDescription.from_string(font_string)
                count = 0
                for terms in parent_window.connection_to_terminals.values():
                    for terminal in terms:
                        if hasattr(terminal, 'vte'):
                            terminal.vte.set_font(font_desc)
                            count += 1
                logger.info(f"Applied font {font_string} to {count} terminals")
        except Exception as e:
            logger.error(f"Failed to apply font to terminals: {e}")
    
    def on_theme_changed(self, combo_row, param):
        """Handle theme selection change"""
        selected = combo_row.get_selected()
        theme_names = ["Follow System", "Light", "Dark"]
        selected_theme = theme_names[selected] if selected < len(theme_names) else "Follow System"
        
        logger.info(f"Theme changed to: {selected_theme}")
        
        # Apply theme immediately
        style_manager = Adw.StyleManager.get_default()
        
        if selected == 0:  # Follow System
            style_manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
            self.config.set_setting('app-theme', 'default')
        elif selected == 1:  # Light
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            self.config.set_setting('app-theme', 'light')
        elif selected == 2:  # Dark
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            self.config.set_setting('app-theme', 'dark')

    def save_advanced_ssh_settings(self):
        """Persist advanced SSH settings from the preferences UI"""
        try:
            if hasattr(self, 'apply_advanced_row'):
                self.config.set_setting('ssh.apply_advanced', bool(self.apply_advanced_row.get_active()))
            if hasattr(self, 'connect_timeout_row'):
                self.config.set_setting('ssh.connection_timeout', int(self.connect_timeout_row.get_value()))
            if hasattr(self, 'connection_attempts_row'):
                self.config.set_setting('ssh.connection_attempts', int(self.connection_attempts_row.get_value()))
            if hasattr(self, 'keepalive_interval_row'):
                self.config.set_setting('ssh.keepalive_interval', int(self.keepalive_interval_row.get_value()))
            if hasattr(self, 'keepalive_count_row'):
                self.config.set_setting('ssh.keepalive_count_max', int(self.keepalive_count_row.get_value()))
            if hasattr(self, 'strict_host_row'):
                options = ["accept-new", "yes", "no", "ask"]
                idx = self.strict_host_row.get_selected()
                value = options[idx] if 0 <= idx < len(options) else 'accept-new'
                self.config.set_setting('ssh.strict_host_key_checking', value)
            if hasattr(self, 'batch_mode_row'):
                self.config.set_setting('ssh.batch_mode', bool(self.batch_mode_row.get_active()))
            if hasattr(self, 'compression_row'):
                self.config.set_setting('ssh.compression', bool(self.compression_row.get_active()))
            if hasattr(self, 'verbosity_row'):
                self.config.set_setting('ssh.verbosity', int(self.verbosity_row.get_value()))
            if hasattr(self, 'debug_enabled_row'):
                self.config.set_setting('ssh.debug_enabled', bool(self.debug_enabled_row.get_active()))
        except Exception as e:
            logger.error(f"Failed to save advanced SSH settings: {e}")

    def on_reset_advanced_ssh(self, *args):
        """Reset only advanced SSH keys to defaults and update UI."""
        try:
            defaults = self.config.get_default_config().get('ssh', {})
            # Persist defaults and disable apply
            self.config.set_setting('ssh.apply_advanced', False)
            for key in ['connection_timeout', 'connection_attempts', 'keepalive_interval', 'keepalive_count_max', 'compression', 'auto_add_host_keys', 'verbosity', 'debug_enabled']:
                self.config.set_setting(f'ssh.{key}', defaults.get(key))
            # Update UI
            if hasattr(self, 'apply_advanced_row'):
                self.apply_advanced_row.set_active(False)
            if hasattr(self, 'connect_timeout_row'):
                self.connect_timeout_row.set_value(int(defaults.get('connection_timeout', 30)))
            if hasattr(self, 'connection_attempts_row'):
                self.connection_attempts_row.set_value(int(defaults.get('connection_attempts', 1)))
            if hasattr(self, 'keepalive_interval_row'):
                self.keepalive_interval_row.set_value(int(defaults.get('keepalive_interval', 60)))
            if hasattr(self, 'keepalive_count_row'):
                self.keepalive_count_row.set_value(int(defaults.get('keepalive_count_max', 3)))
            if hasattr(self, 'strict_host_row'):
                try:
                    self.strict_host_row.set_selected(["accept-new", "yes", "no", "ask"].index('accept-new'))
                except ValueError:
                    self.strict_host_row.set_selected(0)
            if hasattr(self, 'batch_mode_row'):
                self.batch_mode_row.set_active(False)
            if hasattr(self, 'compression_row'):
                self.compression_row.set_active(bool(defaults.get('compression', True)))
            if hasattr(self, 'verbosity_row'):
                self.verbosity_row.set_value(int(defaults.get('verbosity', 0)))
            if hasattr(self, 'debug_enabled_row'):
                self.debug_enabled_row.set_active(bool(defaults.get('debug_enabled', False)))
        except Exception as e:
            logger.error(f"Failed to reset advanced SSH settings: {e}")
    
    def get_theme_name_mapping(self):
        """Get mapping between display names and config keys"""
        return {
            "Default": "default",
            "Solarized Dark": "solarized_dark", 
            "Solarized Light": "solarized_light",
            "Monokai": "monokai",
            "Dracula": "dracula",
            "Nord": "nord",
            "Gruvbox Dark": "gruvbox_dark",
            "One Dark": "one_dark",
            "Tomorrow Night": "tomorrow_night",
            "Material Dark": "material_dark",
        }
    
    def get_reverse_theme_mapping(self):
        """Get mapping from config keys to display names"""
        mapping = self.get_theme_name_mapping()
        return {v: k for k, v in mapping.items()}
    
    def on_color_scheme_changed(self, combo_row, param):
        """Handle terminal color scheme change"""
        selected = combo_row.get_selected()
        scheme_names = [
            "Default", "Solarized Dark", "Solarized Light",
            "Monokai", "Dracula", "Nord",
            "Gruvbox Dark", "One Dark", "Tomorrow Night", "Material Dark"
        ]
        selected_scheme = scheme_names[selected] if selected < len(scheme_names) else "Default"
        
        logger.info(f"Terminal color scheme changed to: {selected_scheme}")
        
        # Convert display name to config key
        theme_mapping = self.get_theme_name_mapping()
        config_key = theme_mapping.get(selected_scheme, "default")
        
        # Save to config using the consistent key
        self.config.set_setting('terminal.theme', config_key)
        
        # Apply to all active terminals
        self.apply_color_scheme_to_terminals(config_key)
        
    def on_confirm_disconnect_changed(self, switch, *args):
        """Handle confirm disconnect setting change"""
        confirm = switch.get_active()
        logger.info(f"Confirm before disconnect setting changed to: {confirm}")
        self.config.set_setting('confirm-disconnect', confirm)
    
    def apply_color_scheme_to_terminals(self, scheme_key):
        """Apply color scheme to all active terminal widgets"""
        try:
            parent_window = self.get_transient_for()
            if parent_window and hasattr(parent_window, 'connection_to_terminals'):
                count = 0
                for terms in parent_window.connection_to_terminals.values():
                    for terminal in terms:
                        if hasattr(terminal, 'apply_theme'):
                            terminal.apply_theme(scheme_key)
                            count += 1
                logger.info(f"Applied color scheme {scheme_key} to {count} terminals")
        except Exception as e:
            logger.error(f"Failed to apply color scheme to terminals: {e}")

class MainWindow(Adw.ApplicationWindow):
    """Main application window"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_terminals = {}
        self.connections = []
        self._is_quitting = False  # Flag to prevent multiple quit attempts
        self._is_controlled_reconnect = False  # Flag to track controlled reconnection
        
        # Initialize managers
        self.connection_manager = ConnectionManager()
        self.config = Config()
        self.key_manager = KeyManager()
        
        # UI state
        self.active_terminals: Dict[Connection, TerminalWidget] = {}  # most recent terminal per connection
        self.connection_to_terminals: Dict[Connection, List[TerminalWidget]] = {}
        self.terminal_to_connection: Dict[TerminalWidget, Connection] = {}
        self.connection_rows = {}   # connection -> row_widget
        # Hide hosts toggle state
        try:
            self._hide_hosts = bool(self.config.get_setting('ui.hide_hosts', False))
        except Exception:
            self._hide_hosts = False
        
        # Set up window
        self.setup_window()
        self.setup_ui()
        self.setup_connections()
        self.setup_signals()
        
        # Add action for activating connections
        self.activate_action = Gio.SimpleAction.new('activate-connection', None)
        self.activate_action.connect('activate', self.on_activate_connection)
        self.add_action(self.activate_action)
        # Context menu action to force opening a new connection tab
        self.open_new_connection_action = Gio.SimpleAction.new('open-new-connection', None)
        self.open_new_connection_action.connect('activate', self.on_open_new_connection_action)
        self.add_action(self.open_new_connection_action)
        
        # Global action for opening new connection tab (Ctrl+Alt+N)
        self.open_new_connection_tab_action = Gio.SimpleAction.new('open-new-connection-tab', None)
        self.open_new_connection_tab_action.connect('activate', self.on_open_new_connection_tab_action)
        self.add_action(self.open_new_connection_tab_action)
        
        # Action for managing files on remote server
        self.manage_files_action = Gio.SimpleAction.new('manage-files', None)
        self.manage_files_action.connect('activate', self.on_manage_files_action)
        self.add_action(self.manage_files_action)
        
        # Action for editing connections via context menu
        self.edit_connection_action = Gio.SimpleAction.new('edit-connection', None)
        self.edit_connection_action.connect('activate', self.on_edit_connection_action)
        self.add_action(self.edit_connection_action)
        
        # Action for deleting connections via context menu
        self.delete_connection_action = Gio.SimpleAction.new('delete-connection', None)
        self.delete_connection_action.connect('activate', self.on_delete_connection_action)
        self.add_action(self.delete_connection_action)
        
        # Action for opening connections in system terminal (only when not in Flatpak)
        if not is_running_in_flatpak():
            self.open_in_system_terminal_action = Gio.SimpleAction.new('open-in-system-terminal', None)
            self.open_in_system_terminal_action.connect('activate', self.on_open_in_system_terminal_action)
            self.add_action(self.open_in_system_terminal_action)
        # (Toasts disabled) Remove any toast-related actions if previously defined
        try:
            if hasattr(self, '_toast_reconnect_action'):
                self.remove_action('toast-reconnect')
        except Exception:
            pass
        
        # Connect to close request signal
        self.connect('close-request', self.on_close_request)
        
        # Start with welcome view (tab view setup already shows welcome initially)
        
        logger.info("Main window initialized")

        # Install sidebar CSS
        try:
            self._install_sidebar_css()
        except Exception as e:
            logger.error(f"Failed to install sidebar CSS: {e}")

        # On startup, focus the first item in the connection list (not the toolbar buttons)
        try:
            GLib.idle_add(self._focus_connection_list_first_row)
        except Exception:
            pass

    def _install_sidebar_css(self):
        """Install sidebar focus CSS"""
        try:
            # Install CSS for sidebar focus highlighting once per display
            display = Gdk.Display.get_default()
            if not display:
                logger.warning("No display available for CSS installation")
                return
            # Use an attribute on the display to avoid re-adding provider
            if getattr(display, '_sidebar_css_installed', False):
                return
            provider = Gtk.CssProvider()
            css = """
            /* Pulse highlight for selected rows */
            .pulse-highlight {
              background: alpha(@accent_bg_color, 0.5);
              border-radius: 8px;
              box-shadow: 0 0 0 0.5px alpha(@accent_bg_color, 0.28) inset;
              opacity: 0;
              transition: opacity 0.3s ease-in-out;
            }
            .pulse-highlight.on {
              opacity: 1;
            }

            /* optional: a subtle focus ring while the list is focused */
            row:selected:focus-within {
            #   box-shadow: 0 0 8px 2px @accent_bg_color inset;
            #border: 2px solid @accent_bg_color;  /* Adds a solid border of 2px thickness */
              border-radius: 8px;
            }
            """
            provider.load_from_data(css.encode('utf-8'))
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            setattr(display, '_sidebar_css_installed', True)
            logger.debug("Sidebar CSS installed successfully")
        except Exception as e:
            logger.error(f"Failed to install sidebar CSS: {e}")
            import traceback
            logger.debug(f"CSS installation traceback: {traceback.format_exc()}")

    def _toggle_class(self, widget, name, on):
        """Helper to toggle CSS class on a widget"""
        if on: 
            widget.add_css_class(name)
        else:  
            widget.remove_css_class(name)



    def pulse_selected_row(self, list_box: Gtk.ListBox, repeats=3, duration_ms=280):
        """Pulse the selected row with highlight effect"""
        row = list_box.get_selected_row() or (list_box.get_selected_rows()[0] if list_box.get_selected_rows() else None)
        if not row:
            return
        if not hasattr(row, "_pulse"):
            return
        # Ensure it's realized so opacity changes render
        if not row.get_mapped():
            row.realize()
        
        # Use CSS-based pulse for now
        pulse = row._pulse
        cycle_duration = max(300, duration_ms // repeats)  # Minimum 300ms per cycle for faster pulses
        
        def do_cycle(count):
            if count == 0:
                return False
            pulse.add_css_class("on")
            # Keep the pulse visible for a shorter time for snappier effect
            GLib.timeout_add(cycle_duration // 2, lambda: (
                pulse.remove_css_class("on"),
                # Add a shorter delay before the next pulse
                GLib.timeout_add(cycle_duration // 2, lambda: do_cycle(count - 1)) or True
            ) and False)
            return False

        GLib.idle_add(lambda: do_cycle(repeats))

    def _test_css_pulse(self, action, param):
        """Simple test to manually toggle CSS class"""
        row = self.connection_list.get_selected_row()
        if row and hasattr(row, "_pulse"):
            pulse = row._pulse
            pulse.add_css_class("on")
            GLib.timeout_add(1000, lambda: (
                pulse.remove_css_class("on")
            ) or False)

    def _setup_interaction_stop_pulse(self):
        """Set up event controllers to stop pulse effect on user interaction"""
        # Mouse click controller
        click_ctl = Gtk.GestureClick()
        click_ctl.connect("pressed", self._stop_pulse_on_interaction)
        self.connection_list.add_controller(click_ctl)
        
        # Key controller
        key_ctl = Gtk.EventControllerKey()
        key_ctl.connect("key-pressed", self._on_connection_list_key_pressed)
        self.connection_list.add_controller(key_ctl)
        
        # Scroll controller
        scroll_ctl = Gtk.EventControllerScroll()
        scroll_ctl.connect("scroll", self._stop_pulse_on_interaction)
        self.connection_list.add_controller(scroll_ctl)

    def _on_connection_list_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in the connection list"""
        # Stop pulse effect on any key press
        self._stop_pulse_on_interaction(controller)
        
        # Handle Enter key specifically
        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            selected_row = self.connection_list.get_selected_row()
            if selected_row and hasattr(selected_row, 'connection'):
                connection = selected_row.connection
                self._focus_most_recent_tab_or_open_new(connection)
            return True  # Consume the event to prevent row-activated
        return False

    def _stop_pulse_on_interaction(self, controller, *args):
        """Stop any ongoing pulse effect when user interacts"""
        # Stop pulse on any row that has the 'on' class
        for row in self.connection_list:
            if hasattr(row, "_pulse"):
                pulse = row._pulse
                if "on" in pulse.get_css_classes():
                    pulse.remove_css_class("on")

    def _wire_pulses(self):
        """Wire pulse effects to trigger on focus-in only"""
        # Track if this is the initial startup focus
        self._is_initial_focus = True
        
        # When list gains keyboard focus (e.g., after Ctrl+L)
        focus_ctl = Gtk.EventControllerFocus()
        def on_focus_enter(*args):
            # Don't pulse on initial startup focus
            if self._is_initial_focus:
                self._is_initial_focus = False
                return
            self.pulse_selected_row(self.connection_list, repeats=1, duration_ms=600)
        focus_ctl.connect("enter", on_focus_enter)
        self.connection_list.add_controller(focus_ctl)
        
        # Stop pulse effect when user interacts with the list
        self._setup_interaction_stop_pulse()
        
        # Add sidebar toggle action and accelerators
        try:
            # Add window-scoped action for sidebar toggle
            sidebar_action = Gio.SimpleAction.new("toggle_sidebar", None)
            sidebar_action.connect("activate", self.on_toggle_sidebar_action)
            self.add_action(sidebar_action)
            

            
            # Bind accelerators (F9 primary, Ctrl+B alternate)
            app = self.get_application()
            if app:
                app.set_accels_for_action("win.toggle_sidebar", ["F9", "<Control>b"])
        except Exception as e:
            logger.warning(f"Failed to add sidebar toggle action: {e}")

    def setup_window(self):
        """Configure main window properties"""
        self.set_title('sshPilot')
        self.set_icon_name('io.github.mfat.sshpilot')
        
        # Load window geometry
        geometry = self.config.get_window_geometry()
        self.set_default_size(geometry['width'], geometry['height'])
        
        # Connect window state signals
        self.connect('notify::default-width', self.on_window_size_changed)
        self.connect('notify::default-height', self.on_window_size_changed)
        # Ensure initial focus after the window is mapped
        try:
            self.connect('map', lambda *a: GLib.timeout_add(50, self._focus_connection_list_first_row))
        except Exception:
            pass

        # Global shortcuts for tab navigation: Alt+Right / Alt+Left
        try:
            nav = Gtk.ShortcutController()
            nav.set_scope(Gtk.ShortcutScope.GLOBAL)
            if hasattr(nav, 'set_propagation_phase'):
                nav.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)

            def _cb_next(widget, *args):
                try:
                    self._select_tab_relative(1)
                except Exception:
                    pass
                return True

            def _cb_prev(widget, *args):
                try:
                    self._select_tab_relative(-1)
                except Exception:
                    pass
                return True

            nav.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Alt>Right'),
                Gtk.CallbackAction.new(_cb_next)
            ))
            nav.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Alt>Left'),
                Gtk.CallbackAction.new(_cb_prev)
            ))
            
            self.add_controller(nav)
        except Exception:
            pass
        
    def on_window_size_changed(self, window, param):
        """Handle window size changes and save the new dimensions"""
        width = self.get_default_width()
        height = self.get_default_height()
        logger.debug(f"Window size changed to: {width}x{height}")
        
        # Save the new window geometry
        self.config.set_window_geometry(width, height)

    def setup_ui(self):
        """Set up the user interface"""
        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Create header bar
        self.header_bar = Adw.HeaderBar()
        self.header_bar.set_title_widget(Gtk.Label(label="sshPilot"))
        
        # Add window controls (minimize, maximize, close)
        self.header_bar.set_show_start_title_buttons(True)
        self.header_bar.set_show_end_title_buttons(True)
        
        # Add sidebar toggle button to the left side of header bar
        self.sidebar_toggle_button = Gtk.ToggleButton()
        self.sidebar_toggle_button.set_can_focus(False)  # Remove focus from sidebar toggle
        
        # Sidebar always starts visible
        sidebar_visible = True
        
        self.sidebar_toggle_button.set_icon_name('sidebar-show-symbolic')
        self.sidebar_toggle_button.set_tooltip_text('Hide Sidebar (F9, Ctrl+B)')
        self.sidebar_toggle_button.set_active(sidebar_visible)
        self.sidebar_toggle_button.connect('toggled', self.on_sidebar_toggle)
        self.header_bar.pack_start(self.sidebar_toggle_button)
        
        # Add header bar to main container
        main_box.append(self.header_bar)
        
        # Create main layout (fallback if OverlaySplitView is unavailable)
        if HAS_OVERLAY_SPLIT:
            self.split_view = Adw.OverlaySplitView()
            try:
                self.split_view.set_sidebar_width_fraction(0.25)
                self.split_view.set_min_sidebar_width(200)
                self.split_view.set_max_sidebar_width(400)
            except Exception:
                pass
            self.split_view.set_vexpand(True)
            self._split_variant = 'overlay'
        else:
            self.split_view = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
            self.split_view.set_wide_handle(True)
            self.split_view.set_vexpand(True)
            self._split_variant = 'paned'
        
        # Sidebar always starts visible
        sidebar_visible = True
        
        # Create sidebar
        self.setup_sidebar()
        
        # Create main content area
        self.setup_content_area()
        
        # Add split view to main container
        main_box.append(self.split_view)
        
        # Sidebar is always visible on startup

        # Create toast overlay and set main content
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(main_box)
        self.set_content(self.toast_overlay)

    def _set_sidebar_widget(self, widget: Gtk.Widget) -> None:
        if HAS_OVERLAY_SPLIT:
            try:
                self.split_view.set_sidebar(widget)
                return
            except Exception:
                pass
        # Fallback for Gtk.Paned
        try:
            self.split_view.set_start_child(widget)
        except Exception:
            pass

    def _set_content_widget(self, widget: Gtk.Widget) -> None:
        if HAS_OVERLAY_SPLIT:
            try:
                self.split_view.set_content(widget)
                return
            except Exception:
                pass
        # Fallback for Gtk.Paned
        try:
            self.split_view.set_end_child(widget)
        except Exception:
            pass

    def _get_sidebar_width(self) -> int:
        try:
            if HAS_OVERLAY_SPLIT and hasattr(self.split_view, 'get_max_sidebar_width'):
                return int(self.split_view.get_max_sidebar_width())
        except Exception:
            pass
        # Fallback: attempt to read allocation of the first child when using Paned
        try:
            sidebar = self.split_view.get_start_child()
            if sidebar is not None:
                alloc = sidebar.get_allocation()
                return int(alloc.width)
        except Exception:
            pass
        return 0

    def setup_sidebar(self):
        """Set up the sidebar with connection list"""
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.add_css_class('sidebar')
        
        # Sidebar header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(12)
        header.set_margin_end(12)
        header.set_margin_top(12)
        header.set_margin_bottom(6)
        
        # Title
        title_label = Gtk.Label()
        title_label.set_markup('<b>Connections</b>')
        title_label.set_halign(Gtk.Align.START)
        title_label.set_hexpand(True)
        header.append(title_label)
        
        # Add connection button
        add_button = Gtk.Button.new_from_icon_name('list-add-symbolic')
        add_button.set_tooltip_text('Add Connection (Ctrl+N)')
        add_button.connect('clicked', self.on_add_connection_clicked)
        try:
            add_button.set_can_focus(False)
        except Exception:
            pass
        header.append(add_button)

        # Menu button
        menu_button = Gtk.MenuButton()
        menu_button.set_can_focus(False)
        menu_button.set_icon_name('open-menu-symbolic')
        menu_button.set_tooltip_text('Menu')
        menu_button.set_menu_model(self.create_menu())
        header.append(menu_button)

        # Hide/Show hostnames button (eye icon)
        def _update_eye_icon(btn):
            try:
                icon = 'view-conceal-symbolic' if self._hide_hosts else 'view-reveal-symbolic'
                btn.set_icon_name(icon)
                btn.set_tooltip_text('Show hostnames' if self._hide_hosts else 'Hide hostnames')
            except Exception:
                pass

        hide_button = Gtk.Button.new_from_icon_name('view-reveal-symbolic')
        _update_eye_icon(hide_button)
        def _on_toggle_hide(btn):
            try:
                self._hide_hosts = not self._hide_hosts
                # Persist setting
                try:
                    self.config.set_setting('ui.hide_hosts', self._hide_hosts)
                except Exception:
                    pass
                # Update all rows
                for row in self.connection_rows.values():
                    if hasattr(row, 'apply_hide_hosts'):
                        row.apply_hide_hosts(self._hide_hosts)
                # Update icon/tooltip
                _update_eye_icon(btn)
            except Exception:
                pass
        hide_button.connect('clicked', _on_toggle_hide)
        try:
            hide_button.set_can_focus(False)
        except Exception:
            pass
        header.append(hide_button)
        
        sidebar_box.append(header)
        
        # Connection list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        
        self.connection_list = Gtk.ListBox()
        self.connection_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        try:
            self.connection_list.set_can_focus(True)
        except Exception:
            pass
        
        # Wire pulse effects
        self._wire_pulses()
        
        # Connect signals
        self.connection_list.connect('row-selected', self.on_connection_selected)  # For button sensitivity
        self.connection_list.connect('row-activated', self.on_connection_activated)  # For Enter key/double-click
        
        # Make sure the connection list is focusable and can receive key events
        self.connection_list.set_focusable(True)
        self.connection_list.set_can_focus(True)
        self.connection_list.set_focus_on_click(True)
        self.connection_list.set_activate_on_single_click(False)  # Require double-click to activate
        
        # Set up drag and drop for reordering
        self.setup_connection_list_dnd()

        # Right-click context menu to open multiple connections
        try:
            context_click = Gtk.GestureClick()
            context_click.set_button(0)  # handle any button; filter inside
            def _on_list_pressed(gesture, n_press, x, y):
                try:
                    btn = 0
                    try:
                        btn = gesture.get_current_button()
                    except Exception:
                        pass
                    if btn not in (Gdk.BUTTON_SECONDARY, 3):
                        return
                    row = self.connection_list.get_row_at_y(int(y))
                    if not row:
                        return
                    self.connection_list.select_row(row)
                    self._context_menu_connection = getattr(row, 'connection', None)
                    menu = Gio.Menu()
                    
                    # Add menu items
                    menu.append(_('+ Open New Connection'), 'win.open-new-connection')
                    menu.append(_('✏ Edit Connection'), 'win.edit-connection')
                    menu.append(_('🗄 Manage Files'), 'win.manage-files')
                    # Only show system terminal option when not in Flatpak
                    if not is_running_in_flatpak():
                        menu.append(_('💻 Open in System Terminal'), 'win.open-in-system-terminal')
                    menu.append(_('🗑 Delete Connection'), 'win.delete-connection')
                    pop = Gtk.PopoverMenu.new_from_model(menu)
                    pop.set_parent(self.connection_list)
                    try:
                        rect = Gdk.Rectangle()
                        rect.x = int(x)
                        rect.y = int(y)
                        rect.width = 1
                        rect.height = 1
                        pop.set_pointing_to(rect)
                    except Exception:
                        pass
                    pop.popup()
                except Exception:
                    pass
            context_click.connect('pressed', _on_list_pressed)
            self.connection_list.add_controller(context_click)
        except Exception:
            pass
        
        # Add keyboard controller for Ctrl+Enter to open new connection
        try:
            key_controller = Gtk.ShortcutController()
            key_controller.set_scope(Gtk.ShortcutScope.LOCAL)
            
            def _on_ctrl_enter(widget, *args):
                try:
                    selected_row = self.connection_list.get_selected_row()
                    if selected_row and hasattr(selected_row, 'connection'):
                        connection = selected_row.connection
                        self.connect_to_host(connection, force_new=True)
                except Exception as e:
                    logger.error(f"Failed to open new connection with Ctrl+Enter: {e}")
                return True
            
            key_controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Control>Return'),
                Gtk.CallbackAction.new(_on_ctrl_enter)
            ))
            
            self.connection_list.add_controller(key_controller)
        except Exception as e:
            logger.debug(f"Failed to add Ctrl+Enter shortcut: {e}")
        
        scrolled.set_child(self.connection_list)
        sidebar_box.append(scrolled)
        
        # Sidebar toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_start(6)
        toolbar.set_margin_end(6)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)
        toolbar.add_css_class('toolbar')
        try:
            # Expose the computed visual height so terminal banners can match
            min_h, nat_h, min_baseline, nat_baseline = toolbar.measure(Gtk.Orientation.VERTICAL, -1)
            self._toolbar_row_height = max(min_h, nat_h)
            # Also track the real allocated height dynamically
            def _on_toolbar_alloc(widget, allocation):
                try:
                    self._toolbar_row_height = allocation.height
                except Exception:
                    pass
            toolbar.connect('size-allocate', _on_toolbar_alloc)
        except Exception:
            self._toolbar_row_height = 36
        
        # Edit button
        self.edit_button = Gtk.Button.new_from_icon_name('document-edit-symbolic')
        self.edit_button.set_tooltip_text('Edit Connection')
        self.edit_button.set_sensitive(False)
        self.edit_button.connect('clicked', self.on_edit_connection_clicked)
        toolbar.append(self.edit_button)

        # Copy key to server button (ssh-copy-id)
        self.copy_key_button = Gtk.Button.new_from_icon_name('dialog-password-symbolic')
        self.copy_key_button.set_tooltip_text('Copy public key to server for passwordless login')
        self.copy_key_button.set_sensitive(False)
        self.copy_key_button.connect('clicked', self.on_copy_key_to_server_clicked)
        toolbar.append(self.copy_key_button)

        # Upload (scp) button
        self.upload_button = Gtk.Button.new_from_icon_name('document-send-symbolic')
        self.upload_button.set_tooltip_text('Upload file(s) to server (scp)')
        self.upload_button.set_sensitive(False)
        self.upload_button.connect('clicked', self.on_upload_file_clicked)
        toolbar.append(self.upload_button)

        # Manage files button
        self.manage_files_button = Gtk.Button.new_from_icon_name('folder-symbolic')
        self.manage_files_button.set_tooltip_text('Open file manager for remote server')
        self.manage_files_button.set_sensitive(False)
        self.manage_files_button.connect('clicked', self.on_manage_files_button_clicked)
        toolbar.append(self.manage_files_button)
        
        # System terminal button (only when not in Flatpak)
        if not is_running_in_flatpak():
            self.system_terminal_button = Gtk.Button.new_from_icon_name('utilities-terminal-symbolic')
            self.system_terminal_button.set_tooltip_text('Open connection in system terminal')
            self.system_terminal_button.set_sensitive(False)
            self.system_terminal_button.connect('clicked', self.on_system_terminal_button_clicked)
            toolbar.append(self.system_terminal_button)
        
        # Delete button
        self.delete_button = Gtk.Button.new_from_icon_name('user-trash-symbolic')
        self.delete_button.set_tooltip_text('Delete Connection')
        self.delete_button.set_sensitive(False)
        self.delete_button.connect('clicked', self.on_delete_connection_clicked)
        toolbar.append(self.delete_button)
        
        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)
        
        sidebar_box.append(toolbar)
        
        self._set_sidebar_widget(sidebar_box)

    def setup_content_area(self):
        """Set up the main content area with stack for tabs and welcome view"""
        # Create stack to switch between welcome view and tab view
        self.content_stack = Gtk.Stack()
        self.content_stack.set_hexpand(True)
        self.content_stack.set_vexpand(True)
        
        # Create welcome/help view
        self.welcome_view = WelcomePage()
        self.content_stack.add_named(self.welcome_view, "welcome")
        
        # Create tab view
        self.tab_view = Adw.TabView()
        self.tab_view.set_hexpand(True)
        self.tab_view.set_vexpand(True)
        
        # Connect tab signals
        self.tab_view.connect('close-page', self.on_tab_close)
        self.tab_view.connect('page-attached', self.on_tab_attached)
        self.tab_view.connect('page-detached', self.on_tab_detached)

        # Whenever the window layout changes, propagate toolbar height to
        # any TerminalWidget so the reconnect banner exactly matches.
        try:
            # Capture the toolbar variable from this scope for measurement
            local_toolbar = locals().get('toolbar', None)
            def _sync_banner_heights(*args):
                try:
                    # Re-measure toolbar height in case style/theme changed
                    try:
                        if local_toolbar is not None:
                            min_h, nat_h, min_baseline, nat_baseline = local_toolbar.measure(Gtk.Orientation.VERTICAL, -1)
                            self._toolbar_row_height = max(min_h, nat_h)
                    except Exception:
                        pass
                    # Push exact allocated height to all terminal widgets (+5px)
                    for terms in self.connection_to_terminals.values():
                        for term in terms:
                            if hasattr(term, 'set_banner_height'):
                                term.set_banner_height(getattr(self, '_toolbar_row_height', 37) + 55)
                except Exception:
                    pass
            # Call once after UI is built and again after a short delay
            def _push_now():
                try:
                    height = getattr(self, '_toolbar_row_height', 36)
                    for terms in self.connection_to_terminals.values():
                        for term in terms:
                            if hasattr(term, 'set_banner_height'):
                                term.set_banner_height(height + 55)
                except Exception:
                    pass
                return False
            GLib.idle_add(_sync_banner_heights)
            GLib.timeout_add(200, _sync_banner_heights)
            GLib.idle_add(_push_now)
        except Exception:
            pass
        
        # Create tab bar
        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.tab_view)
        self.tab_bar.set_autohide(False)
        
        # Create tab content box
        tab_content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tab_content_box.append(self.tab_bar)
        tab_content_box.append(self.tab_view)
        # Ensure background matches terminal theme to avoid white flash
        if hasattr(tab_content_box, 'add_css_class'):
            tab_content_box.add_css_class('terminal-bg')
        
        self.content_stack.add_named(tab_content_box, "tabs")
        # Also color the stack background
        if hasattr(self.content_stack, 'add_css_class'):
            self.content_stack.add_css_class('terminal-bg')
        
        # Start with welcome view visible
        self.content_stack.set_visible_child_name("welcome")
        
        self._set_content_widget(self.content_stack)

    def setup_connection_list_dnd(self):
        """Set up drag and drop for connection list reordering"""
        # TODO: Implement drag and drop reordering
        pass

    def create_menu(self):
        """Create application menu"""
        menu = Gio.Menu()
        
        # Add all menu items directly to the main menu
        menu.append('New Connection', 'app.new-connection')
        menu.append('Generate SSH Key', 'app.new-key')
        menu.append('Preferences', 'app.preferences')
        menu.append('Help', 'app.help')
        menu.append('About', 'app.about')
        menu.append('Quit', 'app.quit')
        
        return menu

    def setup_connections(self):
        """Load and display existing connections"""
        connections = self.connection_manager.get_connections()
        
        for connection in connections:
            self.add_connection_row(connection)
        
        # Select first connection if available
        if connections:
            first_row = self.connection_list.get_row_at_index(0)
            if first_row:
                self.connection_list.select_row(first_row)
                # Defer focus to the list to ensure keyboard navigation works immediately
                GLib.idle_add(self._focus_connection_list_first_row)

    def setup_signals(self):
        """Connect to manager signals"""
        # Connection manager signals - use connect_after to avoid conflict with GObject.connect
        self.connection_manager.connect_after('connection-added', self.on_connection_added)
        self.connection_manager.connect_after('connection-removed', self.on_connection_removed)
        self.connection_manager.connect_after('connection-status-changed', self.on_connection_status_changed)
        
        # Config signals
        self.config.connect('setting-changed', self.on_setting_changed)

    def add_connection_row(self, connection: Connection):
        """Add a connection row to the list"""
        row = ConnectionRow(connection)
        self.connection_list.append(row)
        self.connection_rows[connection] = row
        # Apply current hide-hosts setting to new row
        if hasattr(row, 'apply_hide_hosts'):
            row.apply_hide_hosts(getattr(self, '_hide_hosts', False))

    def show_welcome_view(self):
        """Show the welcome/help view when no connections are active"""
        # Remove terminal background styling so welcome uses app theme colors
        if hasattr(self.content_stack, 'remove_css_class'):
            try:
                self.content_stack.remove_css_class('terminal-bg')
            except Exception:
                pass
        # Ensure welcome fills the pane
        if hasattr(self, 'welcome_view'):
            try:
                self.welcome_view.set_hexpand(True)
                self.welcome_view.set_vexpand(True)
            except Exception:
                pass
        self.content_stack.set_visible_child_name("welcome")
        logger.info("Showing welcome view")

    def _focus_connection_list_first_row(self):
        """Focus the connection list and ensure the first row is selected."""
        try:
            if not hasattr(self, 'connection_list') or self.connection_list is None:
                return False
            # If the list has no selection, select the first row
            selected = self.connection_list.get_selected_row() if hasattr(self.connection_list, 'get_selected_row') else None
            first_row = self.connection_list.get_row_at_index(0)
            if not selected and first_row:
                self.connection_list.select_row(first_row)
            # If no widget currently has focus in the window, give it to the list
            focus_widget = self.get_focus() if hasattr(self, 'get_focus') else None
            if focus_widget is None and first_row:
                self.connection_list.grab_focus()
        except Exception:
            pass
        return False

    def focus_connection_list(self):
        """Focus the connection list and show a toast notification."""
        try:
            if hasattr(self, 'connection_list') and self.connection_list:
                # If sidebar is hidden, show it first
                if hasattr(self, 'sidebar_toggle_button') and self.sidebar_toggle_button:
                    if not self.sidebar_toggle_button.get_active():
                        self.sidebar_toggle_button.set_active(True)
                
                self.connection_list.grab_focus()
                
                # Pulse the selected row
                self.pulse_selected_row(self.connection_list, repeats=1, duration_ms=600)
                
                # Show toast notification
                toast = Adw.Toast.new(
                    "Switched to connection list — ↑/↓ navigate, Enter open, Ctrl+Enter new tab"
                )
                toast.set_timeout(3)  # seconds
                self.toast_overlay.add_toast(toast)
        except Exception as e:
            logger.error(f"Error focusing connection list: {e}")
    
    def show_tab_view(self):
        """Show the tab view when connections are active"""
        # Re-apply terminal background when switching back to tabs
        if hasattr(self.content_stack, 'add_css_class'):
            try:
                self.content_stack.add_css_class('terminal-bg')
            except Exception:
                pass
        self.content_stack.set_visible_child_name("tabs")
        logger.info("Showing tab view")

    def show_connection_dialog(self, connection: Connection = None):
        """Show connection dialog for adding/editing connections"""
        logger.info(f"Show connection dialog for: {connection}")
        
        # Create connection dialog
        dialog = ConnectionDialog(self, connection, self.connection_manager)
        dialog.connect('connection-saved', self.on_connection_saved)
        dialog.present()

    # --- Helpers (use your existing ones if already present) ---------------------

    def _error_dialog(self, heading: str, body: str, detail: str = ""):
        try:
            msg = Adw.MessageDialog(transient_for=self, modal=True,
                                    heading=heading, body=(body + (f"\n\n{detail}" if detail else "")))
            msg.add_response("ok", _("OK"))
            msg.set_default_response("ok")
            msg.set_close_response("ok")
            msg.present()
        except Exception:
            pass

    def _info_dialog(self, heading: str, body: str):
        try:
            msg = Adw.MessageDialog(transient_for=self, modal=True,
                                    heading=heading, body=body)
            msg.add_response("ok", _("OK"))
            msg.set_default_response("ok")
            msg.set_close_response("ok")
            msg.present()
        except Exception:
            pass


    # --- Single, simplified key generator (no copy-to-server inside) ------------

    def show_key_dialog(self, on_success=None):
        """
        Single key generation dialog (Adw). Optional passphrase.
        No copy-to-server in this dialog. If provided, `on_success(key)` is called.
        """
        try:
            dlg = Adw.Dialog.new()
            dlg.set_title(_("Generate SSH Key"))

            tv = Adw.ToolbarView()
            hb = Adw.HeaderBar()
            hb.set_title_widget(Gtk.Label(label=_("New SSH Key")))
            tv.add_top_bar(hb)

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content.set_margin_top(18); content.set_margin_bottom(18)
            content.set_margin_start(18); content.set_margin_end(18)
            content.set_size_request(500, -1)

            form = Adw.PreferencesGroup()

            name_row = Adw.EntryRow()
            name_row.set_title(_("Key file name"))
            name_row.set_text("id_ed25519")
            
            # Add real-time validation
            def on_name_changed(entry):
                key_name = (entry.get_text() or "").strip()
                if key_name and not key_name.startswith(".") and "/" not in key_name:
                    key_path = self.key_manager.ssh_dir / key_name
                    if key_path.exists():
                        entry.add_css_class("error")
                        entry.set_title(_("Key file name (already exists)"))
                    else:
                        entry.remove_css_class("error")
                        entry.set_title(_("Key file name"))
                else:
                    entry.remove_css_class("error")
                    entry.set_title(_("Key file name"))
            
            name_row.connect("changed", on_name_changed)
            form.add(name_row)

            type_row = Adw.ComboRow()
            type_row.set_title(_("Key type"))
            types = Gtk.StringList.new(["ed25519", "rsa"])
            type_row.set_model(types)
            type_row.set_selected(0)
            form.add(type_row)

            pass_switch = Adw.SwitchRow()
            pass_switch.set_title(_("Encrypt with passphrase"))
            pass_switch.set_active(False)
            form.add(pass_switch)

            pass_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            pass1 = Gtk.PasswordEntry()
            pass1.set_property("placeholder-text", _("Passphrase"))
            pass2 = Gtk.PasswordEntry()
            pass2.set_property("placeholder-text", _("Confirm passphrase"))
            pass_box.append(pass1); pass_box.append(pass2)
            pass_box.set_visible(False)



            def on_pass_toggle(*_):
                pass_box.set_visible(pass_switch.get_active())
            pass_switch.connect("notify::active", on_pass_toggle)

            # Buttons
            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            btn_box.set_halign(Gtk.Align.END)
            btn_cancel = Gtk.Button.new_with_label(_("Cancel"))
            btn_primary = Gtk.Button.new_with_label(_("Generate"))
            try:
                btn_primary.add_css_class("suggested-action")
            except Exception:
                pass
            btn_box.append(btn_cancel); btn_box.append(btn_primary)

            # Compose
            content.append(form)
            content.append(pass_box)
            content.append(btn_box)
            tv.set_content(content)
            dlg.set_content(tv)

            def close_dialog(*args):
                try:
                    dlg.force_close()
                except Exception:
                    pass

            btn_cancel.connect("clicked", close_dialog)

            def do_generate(*args):
                try:
                    key_name = (name_row.get_text() or "").strip()
                    if not key_name:
                        raise ValueError(_("Enter a key file name (e.g. id_ed25519)"))
                    if "/" in key_name or key_name.startswith("."):
                        raise ValueError(_("Key file name must not contain '/' or start with '.'"))

                    # Check if key already exists before attempting generation
                    key_path = self.key_manager.ssh_dir / key_name
                    if key_path.exists():
                        # Suggest alternative names
                        base_name = key_name
                        counter = 1
                        while (self.key_manager.ssh_dir / f"{base_name}_{counter}").exists():
                            counter += 1
                        suggestion = f"{base_name}_{counter}"
                        
                        raise ValueError(_("A key named '{}' already exists. Try '{}' instead.").format(key_name, suggestion))

                    kt = "ed25519" if type_row.get_selected() == 0 else "rsa"

                    passphrase = None
                    if pass_switch.get_active():
                        p1 = pass1.get_text() or ""
                        p2 = pass2.get_text() or ""
                        if p1 != p2:
                            raise ValueError(_("Passphrases do not match"))
                        passphrase = p1

                    key = self.key_manager.generate_key(
                        key_name=key_name,
                        key_type=kt,
                        key_size=3072 if kt == "rsa" else 0,
                        comment=None,
                        passphrase=passphrase,
                    )
                    if not key:
                        raise RuntimeError(_("Key generation failed. See logs for details."))

                    self._info_dialog(_("Key Created"),
                                     _("Private: {priv}\nPublic: {pub}").format(
                                         priv=key.private_path, pub=key.public_path))

                    try:
                        dlg.force_close()
                    except Exception:
                        pass

                    if callable(on_success):
                        on_success(key)

                except Exception as e:
                    self._error_dialog(_("Key Generation Error"),
                                      _("Could not create the SSH key."), str(e))

            btn_primary.connect("clicked", do_generate)
            dlg.present()
            return dlg
        except Exception as e:
            logger.error("Failed to present key generator: %s", e)


    # --- Integrate generator into ssh-copy-id chooser ---------------------------

    def on_copy_key_to_server_clicked(self, _button):
        logger.info("Main window: ssh-copy-id button clicked")
        logger.debug("Main window: Starting ssh-copy-id process")
        
        selected_row = self.connection_list.get_selected_row()
        if not selected_row or not getattr(selected_row, "connection", None):
            logger.warning("Main window: No connection selected for ssh-copy-id")
            return
        connection = selected_row.connection
        logger.info(f"Main window: Selected connection: {getattr(connection, 'nickname', 'unknown')}")
        logger.debug(f"Main window: Connection details - host: {getattr(connection, 'host', 'unknown')}, "
                    f"username: {getattr(connection, 'username', 'unknown')}, "
                    f"port: {getattr(connection, 'port', 22)}")

        try:
            logger.info("Main window: Creating SshCopyIdWindow")
            logger.debug("Main window: Initializing SshCopyIdWindow with key_manager and connection_manager")
            win = SshCopyIdWindow(self, connection, self.key_manager, self.connection_manager)
            logger.info("Main window: SshCopyIdWindow created successfully, presenting")
            win.present()
        except Exception as e:
            logger.error(f"Main window: ssh-copy-id window failed: {e}")
            logger.debug(f"Main window: Exception details: {type(e).__name__}: {str(e)}")
            # Fallback error if window cannot be created
            try:
                md = Adw.MessageDialog(transient_for=self, modal=True,
                                       heading="Error",
                                       body=f"Could not open the Copy Key window.\n\n{e}")
                md.add_response("ok", "OK")
                md.present()
            except Exception:
                pass

    def show_preferences(self):
        """Show preferences dialog"""
        logger.info("Show preferences dialog")
        try:
            preferences_window = PreferencesWindow(self, self.config)
            preferences_window.present()
        except Exception as e:
            logger.error(f"Failed to show preferences dialog: {e}")

    def show_about_dialog(self):
        """Show about dialog"""
        # Use Gtk.AboutDialog so we can force a logo even without icon theme entries
        about = Gtk.AboutDialog()
        about.set_transient_for(self)
        about.set_modal(True)
        about.set_program_name('sshPilot')
        try:
            from . import __version__ as APP_VERSION
        except Exception:
            APP_VERSION = "0.0.0"
        about.set_version(APP_VERSION)
        about.set_comments('SSH connection manager with integrated terminal')
        about.set_website('https://github.com/mfat/sshpilot')
        # Gtk.AboutDialog in GTK4 has no set_issue_url; include issue link in website label
        about.set_website_label('Project homepage')
        about.set_license_type(Gtk.License.GPL_3_0)
        about.set_authors(['mFat <newmfat@gmail.com>'])
        
        # Attempt to load logo from GResource; fall back to local files
        logo_texture = None
        # 1) From GResource bundle
        for resource_path in (
            '/io/github/mfat/sshpilot/sshpilot.svg',
        ):
            try:
                logo_texture = Gdk.Texture.new_from_resource(resource_path)
                if logo_texture:
                    break
            except Exception:
                logo_texture = None
        # 2) From project-local files
        if logo_texture is None:
            candidate_files = []
            # repo root (user added io.github.mfat.sshpilot.png)
            try:
                path = os.path.abspath(os.path.dirname(__file__))
                repo_root = path
                while True:
                    if os.path.exists(os.path.join(repo_root, '.git')):
                        break
                    parent = os.path.dirname(repo_root)
                    if parent == repo_root:
                        break
                    repo_root = parent
                candidate_files.extend([
                    os.path.join(repo_root, 'io.github.mfat.sshpilot.svg'),
                    os.path.join(repo_root, 'sshpilot.svg'),
                ])
                # package resources folder (when running from source)
                candidate_files.append(os.path.join(os.path.dirname(__file__), 'resources', 'sshpilot.svg'))
            except Exception:
                pass
            for png_path in candidate_files:
                try:
                    if os.path.exists(png_path):
                        logo_texture = Gdk.Texture.new_from_filename(png_path)
                        if logo_texture:
                            break
                except Exception:
                    logo_texture = None
        # Apply if loaded
        if logo_texture is not None:
            try:
                about.set_logo(logo_texture)
            except Exception:
                pass
        
        about.present()

    def open_help_url(self):
        """Open the SSH Pilot wiki in the default browser"""
        try:
            import subprocess
            import webbrowser
            
            # Try to open the URL using the default browser
            url = "https://github.com/mfat/sshpilot/wiki"
            
            # Use webbrowser module which handles platform differences
            webbrowser.open(url)
            
            logger.info(f"Opened help URL: {url}")
        except Exception as e:
            logger.error(f"Failed to open help URL: {e}")
            # Fallback: show an error dialog
            try:
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    modal=True,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Failed to open help",
                    secondary_text=f"Could not open the help URL. Please visit:\n{url}"
                )
                dialog.present()
            except Exception:
                pass

    def toggle_list_focus(self):
        """Toggle focus between connection list and terminal"""
        if self.connection_list.has_focus():
            # Focus current terminal
            current_page = self.tab_view.get_selected_page()
            if current_page:
                child = current_page.get_child()
                if hasattr(child, 'vte'):
                    child.vte.grab_focus()
        else:
            # Focus connection list with toast notification
            self.focus_connection_list()

    def _select_tab_relative(self, delta: int):
        """Select tab relative to current index, wrapping around."""
        try:
            n = self.tab_view.get_n_pages()
            if n <= 0:
                return
            current = self.tab_view.get_selected_page()
            # If no current selection, pick first
            if not current:
                page = self.tab_view.get_nth_page(0)
                if page:
                    self.tab_view.set_selected_page(page)
                return
            # Find current index
            idx = 0
            for i in range(n):
                if self.tab_view.get_nth_page(i) == current:
                    idx = i
                    break
            new_index = (idx + delta) % n
            page = self.tab_view.get_nth_page(new_index)
            if page:
                self.tab_view.set_selected_page(page)
        except Exception:
            pass

    def connect_to_host(self, connection: Connection, force_new: bool = False):
        """Connect to SSH host and create terminal tab.
        If force_new is False and a tab exists for this server, select the most recent tab.
        If force_new is True, always open a new tab.
        """
        if not force_new:
            # If a tab exists for this connection, activate the most recent one
            if connection in self.active_terminals:
                terminal = self.active_terminals[connection]
                page = self.tab_view.get_page(terminal)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    return
                else:
                    # Terminal exists but not in tab view, remove from active terminals
                    logger.warning(f"Terminal for {connection.nickname} not found in tab view, removing from active terminals")
                    del self.active_terminals[connection]
            # Fallback: look up any existing terminals for this connection
            existing_terms = self.connection_to_terminals.get(connection) or []
            for t in reversed(existing_terms):  # most recent last
                page = self.tab_view.get_page(t)
                if page is not None:
                    self.active_terminals[connection] = t
                    self.tab_view.set_selected_page(page)
                    return
        
        # Create new terminal
        terminal = TerminalWidget(connection, self.config, self.connection_manager)
        
        # Connect signals
        terminal.connect('connection-established', self.on_terminal_connected)
        terminal.connect('connection-failed', lambda w, e: logger.error(f"Connection failed: {e}"))
        terminal.connect('connection-lost', self.on_terminal_disconnected)
        terminal.connect('title-changed', self.on_terminal_title_changed)
        
        # Add to tab view
        page = self.tab_view.append(terminal)
        page.set_title(connection.nickname)
        page.set_icon(Gio.ThemedIcon.new('utilities-terminal-symbolic'))
        
        # Store references for multi-tab tracking
        self.connection_to_terminals.setdefault(connection, []).append(terminal)
        self.terminal_to_connection[terminal] = connection
        self.active_terminals[connection] = terminal
        
        # Switch to tab view when first connection is made
        self.show_tab_view()
        
        # Activate the new tab
        self.tab_view.set_selected_page(page)
        
        # Force set colors after the terminal is added to the UI
        def _set_terminal_colors():
            try:
                # Set colors using RGBA
                fg = Gdk.RGBA()
                fg.parse('rgb(0,0,0)')  # Black
                
                bg = Gdk.RGBA()
                bg.parse('rgb(255,255,255)')  # White
                
                # Set colors using both methods for maximum compatibility
                terminal.vte.set_color_foreground(fg)
                terminal.vte.set_color_background(bg)
                terminal.vte.set_colors(fg, bg, None)
                
                # Force a redraw
                terminal.vte.queue_draw()
                
                # Connect to the SSH server after setting colors
                if not terminal._connect_ssh():
                    logger.error("Failed to establish SSH connection")
                    self.tab_view.close_page(page)
                    # Cleanup on failure
                    try:
                        if connection in self.active_terminals and self.active_terminals[connection] is terminal:
                            del self.active_terminals[connection]
                        if terminal in self.terminal_to_connection:
                            del self.terminal_to_connection[terminal]
                        if connection in self.connection_to_terminals and terminal in self.connection_to_terminals[connection]:
                            self.connection_to_terminals[connection].remove(terminal)
                            if not self.connection_to_terminals[connection]:
                                del self.connection_to_terminals[connection]
                    except Exception:
                        pass
                        
            except Exception as e:
                logger.error(f"Error setting terminal colors: {e}")
                # Still try to connect even if color setting fails
                if not terminal._connect_ssh():
                    logger.error("Failed to establish SSH connection")
                    self.tab_view.close_page(page)
                    # Cleanup on failure
                    try:
                        if connection in self.active_terminals and self.active_terminals[connection] is terminal:
                            del self.active_terminals[connection]
                        if terminal in self.terminal_to_connection:
                            del self.terminal_to_connection[terminal]
                        if connection in self.connection_to_terminals and terminal in self.connection_to_terminals[connection]:
                            self.connection_to_terminals[connection].remove(terminal)
                            if not self.connection_to_terminals[connection]:
                                del self.connection_to_terminals[connection]
                    except Exception:
                        pass
        
        # Schedule the color setting to run after the terminal is fully initialized
        GLib.idle_add(_set_terminal_colors)

    def _on_disconnect_confirmed(self, dialog, response_id, connection):
        """Handle response from disconnect confirmation dialog"""
        dialog.destroy()
        if response_id == 'disconnect' and connection in self.active_terminals:
            terminal = self.active_terminals[connection]
            terminal.disconnect()
            # If part of a delete flow, remove the connection now
            if getattr(self, '_pending_delete_connection', None) is connection:
                try:
                    self.connection_manager.remove_connection(connection)
                finally:
                    self._pending_delete_connection = None
    
    def disconnect_from_host(self, connection: Connection):
        """Disconnect from SSH host"""
        if connection not in self.active_terminals:
            return
            
        # Check if confirmation is required
        confirm_disconnect = self.config.get_setting('confirm-disconnect', True)
        
        if confirm_disconnect:
            # Show confirmation dialog
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Disconnect from {}").format(connection.nickname or connection.host),
                body=_("Are you sure you want to disconnect from this host?")
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('disconnect', _("Disconnect"))
            dialog.set_response_appearance('disconnect', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
            
            dialog.connect('response', self._on_disconnect_confirmed, connection)
            dialog.present()
        else:
            # Disconnect immediately without confirmation
            terminal = self.active_terminals[connection]
            terminal.disconnect()

    # Signal handlers
    def on_connection_click(self, gesture, n_press, x, y):
        """Handle clicks on the connection list"""
        # Get the row that was clicked
        row = self.connection_list.get_row_at_y(int(y))
        if row is None:
            return
        
        if n_press == 1:  # Single click - just select
            self.connection_list.select_row(row)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        elif n_press == 2:  # Double click - connect
            self._cycle_connection_tabs_or_open(row.connection)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def on_connection_activated(self, list_box, row):
        """Handle connection activation (Enter key)"""
        if row:
            self._cycle_connection_tabs_or_open(row.connection)
            

        
    def on_connection_activate(self, list_box, row):
        """Handle connection activation (Enter key or double-click)"""
        if row:
            self._cycle_connection_tabs_or_open(row.connection)
            return True  # Stop event propagation
        return False
        
    def on_activate_connection(self, action, param):
        """Handle the activate-connection action"""
        row = self.connection_list.get_selected_row()
        if row:
            self._cycle_connection_tabs_or_open(row.connection)
            
    def on_connection_activated(self, list_box, row):
        """Handle connection activation (double-click)"""
        if row:
            self._cycle_connection_tabs_or_open(row.connection)

    def _focus_most_recent_tab_or_open_new(self, connection: Connection):
        """If there are open tabs for this server, focus the most recent one.
        Otherwise open a new tab for the server.
        """
        try:
            # Check if there are open tabs for this connection
            terms_for_conn = []
            try:
                n = self.tab_view.get_n_pages()
            except Exception:
                n = 0
            for i in range(n):
                page = self.tab_view.get_nth_page(i)
                child = page.get_child() if hasattr(page, 'get_child') else None
                if child is not None and self.terminal_to_connection.get(child) == connection:
                    terms_for_conn.append(child)

            if terms_for_conn:
                # Focus the most recent tab for this connection
                most_recent_term = self.active_terminals.get(connection)
                if most_recent_term and most_recent_term in terms_for_conn:
                    # Use the most recent terminal
                    target_term = most_recent_term
                else:
                    # Fallback to the first tab for this connection
                    target_term = terms_for_conn[0]
                
                page = self.tab_view.get_page(target_term)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    # Update most-recent mapping
                    self.active_terminals[connection] = target_term
                    # Give focus to the VTE terminal so user can start typing immediately
                    target_term.vte.grab_focus()
                    return

            # No existing tabs for this connection -> open a new one
            self.connect_to_host(connection, force_new=False)
        except Exception as e:
            logger.error(f"Failed to focus most recent tab or open new for {getattr(connection, 'nickname', '')}: {e}")

    def _cycle_connection_tabs_or_open(self, connection: Connection):
        """If there are open tabs for this server, cycle to the next one (wrap).
        Otherwise open a new tab for the server.
        """
        try:
            # Collect current pages in visual/tab order
            terms_for_conn = []
            try:
                n = self.tab_view.get_n_pages()
            except Exception:
                n = 0
            for i in range(n):
                page = self.tab_view.get_nth_page(i)
                child = page.get_child() if hasattr(page, 'get_child') else None
                if child is not None and self.terminal_to_connection.get(child) == connection:
                    terms_for_conn.append(child)

            if terms_for_conn:
                # Determine current index among this connection's tabs
                selected = self.tab_view.get_selected_page()
                current_idx = -1
                if selected is not None:
                    current_child = selected.get_child()
                    for i, t in enumerate(terms_for_conn):
                        if t == current_child:
                            current_idx = i
                            break
                # Compute next index (wrap)
                next_idx = (current_idx + 1) % len(terms_for_conn) if current_idx >= 0 else 0
                next_term = terms_for_conn[next_idx]
                page = self.tab_view.get_page(next_term)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    # Update most-recent mapping
                    self.active_terminals[connection] = next_term
                    return

            # No existing tabs for this connection -> open a new one
            self.connect_to_host(connection, force_new=False)
        except Exception as e:
            logger.error(f"Failed to cycle or open for {getattr(connection, 'nickname', '')}: {e}")

    def on_connection_selected(self, list_box, row):
        """Handle connection list selection change"""
        has_selection = row is not None
        self.edit_button.set_sensitive(has_selection)
        if hasattr(self, 'copy_key_button'):
            self.copy_key_button.set_sensitive(has_selection)
        if hasattr(self, 'upload_button'):
            self.upload_button.set_sensitive(has_selection)
        if hasattr(self, 'manage_files_button'):
            self.manage_files_button.set_sensitive(has_selection)
        if hasattr(self, 'system_terminal_button') and self.system_terminal_button:
            self.system_terminal_button.set_sensitive(has_selection)
        self.delete_button.set_sensitive(has_selection)

    def on_add_connection_clicked(self, button):
        """Handle add connection button click"""
        self.show_connection_dialog()

    def on_edit_connection_clicked(self, button):
        """Handle edit connection button click"""
        selected_row = self.connection_list.get_selected_row()
        if selected_row:
            self.show_connection_dialog(selected_row.connection)

    def on_sidebar_toggle(self, button):
        """Handle sidebar toggle button click"""
        try:
            is_visible = button.get_active()
            self._toggle_sidebar_visibility(is_visible)
            
            # Update button icon and tooltip
            if is_visible:
                button.set_icon_name('sidebar-show-symbolic')
                button.set_tooltip_text('Hide Sidebar (F9, Ctrl+B)')
            else:
                button.set_icon_name('sidebar-show-symbolic')
                button.set_tooltip_text('Show Sidebar (F9, Ctrl+B)')
            
            # No need to save state - sidebar always starts visible
                
        except Exception as e:
            logger.error(f"Failed to toggle sidebar: {e}")

    def on_toggle_sidebar_action(self, action, param):
        """Handle sidebar toggle action (for keyboard shortcuts)"""
        try:
            # Get current sidebar visibility
            if HAS_OVERLAY_SPLIT:
                current_visible = self.split_view.get_show_sidebar()
            else:
                sidebar_widget = self.split_view.get_start_child()
                current_visible = sidebar_widget.get_visible() if sidebar_widget else True
            
            # Toggle to opposite state
            new_visible = not current_visible
            
            # Update sidebar visibility
            self._toggle_sidebar_visibility(new_visible)
            
            # Update button state if it exists
            if hasattr(self, 'sidebar_toggle_button'):
                self.sidebar_toggle_button.set_active(new_visible)
            
            # No need to save state - sidebar always starts visible
                
        except Exception as e:
            logger.error(f"Failed to toggle sidebar via action: {e}")

    def _toggle_sidebar_visibility(self, is_visible):
        """Helper method to toggle sidebar visibility"""
        try:
            if HAS_OVERLAY_SPLIT:
                # For Adw.OverlaySplitView
                self.split_view.set_show_sidebar(is_visible)
            else:
                # For Gtk.Paned fallback
                sidebar_widget = self.split_view.get_start_child()
                if sidebar_widget:
                    sidebar_widget.set_visible(is_visible)
        except Exception as e:
            logger.error(f"Failed to toggle sidebar visibility: {e}")



    def on_upload_file_clicked(self, button):
        """Show SCP intro dialog and start upload to selected server."""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return

            intro = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Upload files to server'),
                body=_('We will use scp to upload file(s) to the selected server. You will be prompted to choose files and a destination path on the server.')
            )
            intro.add_response('cancel', _('Cancel'))
            intro.add_response('choose', _('Choose files…'))
            intro.set_default_response('choose')
            intro.set_close_response('cancel')

            def _on_intro(dlg, response):
                dlg.close()
                if response != 'choose':
                    return
                # Choose local files
                file_chooser = Gtk.FileChooserDialog(
                    title=_('Select files to upload'),
                    action=Gtk.FileChooserAction.OPEN,
                )
                file_chooser.set_transient_for(self)
                file_chooser.set_modal(True)
                file_chooser.add_button(_('Cancel'), Gtk.ResponseType.CANCEL)
                file_chooser.add_button(_('Open'), Gtk.ResponseType.ACCEPT)
                file_chooser.set_select_multiple(True)
                file_chooser.connect('response', lambda fc, resp: self._on_files_chosen(fc, resp, connection))
                file_chooser.show()

            intro.connect('response', _on_intro)
            intro.present()
        except Exception as e:
            logger.error(f'Upload dialog failed: {e}')

    def on_manage_files_button_clicked(self, button):
        """Handle manage files button click from toolbar"""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return
            
            # Use the same logic as the context menu action
            try:
                # Define error callback for async operation
                def error_callback(error_msg):
                    logger.error(f"Failed to open file manager for {connection.nickname}: {error_msg}")
                    # Show error dialog to user
                    self._show_manage_files_error(connection.nickname, error_msg or "Failed to open file manager")
                
                success, error_msg = open_remote_in_file_manager(
                    user=connection.username,
                    host=connection.host,
                    port=connection.port if connection.port != 22 else None,
                    error_callback=error_callback,
                    parent_window=self
                )
                if success:
                    logger.info(f"Started file manager process for {connection.nickname}")
                else:
                    logger.error(f"Failed to start file manager process for {connection.nickname}: {error_msg}")
                    # Show error dialog to user
                    self._show_manage_files_error(connection.nickname, error_msg or "Failed to start file manager process")
            except Exception as e:
                logger.error(f"Error opening file manager: {e}")
                # Show error dialog to user
                self._show_manage_files_error(connection.nickname, str(e))
        except Exception as e:
            logger.error(f"Manage files button click failed: {e}")

    def on_system_terminal_button_clicked(self, button):
        """Handle system terminal button click from toolbar"""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return
            
            # Use the same logic as the context menu action
            self.open_in_system_terminal(connection)
        except Exception as e:
            logger.error(f"System terminal button click failed: {e}")

    def _show_ssh_copy_id_terminal_using_main_widget(self, connection, ssh_key):
        """Show a window with header bar and embedded terminal running ssh-copy-id.

        Requirements:
        - Terminal expands horizontally, no borders around it
        - Header bar contains Cancel and Close buttons
        """
        logger.info("Main window: Starting ssh-copy-id terminal window creation")
        logger.debug(f"Main window: Connection details - host: {getattr(connection, 'host', 'unknown')}, "
                    f"username: {getattr(connection, 'username', 'unknown')}, "
                    f"port: {getattr(connection, 'port', 22)}")
        logger.debug(f"Main window: SSH key details - private_path: {getattr(ssh_key, 'private_path', 'unknown')}, "
                    f"public_path: {getattr(ssh_key, 'public_path', 'unknown')}")
        
        try:
            target = f"{connection.username}@{connection.host}" if getattr(connection, 'username', '') else str(connection.host)
            pub_name = os.path.basename(getattr(ssh_key, 'public_path', '') or '')
            body_text = _('This will add your public key to the server\'s ~/.ssh/authorized_keys so future logins can use SSH keys.')
            logger.debug(f"Main window: Target: {target}, public key name: {pub_name}")
            
            dlg = Adw.Window()
            dlg.set_transient_for(self)
            dlg.set_modal(True)
            logger.debug("Main window: Created modal window")
            try:
                dlg.set_title(_('ssh-copy-id'))
            except Exception:
                pass
            try:
                dlg.set_default_size(920, 520)
            except Exception:
                pass

            # Header bar with Cancel
            header = Adw.HeaderBar()
            title_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(label=_('ssh-copy-id'))
            title_label.set_halign(Gtk.Align.START)
            subtitle_label = Gtk.Label(label=_('Copying {key} to {target}').format(key=pub_name or _('selected key'), target=target))
            subtitle_label.set_halign(Gtk.Align.START)
            try:
                title_label.add_css_class('title-2')
                subtitle_label.add_css_class('dim-label')
            except Exception:
                pass
            title_widget.append(title_label)
            title_widget.append(subtitle_label)
            header.set_title_widget(title_widget)

            # Close button is omitted; window has native close (X)

            # Content: TerminalWidget without connecting spinner/banner
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            try:
                content_box.set_margin_top(12)
                content_box.set_margin_bottom(12)
                content_box.set_margin_start(6)
                content_box.set_margin_end(6)
            except Exception:
                pass
            # Optional info text under header bar
            info_lbl = Gtk.Label(label=body_text)
            info_lbl.set_halign(Gtk.Align.START)
            try:
                info_lbl.add_css_class('dim-label')
                info_lbl.set_wrap(True)
            except Exception:
                pass
            content_box.append(info_lbl)

            term_widget = TerminalWidget(connection, self.config, self.connection_manager)
            # Hide connecting overlay and suppress disconnect banner for this non-SSH task
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            term_widget.set_hexpand(True)
            term_widget.set_vexpand(True)
            # No frame: avoid borders around the terminal
            content_box.append(term_widget)

            # Bottom button area with Close button
            button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            button_box.set_halign(Gtk.Align.END)
            button_box.set_margin_top(12)
            
            cancel_btn = Gtk.Button(label=_('Close'))
            try:
                cancel_btn.add_css_class('suggested-action')
            except Exception:
                pass
            button_box.append(cancel_btn)
            
            content_box.append(button_box)

            # Root container combines header and content
            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root_box.append(header)
            root_box.append(content_box)
            dlg.set_content(root_box)

            def _on_cancel(btn):
                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass
                dlg.close()
            cancel_btn.connect('clicked', _on_cancel)
            # No explicit close button; use window close (X)

            # Build ssh-copy-id command with options derived from connection settings
            logger.debug("Main window: Building ssh-copy-id command arguments")
            argv = self._build_ssh_copy_id_argv(connection, ssh_key)
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            logger.info("Starting ssh-copy-id: %s", ' '.join(argv))
            logger.info("Full command line: %s", cmdline)
            logger.debug(f"Main window: Command argv: {argv}")
            logger.debug(f"Main window: Shell-quoted command: {cmdline}")

            # Helper to write colored lines into the terminal
            def _feed_colored_line(text: str, color: str):
                colors = {
                    'red': '\x1b[31m',
                    'green': '\x1b[32m',
                    'yellow': '\x1b[33m',
                    'blue': '\x1b[34m',
                }
                prefix = colors.get(color, '')
                try:
                    term_widget.vte.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                except Exception:
                    pass

            # Initial info line
            _feed_colored_line(_('Running ssh-copy-id…'), 'yellow')

            # Handle password authentication consistently with terminal connections
            logger.debug("Main window: Setting up authentication environment")
            env = os.environ.copy()
            logger.debug(f"Main window: Environment variables count: {len(env)}")
            
            # Determine auth method and check for saved password
            prefer_password = False
            logger.debug("Main window: Determining authentication preferences")
            try:
                cfg = Config()
                meta = cfg.get_connection_meta(connection.nickname) if hasattr(cfg, 'get_connection_meta') else {}
                logger.debug(f"Main window: Connection metadata: {meta}")
                if isinstance(meta, dict) and 'auth_method' in meta:
                    prefer_password = int(meta.get('auth_method', 0) or 0) == 1
                    logger.debug(f"Main window: Auth method from metadata: {meta.get('auth_method')} -> prefer_password={prefer_password}")
            except Exception as e:
                logger.debug(f"Main window: Failed to get auth method from metadata: {e}")
                try:
                    prefer_password = int(getattr(connection, 'auth_method', 0) or 0) == 1
                    logger.debug(f"Main window: Auth method from connection object: {getattr(connection, 'auth_method', 0)} -> prefer_password={prefer_password}")
                except Exception as e2:
                    logger.debug(f"Main window: Failed to get auth method from connection object: {e2}")
                    prefer_password = False
            
            has_saved_password = bool(self.connection_manager.get_password(connection.host, connection.username))
            logger.debug(f"Main window: Has saved password: {has_saved_password}")
            logger.debug(f"Main window: Authentication setup - prefer_password={prefer_password}, has_saved_password={has_saved_password}")
            
            if prefer_password and has_saved_password:
                # Use sshpass for password authentication
                logger.debug("Main window: Using sshpass for password authentication")
                import shutil
                sshpass_path = None
                
                # Check if sshpass is available and executable
                logger.debug("Main window: Checking for sshpass availability")
                if os.path.exists('/app/bin/sshpass') and os.access('/app/bin/sshpass', os.X_OK):
                    sshpass_path = '/app/bin/sshpass'
                    logger.debug("Found sshpass at /app/bin/sshpass")
                elif shutil.which('sshpass'):
                    sshpass_path = shutil.which('sshpass')
                    logger.debug(f"Found sshpass in PATH: {sshpass_path}")
                else:
                    logger.debug("sshpass not found or not executable")
                
                if sshpass_path:
                    # Use the same approach as ssh_password_exec.py for consistency
                    logger.debug("Main window: Setting up sshpass with FIFO")
                    from .ssh_password_exec import _mk_priv_dir, _write_once_fifo
                    import threading
                    
                    # Create private temp directory and FIFO
                    logger.debug("Main window: Creating private temp directory")
                    tmpdir = _mk_priv_dir()
                    fifo = os.path.join(tmpdir, "pw.fifo")
                    logger.debug(f"Main window: FIFO path: {fifo}")
                    os.mkfifo(fifo, 0o600)
                    logger.debug("Main window: FIFO created with permissions 0o600")
                    
                    # Start writer thread that writes the password exactly once
                    saved_password = self.connection_manager.get_password(connection.host, connection.username)
                    logger.debug(f"Main window: Retrieved saved password, length: {len(saved_password) if saved_password else 0}")
                    t = threading.Thread(target=_write_once_fifo, args=(fifo, saved_password), daemon=True)
                    t.start()
                    logger.debug("Main window: Password writer thread started")
                    
                    # Use sshpass with FIFO
                    original_argv = argv.copy()
                    argv = [sshpass_path, "-f", fifo] + argv
                    logger.debug(f"Main window: Modified argv - added sshpass: {argv}")
                    
                    # Important: strip askpass vars so OpenSSH won't try the askpass helper for passwords
                    env.pop("SSH_ASKPASS", None)
                    env.pop("SSH_ASKPASS_REQUIRE", None)
                    logger.debug("Main window: Removed SSH_ASKPASS environment variables")
                    
                    logger.debug("Using sshpass with FIFO for ssh-copy-id password authentication")
                    
                    # Store tmpdir for cleanup (will be cleaned up when process exits)
                    def cleanup_tmpdir():
                        try:
                            import shutil
                            shutil.rmtree(tmpdir, ignore_errors=True)
                        except Exception:
                            pass
                    import atexit
                    atexit.register(cleanup_tmpdir)
                else:
                    # sshpass not available, fallback to askpass
                    logger.debug("Main window: sshpass not available, falling back to askpass")
                    from .askpass_utils import get_ssh_env_with_askpass
                    askpass_env = get_ssh_env_with_askpass("force")
                    logger.debug(f"Main window: Askpass environment variables: {list(askpass_env.keys())}")
                    env.update(askpass_env)
            elif prefer_password and not has_saved_password:
                # Password auth selected but no saved password - let SSH prompt interactively
                # Don't set any askpass environment variables
                logger.debug("Main window: Password auth selected but no saved password - using interactive prompt")
            else:
                # Use askpass for passphrase prompts (key-based auth)
                logger.debug("Main window: Using askpass for key-based authentication")
                from .askpass_utils import get_ssh_env_with_askpass
                askpass_env = get_ssh_env_with_askpass("force")
                logger.debug(f"Main window: Askpass environment variables: {list(askpass_env.keys())}")
                env.update(askpass_env)

            # Ensure /app/bin is first in PATH for Flatpak compatibility
            logger.debug("Main window: Setting up PATH for Flatpak compatibility")
            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                logger.debug(f"Main window: Current PATH: {current_path}")
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"
                    logger.debug(f"Main window: Updated PATH: {env['PATH']}")
                else:
                    logger.debug("Main window: /app/bin already in PATH")
            else:
                logger.debug("Main window: /app/bin does not exist, skipping PATH modification")
            
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            logger.info("Starting ssh-copy-id: %s", ' '.join(argv))
            logger.debug(f"Main window: Final command line: {cmdline}")
            envv = [f"{k}={v}" for k, v in env.items()]
            logger.debug(f"Main window: Environment variables count: {len(envv)}")

            try:
                logger.debug("Main window: Spawning ssh-copy-id process in VTE terminal")
                logger.debug(f"Main window: Working directory: {os.path.expanduser('~') or '/'}")
                logger.debug(f"Main window: Command: ['bash', '-lc', '{cmdline}']")
                
                term_widget.vte.spawn_async(
                    Vte.PtyFlags.DEFAULT,
                    os.path.expanduser('~') or '/',
                    ['bash', '-lc', cmdline],
                    envv,  # <— use merged env
                    GLib.SpawnFlags.DEFAULT,
                    None,
                    None,
                    -1,
                    None,
                    None
                )
                logger.debug("Main window: ssh-copy-id process spawned successfully")

                # Show result modal when the command finishes
                def _on_copyid_exited(vte, status):
                    logger.debug(f"Main window: ssh-copy-id process exited with raw status: {status}")
                    # Normalize exit code
                    exit_code = None
                    try:
                        if os.WIFEXITED(status):
                            exit_code = os.WEXITSTATUS(status)
                            logger.debug(f"Main window: Process exited normally, exit code: {exit_code}")
                        else:
                            exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
                            logger.debug(f"Main window: Process did not exit normally, normalized exit code: {exit_code}")
                    except Exception as e:
                        logger.debug(f"Main window: Error normalizing exit status: {e}")
                        try:
                            exit_code = int(status)
                            logger.debug(f"Main window: Converted status to int: {exit_code}")
                        except Exception as e2:
                            logger.debug(f"Main window: Failed to convert status to int: {e2}")
                            exit_code = status

                    logger.info(f"ssh-copy-id exited with status: {status}, normalized exit_code: {exit_code}")
                    ok = (exit_code == 0)
                    if ok:
                        logger.info("ssh-copy-id completed successfully")
                        logger.debug("Main window: ssh-copy-id succeeded, showing success message")
                        _feed_colored_line(_('Public key was installed successfully.'), 'green')
                    else:
                        logger.error(f"ssh-copy-id failed with exit code: {exit_code}")
                        logger.debug(f"Main window: ssh-copy-id failed with exit code {exit_code}")
                        _feed_colored_line(_('Failed to install the public key.'), 'red')

                    def _present_result_dialog():
                        logger.debug(f"Main window: Presenting result dialog - success: {ok}")
                        msg = Adw.MessageDialog(
                            transient_for=dlg,
                            modal=True,
                            heading=_('Success') if ok else _('Error'),
                            body=(_('Public key copied to {}@{}').format(connection.username, connection.host)
                                  if ok else _('Failed to copy the public key. Check logs for details.')),
                        )
                        msg.add_response('ok', _('OK'))
                        msg.set_default_response('ok')
                        msg.set_close_response('ok')
                        msg.present()
                        logger.debug("Main window: Result dialog presented")
                        return False

                    GLib.idle_add(_present_result_dialog)

                try:
                    term_widget.vte.connect('child-exited', _on_copyid_exited)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f'Failed to spawn ssh-copy-id in TerminalWidget: {e}')
                logger.debug(f'Main window: Exception details: {type(e).__name__}: {str(e)}')
                dlg.close()
                # No fallback method available
                logger.error(f'Terminal ssh-copy-id failed: {e}')
                self._error_dialog(_("SSH Key Copy Error"),
                                  _("Failed to copy SSH key to server."), 
                                  f"Terminal error: {str(e)}\n\nPlease check:\n• Network connectivity\n• SSH server configuration\n• User permissions")
                return

            dlg.present()
            logger.debug("Main window: ssh-copy-id terminal window presented successfully")
        except Exception as e:
            logger.error(f'VTE ssh-copy-id window failed: {e}')
            logger.debug(f'Main window: Exception details: {type(e).__name__}: {str(e)}')
            self._error_dialog(_("SSH Key Copy Error"),
                              _("Failed to create ssh-copy-id terminal window."), 
                              f"Error: {str(e)}\n\nThis could be due to:\n• Missing VTE terminal widget\n• Display/GTK issues\n• System resource limitations")

    def _build_ssh_copy_id_argv(self, connection, ssh_key):
        """Construct argv for ssh-copy-id honoring saved UI auth preferences."""
        logger.info(f"Building ssh-copy-id argv for key: {getattr(ssh_key, 'public_path', 'unknown')}")
        logger.debug(f"Main window: Building ssh-copy-id command arguments")
        logger.debug(f"Main window: Connection object: {type(connection)}")
        logger.debug(f"Main window: SSH key object: {type(ssh_key)}")
        logger.info(f"Key object attributes: private_path={getattr(ssh_key, 'private_path', 'unknown')}, public_path={getattr(ssh_key, 'public_path', 'unknown')}")
        
        # Verify the public key file exists
        logger.debug(f"Main window: Checking if public key file exists: {ssh_key.public_path}")
        if not os.path.exists(ssh_key.public_path):
            logger.error(f"Public key file does not exist: {ssh_key.public_path}")
            logger.debug(f"Main window: Public key file missing: {ssh_key.public_path}")
            raise RuntimeError(f"Public key file not found: {ssh_key.public_path}")
        
        logger.debug(f"Main window: Public key file verified: {ssh_key.public_path}")
        argv = ['ssh-copy-id', '-i', ssh_key.public_path]
        logger.debug(f"Main window: Base command: {argv}")
        try:
            port = getattr(connection, 'port', 22)
            logger.debug(f"Main window: Connection port: {port}")
            if port and port != 22:
                argv += ['-p', str(connection.port)]
                logger.debug(f"Main window: Added port option: -p {connection.port}")
        except Exception as e:
            logger.debug(f"Main window: Error getting port: {e}")
            pass
        # Honor app SSH settings: strict host key checking / auto-add
        logger.debug("Main window: Loading SSH configuration")
        try:
            cfg = Config()
            ssh_cfg = cfg.get_ssh_config() if hasattr(cfg, 'get_ssh_config') else {}
            logger.debug(f"Main window: SSH config: {ssh_cfg}")
            strict_val = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
            auto_add = bool(ssh_cfg.get('auto_add_host_keys', True))
            logger.debug(f"Main window: SSH settings - strict_val='{strict_val}', auto_add={auto_add}")
            if strict_val:
                argv += ['-o', f'StrictHostKeyChecking={strict_val}']
                logger.debug(f"Main window: Added strict host key checking: {strict_val}")
            elif auto_add:
                argv += ['-o', 'StrictHostKeyChecking=accept-new']
                logger.debug("Main window: Added auto-accept new host keys")
        except Exception as e:
            logger.debug(f"Main window: Error loading SSH config: {e}")
            argv += ['-o', 'StrictHostKeyChecking=accept-new']
            logger.debug("Main window: Using default strict host key checking: accept-new")
        # Derive auth prefs from saved config and connection
        logger.debug("Main window: Determining authentication preferences")
        prefer_password = False
        key_mode = 0
        keyfile = getattr(connection, 'keyfile', '') or ''
        logger.debug(f"Main window: Connection keyfile: '{keyfile}'")
        
        try:
            cfg = Config()
            meta = cfg.get_connection_meta(connection.nickname) if hasattr(cfg, 'get_connection_meta') else {}
            logger.debug(f"Main window: Connection metadata: {meta}")
            if isinstance(meta, dict) and 'auth_method' in meta:
                prefer_password = int(meta.get('auth_method', 0) or 0) == 1
                logger.debug(f"Main window: Auth method from metadata: {meta.get('auth_method')} -> prefer_password={prefer_password}")
        except Exception as e:
            logger.debug(f"Main window: Error getting auth method from metadata: {e}")
            try:
                prefer_password = int(getattr(connection, 'auth_method', 0) or 0) == 1
                logger.debug(f"Main window: Auth method from connection object: {getattr(connection, 'auth_method', 0)} -> prefer_password={prefer_password}")
            except Exception as e2:
                logger.debug(f"Main window: Error getting auth method from connection object: {e2}")
                prefer_password = False
        
        try:
            # key_select_mode is saved in ssh config, our connection object should have it post-load
            key_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
            logger.debug(f"Main window: Key select mode: {key_mode}")
        except Exception as e:
            logger.debug(f"Main window: Error getting key select mode: {e}")
            key_mode = 0
        
        # Validate keyfile path
        try:
            keyfile_ok = bool(keyfile) and os.path.isfile(keyfile)
            logger.debug(f"Main window: Keyfile validation - keyfile='{keyfile}', exists={keyfile_ok}")
        except Exception as e:
            logger.debug(f"Main window: Error validating keyfile: {e}")
            keyfile_ok = False

        # Priority: if UI selected a specific key and it exists, use it; otherwise fall back to password prefs/try-all
        logger.debug(f"Main window: Applying authentication options - key_mode={key_mode}, keyfile_ok={keyfile_ok}, prefer_password={prefer_password}")
        
        # For ssh-copy-id, we should NOT add IdentityFile options because:
        # 1. ssh-copy-id should use the same key for authentication that it's copying
        # 2. The -i parameter already specifies which key to copy
        # 3. Adding IdentityFile would cause ssh-copy-id to use a different key for auth
        
        if key_mode == 1 and keyfile_ok:
            # Don't add IdentityFile for ssh-copy-id - it should use the key being copied
            logger.debug(f"Main window: Skipping IdentityFile for ssh-copy-id - using key being copied for authentication")
        else:
            # Only force password when user selected password auth
            if prefer_password:
                argv += ['-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password,keyboard-interactive']
                logger.debug("Main window: Added password authentication options - PubkeyAuthentication=no, PreferredAuthentications=password,keyboard-interactive")
        
        # Target
        target = f"{connection.username}@{connection.host}" if getattr(connection, 'username', '') else str(connection.host)
        argv.append(target)
        logger.debug(f"Main window: Added target: {target}")
        logger.debug(f"Main window: Final argv: {argv}")
        return argv

    def _on_files_chosen(self, chooser, response, connection):
        try:
            if response != Gtk.ResponseType.ACCEPT:
                chooser.destroy()
                return
            files = chooser.get_files()
            chooser.destroy()
            if not files:
                return
            # Ask remote destination path
            prompt = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Remote destination'),
                body=_('Enter a remote directory (e.g., ~/ or /var/tmp). Files will be uploaded using scp.')
            )
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            dest_row = Adw.EntryRow(title=_('Remote directory'))
            dest_row.set_text('~')
            box.append(dest_row)
            prompt.set_extra_child(box)
            prompt.add_response('cancel', _('Cancel'))
            prompt.add_response('upload', _('Upload'))
            prompt.set_default_response('upload')
            prompt.set_close_response('cancel')

            def _go(d, resp):
                d.close()
                if resp != 'upload':
                    return
                remote_dir = dest_row.get_text().strip() or '~'
                self._start_scp_upload(connection, [f.get_path() for f in files], remote_dir)

            prompt.connect('response', _go)
            prompt.present()
        except Exception as e:
            logger.error(f'File selection failed: {e}')

    def _start_scp_upload(self, connection, local_paths, remote_dir):
        """Run scp using the same terminal window layout as ssh-copy-id."""
        try:
            self._show_scp_upload_terminal_window(connection, local_paths, remote_dir)
        except Exception as e:
            logger.error(f'scp upload failed to start: {e}')

    def _show_scp_upload_terminal_window(self, connection, local_paths, remote_dir):
        try:
            target = f"{connection.username}@{connection.host}"
            info_text = _('We will use scp to upload file(s) to the selected server.')

            dlg = Adw.Window()
            dlg.set_transient_for(self)
            dlg.set_modal(True)
            try:
                dlg.set_title(_('Upload files (scp)'))
            except Exception:
                pass
            try:
                dlg.set_default_size(920, 520)
            except Exception:
                pass

            # Header bar with Cancel
            header = Adw.HeaderBar()
            title_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(label=_('Upload files (scp)'))
            title_label.set_halign(Gtk.Align.START)
            subtitle_label = Gtk.Label(label=_('Uploading to {target}:{dir}').format(target=target, dir=remote_dir))
            subtitle_label.set_halign(Gtk.Align.START)
            try:
                title_label.add_css_class('title-2')
                subtitle_label.add_css_class('dim-label')
            except Exception:
                pass
            title_widget.append(title_label)
            title_widget.append(subtitle_label)
            header.set_title_widget(title_widget)

            cancel_btn = Gtk.Button(label=_('Cancel'))
            try:
                cancel_btn.add_css_class('flat')
            except Exception:
                pass
            header.pack_start(cancel_btn)

            # Content area
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            try:
                content_box.set_margin_top(12)
                content_box.set_margin_bottom(12)
                content_box.set_margin_start(6)
                content_box.set_margin_end(6)
            except Exception:
                pass

            info_lbl = Gtk.Label(label=info_text)
            info_lbl.set_halign(Gtk.Align.START)
            try:
                info_lbl.add_css_class('dim-label')
                info_lbl.set_wrap(True)
            except Exception:
                pass
            content_box.append(info_lbl)

            term_widget = TerminalWidget(connection, self.config, self.connection_manager)
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            term_widget.set_hexpand(True)
            term_widget.set_vexpand(True)
            content_box.append(term_widget)

            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root_box.append(header)
            root_box.append(content_box)
            dlg.set_content(root_box)

            def _on_cancel(btn):
                # Clean up askpass helper scripts
                try:
                    if hasattr(self, '_scp_askpass_helpers'):
                        for helper_path in self._scp_askpass_helpers:
                            try:
                                os.unlink(helper_path)
                            except Exception:
                                pass
                        self._scp_askpass_helpers.clear()
                except Exception:
                    pass
                
                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass
                dlg.close()
            cancel_btn.connect('clicked', _on_cancel)

            # Build and run scp command in the terminal
            argv = self._build_scp_argv(connection, local_paths, remote_dir)

            # Handle environment variables for authentication
            env = os.environ.copy()
            
            # Check if we have stored askpass environment from key passphrase handling
            if hasattr(self, '_scp_askpass_env') and self._scp_askpass_env:
                env.update(self._scp_askpass_env)
                logger.debug(f"SCP: Using askpass environment for key passphrase: {list(self._scp_askpass_env.keys())}")
                # Clear the stored environment after use
                self._scp_askpass_env = {}
                
                # For key-based auth, ensure the key is loaded in SSH agent first
                try:
                    keyfile = getattr(connection, 'keyfile', '') or ''
                    if keyfile and os.path.isfile(keyfile):
                        # Prepare key for connection (add to ssh-agent if needed)
                        if hasattr(self, 'connection_manager') and self.connection_manager:
                            key_prepared = self.connection_manager.prepare_key_for_connection(keyfile)
                            if key_prepared:
                                logger.debug(f"SCP: Key prepared for connection: {keyfile}")
                            else:
                                logger.warning(f"SCP: Failed to prepare key for connection: {keyfile}")
                except Exception as e:
                    logger.warning(f"SCP: Error preparing key for connection: {e}")
            else:
                # Handle password authentication - sshpass is already handled in _build_scp_argv
                # No additional environment setup needed here
                logger.debug("SCP: Password authentication handled by sshpass in command line")

            # Ensure /app/bin is first in PATH for Flatpak compatibility
            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"
            
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            envv = [f"{k}={v}" for k, v in env.items()]
            logger.debug(f"SCP: Final environment variables: SSH_ASKPASS={env.get('SSH_ASKPASS', 'NOT_SET')}, SSH_ASKPASS_REQUIRE={env.get('SSH_ASKPASS_REQUIRE', 'NOT_SET')}")
            logger.debug(f"SCP: Command line: {cmdline}")

            # Helper to write colored lines
            def _feed_colored_line(text: str, color: str):
                colors = {
                    'red': '\x1b[31m',
                    'green': '\x1b[32m',
                    'yellow': '\x1b[33m',
                    'blue': '\x1b[34m',
                }
                prefix = colors.get(color, '')
                try:
                    term_widget.vte.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                except Exception:
                    pass

            _feed_colored_line(_('Starting upload…'), 'yellow')

            try:
                term_widget.vte.spawn_async(
                    Vte.PtyFlags.DEFAULT,
                    os.path.expanduser('~') or '/',
                    ['bash', '-lc', cmdline],
                    envv,  # <— use merged env (ASKPASS + DISPLAY + SSHPILOT_* )
                    GLib.SpawnFlags.DEFAULT,
                    None,
                    None,
                    -1,
                    None,
                    None
                )

                def _on_scp_exited(vte, status):
                    # Normalize exit code
                    exit_code = None
                    try:
                        if os.WIFEXITED(status):
                            exit_code = os.WEXITSTATUS(status)
                        else:
                            exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
                    except Exception:
                        try:
                            exit_code = int(status)
                        except Exception:
                            exit_code = status
                    ok = (exit_code == 0)
                    if ok:
                        _feed_colored_line(_('Upload finished successfully.'), 'green')
                    else:
                        _feed_colored_line(_('Upload failed. See output above.'), 'red')

                    def _present_result_dialog():
                        # Clean up askpass helper scripts
                        try:
                            if hasattr(self, '_scp_askpass_helpers'):
                                for helper_path in self._scp_askpass_helpers:
                                    try:
                                        os.unlink(helper_path)
                                    except Exception:
                                        pass
                                self._scp_askpass_helpers.clear()
                        except Exception:
                            pass
                        
                        msg = Adw.MessageDialog(
                            transient_for=dlg,
                            modal=True,
                            heading=_('Upload complete') if ok else _('Upload failed'),
                            body=(_('Files uploaded to {target}:{dir}').format(target=target, dir=remote_dir)
                                  if ok else _('scp exited with an error. Please review the log output.')),
                        )
                        msg.add_response('ok', _('OK'))
                        msg.set_default_response('ok')
                        msg.set_close_response('ok')
                        msg.present()
                        return False

                    GLib.idle_add(_present_result_dialog)

                try:
                    term_widget.vte.connect('child-exited', _on_scp_exited)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f'Failed to spawn scp in TerminalWidget: {e}')
                dlg.close()
                # Fallback could be implemented here if needed
                return

            dlg.present()
        except Exception as e:
            logger.error(f'Failed to open scp terminal window: {e}')

    def _build_scp_argv(self, connection, local_paths, remote_dir):
        argv = ['scp', '-v']
        # Port
        try:
            if getattr(connection, 'port', 22) and connection.port != 22:
                argv += ['-P', str(connection.port)]
        except Exception:
            pass
        # Auth/SSH options similar to ssh-copy-id
        try:
            cfg = Config()
            ssh_cfg = cfg.get_ssh_config() if hasattr(cfg, 'get_ssh_config') else {}
            strict_val = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
            auto_add = bool(ssh_cfg.get('auto_add_host_keys', True))
            if strict_val:
                argv += ['-o', f'StrictHostKeyChecking={strict_val}']
            elif auto_add:
                argv += ['-o', 'StrictHostKeyChecking=accept-new']
        except Exception:
            argv += ['-o', 'StrictHostKeyChecking=accept-new']
        # Prefer password if selected
        prefer_password = False
        key_mode = 0
        keyfile = getattr(connection, 'keyfile', '') or ''
        try:
            cfg = Config()
            meta = cfg.get_connection_meta(connection.nickname) if hasattr(cfg, 'get_connection_meta') else {}
            if isinstance(meta, dict) and 'auth_method' in meta:
                prefer_password = int(meta.get('auth_method', 0) or 0) == 1
        except Exception:
            try:
                prefer_password = int(getattr(connection, 'auth_method', 0) or 0) == 1
            except Exception:
                prefer_password = False
        try:
            key_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
        except Exception:
            key_mode = 0
        try:
            keyfile_ok = bool(keyfile) and os.path.isfile(keyfile)
        except Exception:
            keyfile_ok = False
        # Handle authentication with saved credentials
        if key_mode == 1 and keyfile_ok:
            argv += ['-i', keyfile, '-o', 'IdentitiesOnly=yes']
            
            # Try to get saved passphrase for the key
            try:
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    saved_passphrase = self.connection_manager.get_key_passphrase(keyfile)
                    if saved_passphrase:
                        # Use the secure askpass script for passphrase authentication
                        # This avoids storing passphrases in plain text temporary files
                        from .askpass_utils import get_ssh_env_with_forced_askpass, get_scp_ssh_options
                        askpass_env = get_ssh_env_with_forced_askpass()
                        # Store for later use in the main execution
                        if not hasattr(self, '_scp_askpass_env'):
                            self._scp_askpass_env = {}
                        self._scp_askpass_env.update(askpass_env)
                        logger.debug(f"SCP: Stored askpass environment for key passphrase: {list(askpass_env.keys())}")
                        
                        # Add SSH options to force public key authentication and prevent password fallback
                        argv += get_scp_ssh_options()
            except Exception as e:
                logger.debug(f"Failed to get saved passphrase for SCP: {e}")
                
        elif prefer_password:
            argv += ['-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password,keyboard-interactive']
            
            # Try to get saved password
            try:
                if hasattr(self, 'connection_manager') and self.connection_manager:
                    saved_password = self.connection_manager.get_password(connection.host, connection.username)
                    if saved_password:
                        # Use sshpass for password authentication
                        import shutil
                        sshpass_path = None
                        
                        # Check if sshpass is available and executable
                        if os.path.exists('/app/bin/sshpass') and os.access('/app/bin/sshpass', os.X_OK):
                            sshpass_path = '/app/bin/sshpass'
                            logger.debug("Found sshpass at /app/bin/sshpass")
                        elif shutil.which('sshpass'):
                            sshpass_path = shutil.which('sshpass')
                            logger.debug(f"Found sshpass in PATH: {sshpass_path}")
                        else:
                            logger.debug("sshpass not found or not executable")
                        
                        if sshpass_path:
                            # Use the same approach as ssh_password_exec.py for consistency
                            from .ssh_password_exec import _mk_priv_dir, _write_once_fifo
                            import threading
                            
                            # Create private temp directory and FIFO
                            tmpdir = _mk_priv_dir()
                            fifo = os.path.join(tmpdir, "pw.fifo")
                            os.mkfifo(fifo, 0o600)
                            
                            # Start writer thread that writes the password exactly once
                            t = threading.Thread(target=_write_once_fifo, args=(fifo, saved_password), daemon=True)
                            t.start()
                            
                            # Use sshpass with FIFO
                            argv = [sshpass_path, "-f", fifo] + argv
                            
                            logger.debug("Using sshpass with FIFO for SCP password authentication")
                            
                            # Store tmpdir for cleanup (will be cleaned up when process exits)
                            def cleanup_tmpdir():
                                try:
                                    import shutil
                                    shutil.rmtree(tmpdir, ignore_errors=True)
                                except Exception:
                                    pass
                            import atexit
                            atexit.register(cleanup_tmpdir)
                        else:
                            # sshpass not available → use askpass env (same pattern as in ssh-copy-id path)
                            from .askpass_utils import get_ssh_env_with_askpass
                            askpass_env = get_ssh_env_with_askpass("force")
                            # Store for later use in the main execution
                            if not hasattr(self, '_scp_askpass_env'):
                                self._scp_askpass_env = {}
                            self._scp_askpass_env.update(askpass_env)
                            logger.debug("SCP: sshpass unavailable, using SSH_ASKPASS fallback")
                    else:
                        # No saved password - will use interactive prompt
                        logger.debug("SCP: Password auth selected but no saved password - using interactive prompt")
            except Exception as e:
                logger.debug(f"Failed to get saved password for SCP: {e}")
        
        # Paths
        for p in local_paths:
            argv.append(p)
        target = f"{connection.username}@{connection.host}" if getattr(connection, 'username', '') else str(connection.host)
        argv.append(f"{target}:{remote_dir}")
        return argv

    def on_delete_connection_clicked(self, button):
        """Handle delete connection button click"""
        selected_row = self.connection_list.get_selected_row()
        if not selected_row:
            return
        
        connection = selected_row.connection
        
        # If host has active connections/tabs, warn about closing them first
        has_active_terms = bool(self.connection_to_terminals.get(connection, []))
        if getattr(connection, 'is_connected', False) or has_active_terms:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_('Remove host?'),
                body=_('Close connections and remove host?')
            )
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('close_remove', _('Close and Remove'))
            dialog.set_response_appearance('close_remove', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
        else:
            # Simple delete confirmation when not connected
            dialog = Adw.MessageDialog.new(self, _('Delete Connection?'),
                                         _('Are you sure you want to delete "{}"?').format(connection.nickname))
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('delete', _('Delete'))
            dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('cancel')
            dialog.set_close_response('cancel')

        dialog.connect('response', self.on_delete_connection_response, connection)
        dialog.present()

    def on_delete_connection_response(self, dialog, response, connection):
        """Handle delete connection dialog response"""
        if response == 'delete':
            # Simple deletion when not connected
            self.connection_manager.remove_connection(connection)
        elif response == 'close_remove':
            # Close connections immediately (no extra confirmation), then remove
            try:
                # Disconnect all terminals for this connection
                for term in list(self.connection_to_terminals.get(connection, [])):
                    try:
                        if hasattr(term, 'disconnect'):
                            term.disconnect()
                    except Exception:
                        pass
                # Also disconnect the active terminal if tracked separately
                term = self.active_terminals.get(connection)
                if term and hasattr(term, 'disconnect'):
                    try:
                        term.disconnect()
                    except Exception:
                        pass
            finally:
                # Remove connection without further prompts
                self.connection_manager.remove_connection(connection)

    def _on_tab_close_confirmed(self, dialog, response_id, tab_view, page):
        """Handle response from tab close confirmation dialog"""
        dialog.destroy()
        if response_id == 'close':
            self._close_tab(tab_view, page)
        # If cancelled, do nothing - the tab remains open
    
    def _close_tab(self, tab_view, page):
        """Close the tab and clean up resources"""
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                # Get the connection associated with this terminal using reverse map
                connection = self.terminal_to_connection.get(child)
                # Disconnect the terminal
                child.disconnect()
                # Clean up multi-tab tracking maps
                try:
                    if connection is not None:
                        # Remove from list for this connection
                        if connection in self.connection_to_terminals and child in self.connection_to_terminals[connection]:
                            self.connection_to_terminals[connection].remove(child)
                            if not self.connection_to_terminals[connection]:
                                del self.connection_to_terminals[connection]
                        # Update most-recent mapping
                        if connection in self.active_terminals and self.active_terminals[connection] is child:
                            remaining = self.connection_to_terminals.get(connection)
                            if remaining:
                                self.active_terminals[connection] = remaining[-1]
                            else:
                                del self.active_terminals[connection]
                    if child in self.terminal_to_connection:
                        del self.terminal_to_connection[child]
                except Exception:
                    pass
        
        # Close the tab page
        tab_view.close_page(page)
        
        # Update the UI based on the number of remaining tabs
        GLib.idle_add(self._update_ui_after_tab_close)
    
    def on_tab_close(self, tab_view, page):
        """Handle tab close - THE KEY FIX: Never call close_page ourselves"""
        # If we are closing pages programmatically (e.g., after deleting a
        # connection), suppress the confirmation dialog and allow the default
        # close behavior to proceed.
        if getattr(self, '_suppress_close_confirmation', False):
            return False
        # Get the connection for this tab
        connection = None
        terminal = None
        if hasattr(page, 'get_child'):
            child = page.get_child()
            if hasattr(child, 'disconnect'):
                terminal = child
                connection = self.terminal_to_connection.get(child)
        
        if not connection:
            # For non-terminal tabs, allow immediate close
            return False  # Allow the default close behavior
        
        # Check if confirmation is required
        confirm_disconnect = self.config.get_setting('confirm-disconnect', True)
        
        if confirm_disconnect:
            # Store tab view and page as instance variables
            self._pending_close_tab_view = tab_view
            self._pending_close_page = page
            self._pending_close_connection = connection
            self._pending_close_terminal = terminal
            
            # Show confirmation dialog
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Close connection to {}").format(connection.nickname or connection.host),
                body=_("Are you sure you want to close this connection?")
            )
            dialog.add_response('cancel', _("Cancel"))
            dialog.add_response('close', _("Close"))
            dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close')
            dialog.set_close_response('cancel')
            
            # Connect to response signal before showing the dialog
            dialog.connect('response', self._on_tab_close_response)
            dialog.present()
            
            # Prevent the default close behavior while we show confirmation
            return True
        else:
            # If no confirmation is needed, just allow the default close behavior.
            # The default handler will close the page, which in turn triggers the
            # terminal disconnection via the page's 'unmap' or 'destroy' signal.
            return False

    def _on_tab_close_response(self, dialog, response_id):
        """Handle the response from the close confirmation dialog."""
        # Retrieve the pending tab info
        tab_view = self._pending_close_tab_view
        page = self._pending_close_page
        terminal = self._pending_close_terminal

        if response_id == 'close':
            # User confirmed, disconnect the terminal. The tab will be removed
            # by the AdwTabView once we finish the close operation.
            if terminal and hasattr(terminal, 'disconnect'):
                terminal.disconnect()
            # Now, tell the tab view to finish closing the page.
            tab_view.close_page_finish(page, True)
            
            # Check if this was the last tab and show welcome screen if needed
            if tab_view.get_n_pages() == 0:
                self.show_welcome_view()
        else:
            # User cancelled, so we reject the close request.
            # This is the critical step that makes the close button work again.
            tab_view.close_page_finish(page, False)

        dialog.destroy()
        # Clear pending state to avoid memory leaks
        self._pending_close_tab_view = None
        self._pending_close_page = None
        self._pending_close_connection = None
        self._pending_close_terminal = None
    
    def on_tab_attached(self, tab_view, page, position):
        """Handle tab attached"""
        pass

    def on_tab_detached(self, tab_view, page, position):
        """Handle tab detached"""
        # Cleanup terminal-to-connection maps when a page is detached
        try:
            if hasattr(page, 'get_child'):
                child = page.get_child()
                if child in self.terminal_to_connection:
                    connection = self.terminal_to_connection.get(child)
                    # Remove reverse map
                    del self.terminal_to_connection[child]
                    # Remove from per-connection list
                    if connection in self.connection_to_terminals and child in self.connection_to_terminals[connection]:
                        self.connection_to_terminals[connection].remove(child)
                        if not self.connection_to_terminals[connection]:
                            del self.connection_to_terminals[connection]
                    # Update most recent mapping if needed
                    if connection in self.active_terminals and self.active_terminals[connection] is child:
                        remaining = self.connection_to_terminals.get(connection)
                        if remaining:
                            self.active_terminals[connection] = remaining[-1]
                        else:
                            del self.active_terminals[connection]
        except Exception:
            pass

        # Show welcome view if no more tabs are left
        if tab_view.get_n_pages() == 0:
            self.show_welcome_view()

    def on_terminal_connected(self, terminal):
        """Handle terminal connection established"""
        # Update the connection's is_connected status
        terminal.connection.is_connected = True
        
        # Update connection row status
        if terminal.connection in self.connection_rows:
            row = self.connection_rows[terminal.connection]
            row.update_status()
            row.queue_draw()  # Force redraw
        
        # Hide reconnecting feedback if visible and reset controlled flag
        GLib.idle_add(self._hide_reconnecting_message)
        self._is_controlled_reconnect = False

        # Log connection event
        if not getattr(self, '_is_controlled_reconnect', False):
            logger.info(f"Terminal connected: {terminal.connection.nickname} ({terminal.connection.username}@{terminal.connection.host})")
        else:
            logger.debug(f"Terminal reconnected after settings update: {terminal.connection.nickname}")

    def on_terminal_disconnected(self, terminal):
        """Handle terminal connection lost"""
        # Update the connection's is_connected status
        terminal.connection.is_connected = False
        
        # Update connection row status
        if terminal.connection in self.connection_rows:
            row = self.connection_rows[terminal.connection]
            row.update_status()
            row.queue_draw()  # Force redraw
            
        logger.info(f"Terminal disconnected: {terminal.connection.nickname} ({terminal.connection.username}@{terminal.connection.host})")
        
        # Do not reset controlled reconnect flag here; it is managed by the
        # reconnection flow (_on_reconnect_response/_reset_controlled_reconnect)

        # Toasts are disabled per user preference; no notification here.
        pass
            
    def on_connection_added(self, manager, connection):
        """Handle new connection added to the connection manager"""
        logger.info(f"New connection added: {connection.nickname}")
        self.add_connection_row(connection)
        
    def on_terminal_title_changed(self, terminal, title):
        """Handle terminal title change"""
        # Update the tab title with the new terminal title
        page = self.tab_view.get_page(terminal)
        if page:
            if title and title != terminal.connection.nickname:
                page.set_title(f"{terminal.connection.nickname} - {title}")
            else:
                page.set_title(terminal.connection.nickname)
                
    def on_connection_removed(self, manager, connection):
        """Handle connection removed from the connection manager"""
        logger.info(f"Connection removed: {connection.nickname}")

        # Remove from UI if it exists
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            self.connection_list.remove(row)
            del self.connection_rows[connection]

        # Close all terminals for this connection and clean up maps
        terminals = list(self.connection_to_terminals.get(connection, []))
        # Suppress confirmation while we programmatically close pages
        self._suppress_close_confirmation = True
        try:
            for term in terminals:
                try:
                    page = self.tab_view.get_page(term)
                    if page:
                        self.tab_view.close_page(page)
                except Exception:
                    pass
                try:
                    if hasattr(term, 'disconnect'):
                        term.disconnect()
                except Exception:
                    pass
                # Remove reverse map entry for each terminal
                try:
                    if term in self.terminal_to_connection:
                        del self.terminal_to_connection[term]
                except Exception:
                    pass
        finally:
            self._suppress_close_confirmation = False
        if connection in self.connection_to_terminals:
            del self.connection_to_terminals[connection]
        if connection in self.active_terminals:
            del self.active_terminals[connection]



    def on_connection_added(self, manager, connection):
        """Handle new connection added"""
        self.add_connection_row(connection)

    def on_connection_removed(self, manager, connection):
        """Handle connection removed (multi-tab aware)"""
        # Remove from UI
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            self.connection_list.remove(row)
            del self.connection_rows[connection]

        # Close all terminals for this connection and clean up maps
        terminals = list(self.connection_to_terminals.get(connection, []))
        # Suppress confirmation while we programmatically close pages
        self._suppress_close_confirmation = True
        try:
            for term in terminals:
                try:
                    page = self.tab_view.get_page(term)
                    if page:
                        self.tab_view.close_page(page)
                except Exception:
                    pass
                try:
                    if hasattr(term, 'disconnect'):
                        term.disconnect()
                except Exception:
                    pass
                # Remove reverse map entry for each terminal
                try:
                    if term in self.terminal_to_connection:
                        del self.terminal_to_connection[term]
                except Exception:
                    pass
        finally:
            self._suppress_close_confirmation = False
        if connection in self.connection_to_terminals:
            del self.connection_to_terminals[connection]
        if connection in self.active_terminals:
            del self.active_terminals[connection]

    def on_connection_status_changed(self, manager, connection, is_connected):
        """Handle connection status change"""
        logger.debug(f"Connection status changed: {connection.nickname} - {'Connected' if is_connected else 'Disconnected'}")
        if connection in self.connection_rows:
            row = self.connection_rows[connection]
            # Force update the connection's is_connected state
            connection.is_connected = is_connected
            # Update the row's status
            row.update_status()
            # Force a redraw of the row
            row.queue_draw()

        # If this was a controlled reconnect and we are now connected, hide feedback
        if is_connected and getattr(self, '_is_controlled_reconnect', False):
            GLib.idle_add(self._hide_reconnecting_message)
            self._is_controlled_reconnect = False

        # Use the same reliable status to control terminal banners
        try:
            for term in self.connection_to_terminals.get(connection, []) or []:
                if hasattr(term, '_set_disconnected_banner_visible'):
                    if is_connected:
                        term._set_disconnected_banner_visible(False)
                    else:
                        # Do not force-show here to avoid duplicate messages; terminals handle showing on failure/loss
                        pass
        except Exception:
            pass

    def on_setting_changed(self, config, key, value):
        """Handle configuration setting change"""
        logger.debug(f"Setting changed: {key} = {value}")
        
        # Apply relevant changes
        if key.startswith('terminal.'):
            # Update terminal themes/fonts
            for terms in self.connection_to_terminals.values():
                for terminal in terms:
                    terminal.apply_theme()

    def on_window_size_changed(self, window, param):
        """Handle window size change"""
        width = self.get_default_size()[0]
        height = self.get_default_size()[1]
        sidebar_width = self._get_sidebar_width()
        
        self.config.save_window_geometry(width, height, sidebar_width)

    def simple_close_handler(self, window):
        """Handle window close - distinguish between tab close and window close"""
        logger.info("")
        
        try:
            # Check if we have any tabs open
            n_pages = self.tab_view.get_n_pages()
            logger.info(f" Number of tabs: {n_pages}")
            
            # If we have tabs, close all tabs first and then quit
            if n_pages > 0:
                logger.info(" CLOSING ALL TABS FIRST")
                # Close all tabs
                while self.tab_view.get_n_pages() > 0:
                    page = self.tab_view.get_nth_page(0)
                    self.tab_view.close_page(page)
            
            # Now quit the application
            logger.info(" QUITTING APPLICATION")
            app = self.get_application()
            if app:
                app.quit()
                
        except Exception as e:
            logger.error(f" ERROR IN WINDOW CLOSE: {e}")
            # Force quit even if there's an error
            app = self.get_application()
            self.show_quit_confirmation_dialog()
            return False  # Don't quit yet, let dialog handle it
        
        # No active connections, safe to quit
        self._do_quit()
        return True  # Safe to quit

    def on_close_request(self, window):
        """Handle window close request - MAIN ENTRY POINT"""
        if self._is_quitting:
            return False  # Already quitting, allow close
            
        # Check for active connections across all tabs
        actually_connected = {}
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                if getattr(term, 'is_connected', False):
                    actually_connected.setdefault(conn, []).append(term)
        if actually_connected:
            self.show_quit_confirmation_dialog()
            return True  # Prevent close, let dialog handle it
        
        # No active connections, safe to close
        return False  # Allow close

    def show_quit_confirmation_dialog(self):
        """Show confirmation dialog when quitting with active connections"""
        # Only count terminals that are actually connected across all tabs
        connected_items = []
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                if getattr(term, 'is_connected', False):
                    connected_items.append((conn, term))
        active_count = len(connected_items)
        connection_names = [conn.nickname for conn, _ in connected_items]
        
        if active_count == 1:
            message = f"You have 1 active SSH connection to '{connection_names[0]}'."
            detail = "Closing the application will disconnect this connection."
        else:
            message = f"You have {active_count} active SSH connections."
            detail = f"Closing the application will disconnect all connections:\n• " + "\n• ".join(connection_names)
        
        dialog = Adw.AlertDialog()
        dialog.set_heading("Active SSH Connections")
        dialog.set_body(f"{message}\n\n{detail}")
        
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('quit', 'Quit Anyway')
        dialog.set_response_appearance('quit', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('quit')
        dialog.set_close_response('cancel')
        
        dialog.connect('response', self.on_quit_confirmation_response)
        dialog.present(self)
    
    def on_quit_confirmation_response(self, dialog, response):
        """Handle quit confirmation dialog response"""
        dialog.close()
        
        if response == 'quit':
            # Start cleanup process
            self._cleanup_and_quit()

    def on_open_new_connection_action(self, action, param=None):
        """Open a new tab for the selected connection via context menu."""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            self.connect_to_host(connection, force_new=True)
        except Exception as e:
            logger.error(f"Failed to open new connection tab: {e}")

    def on_open_new_connection_tab_action(self, action, param=None):
        """Open a new tab for the selected connection via global shortcut (Ctrl+Alt+N)."""
        try:
            # Get the currently selected connection
            row = self.connection_list.get_selected_row()
            if row and hasattr(row, 'connection'):
                connection = row.connection
                self.connect_to_host(connection, force_new=True)
            else:
                # If no connection is selected, show a message or fall back to new connection dialog
                logger.debug("No connection selected for Ctrl+Alt+N, opening new connection dialog")
                self.show_connection_dialog()
        except Exception as e:
            logger.error(f"Failed to open new connection tab with Ctrl+Alt+N: {e}")

    def on_manage_files_action(self, action, param=None):
        """Handle manage files action from context menu"""
        if hasattr(self, '_context_menu_connection') and self._context_menu_connection:
            connection = self._context_menu_connection
            try:
                # Define error callback for async operation
                def error_callback(error_msg):
                    logger.error(f"Failed to open file manager for {connection.nickname}: {error_msg}")
                    # Show error dialog to user
                    self._show_manage_files_error(connection.nickname, error_msg or "Failed to open file manager")
                
                success, error_msg = open_remote_in_file_manager(
                    user=connection.username,
                    host=connection.host,
                    port=connection.port if connection.port != 22 else None,
                    error_callback=error_callback,
                    parent_window=self
                )
                if success:
                    logger.info(f"Started file manager process for {connection.nickname}")
                else:
                    logger.error(f"Failed to start file manager process for {connection.nickname}: {error_msg}")
                    # Show error dialog to user
                    self._show_manage_files_error(connection.nickname, error_msg or "Failed to start file manager process")
            except Exception as e:
                logger.error(f"Error opening file manager: {e}")
                # Show error dialog to user
                self._show_manage_files_error(connection.nickname, str(e))

    def on_edit_connection_action(self, action, param=None):
        """Handle edit connection action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            self.show_connection_dialog(connection)
        except Exception as e:
            logger.error(f"Failed to edit connection: {e}")

    def on_delete_connection_action(self, action, param=None):
        """Handle delete connection action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            
            # Use the same logic as the button click handler
            # If host has active connections/tabs, warn about closing them first
            has_active_terms = bool(self.connection_to_terminals.get(connection, []))
            if getattr(connection, 'is_connected', False) or has_active_terms:
                dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_('Remove host?'),
                    body=_('Close connections and remove host?')
                )
                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('close_remove', _('Close and Remove'))
                dialog.set_response_appearance('close_remove', Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response('close')
                dialog.set_close_response('cancel')
            else:
                # Simple delete confirmation when not connected
                dialog = Adw.MessageDialog.new(self, _('Delete Connection?'),
                                             _('Are you sure you want to delete "{}"?').format(connection.nickname))
                dialog.add_response('cancel', _('Cancel'))
                dialog.add_response('delete', _('Delete'))
                dialog.add_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response('cancel')
                dialog.set_close_response('cancel')

            dialog.connect('response', self.on_delete_connection_response, connection)
            dialog.present()
        except Exception as e:
            logger.error(f"Failed to delete connection: {e}")

    def on_open_in_system_terminal_action(self, action, param=None):
        """Handle open in system terminal action from context menu"""
        try:
            connection = getattr(self, '_context_menu_connection', None)
            if connection is None:
                # Fallback to selected row if any
                row = self.connection_list.get_selected_row()
                connection = getattr(row, 'connection', None) if row else None
            if connection is None:
                return
            
            self.open_in_system_terminal(connection)
        except Exception as e:
            logger.error(f"Failed to open in system terminal: {e}")

    def open_in_system_terminal(self, connection):
        """Open the connection in the system's default terminal"""
        try:
            # Build the SSH command
            port_text = f" -p {connection.port}" if hasattr(connection, 'port') and connection.port != 22 else ""
            ssh_command = f"ssh{port_text} {connection.username}@{connection.host}"
            
            # Get the default terminal
            terminal_command = self._get_default_terminal_command()
            
            if not terminal_command:
                # Fallback to common terminals
                common_terminals = [
                    'gnome-terminal', 'konsole', 'xterm', 'alacritty', 
                    'kitty', 'terminator', 'tilix', 'xfce4-terminal'
                ]
                
                for term in common_terminals:
                    try:
                        result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                        if result.returncode == 0:
                            terminal_command = term
                            break
                    except Exception:
                        continue
            
            if not terminal_command:
                # Last resort: try xdg-terminal
                try:
                    result = subprocess.run(['which', 'xdg-terminal'], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        terminal_command = 'xdg-terminal'
                except Exception:
                    pass
            
            if not terminal_command:
                # Show error dialog
                self._show_terminal_error_dialog()
                return
            
            # Launch the terminal with SSH command
            if terminal_command in ['gnome-terminal', 'tilix', 'xfce4-terminal']:
                # These terminals use -- to separate options from command
                cmd = [terminal_command, '--', 'bash', '-c', f'{ssh_command}; exec bash']
            elif terminal_command in ['konsole', 'terminator']:
                # These terminals use -e for command execution
                cmd = [terminal_command, '-e', f'bash -c "{ssh_command}; exec bash"']
            elif terminal_command in ['alacritty', 'kitty']:
                # These terminals use -e for command execution
                cmd = [terminal_command, '-e', 'bash', '-c', f'{ssh_command}; exec bash']
            elif terminal_command == 'xterm':
                # xterm uses -e for command execution
                cmd = [terminal_command, '-e', f'bash -c "{ssh_command}; exec bash"']
            elif terminal_command == 'xdg-terminal':
                # xdg-terminal opens the default terminal
                cmd = [terminal_command, ssh_command]
            else:
                # Generic fallback
                cmd = [terminal_command, ssh_command]
            
            logger.info(f"Launching system terminal: {' '.join(cmd)}")
            subprocess.Popen(cmd, start_new_session=True)
            
        except Exception as e:
            logger.error(f"Failed to open system terminal: {e}")
            self._show_terminal_error_dialog()

    def _get_default_terminal_command(self):
        """Get the default terminal command from desktop environment"""
        try:
            # Check for desktop-specific terminals
            desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
            
            if 'gnome' in desktop:
                return 'gnome-terminal'
            elif 'kde' in desktop or 'plasma' in desktop:
                return 'konsole'
            elif 'xfce' in desktop:
                return 'xfce4-terminal'
            elif 'cinnamon' in desktop:
                return 'gnome-terminal'  # Cinnamon uses gnome-terminal
            elif 'mate' in desktop:
                return 'mate-terminal'
            elif 'lxqt' in desktop:
                return 'qterminal'
            elif 'lxde' in desktop:
                return 'lxterminal'
            
            # Check for common terminals in PATH
            common_terminals = [
                'gnome-terminal', 'konsole', 'xfce4-terminal', 'alacritty', 
                'kitty', 'terminator', 'tilix'
            ]
            
            for term in common_terminals:
                try:
                    result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        return term
                except Exception:
                    continue
            
            return None
            
        except Exception as e:
            logger.error(f"Failed to get default terminal: {e}")
            return None

    def _show_terminal_error_dialog(self):
        """Show error dialog when no terminal is found"""
        try:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("No Terminal Found"),
                body=_("Could not find a suitable terminal application. Please install a terminal like gnome-terminal, konsole, or xterm.")
            )
            
            dialog.add_response("ok", _("OK"))
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
            dialog.present()
            
        except Exception as e:
            logger.error(f"Failed to show terminal error dialog: {e}")

    def _show_manage_files_error(self, connection_name: str, error_message: str):
        """Show error dialog for manage files failure"""
        try:
            # Determine error type for appropriate messaging
            is_ssh_error = "ssh connection" in error_message.lower() or "connection failed" in error_message.lower()
            is_timeout_error = "timeout" in error_message.lower()
            
            if is_ssh_error or is_timeout_error:
                heading = _("SSH Connection Failed")
                body = _("Could not establish SSH connection to the server. Please check:")
                
                suggestions = [
                    _("• Server is running and accessible"),
                    _("• SSH service is enabled on the server"),
                    _("• Firewall allows SSH connections"),
                    _("• Your SSH keys or credentials are correct"),
                    _("• Network connectivity to the server")
                ]
            else:
                heading = _("File Manager Error")
                body = _("Failed to open file manager for remote server.")
                suggestions = [
                    _("• Try again in a moment"),
                    _("• Check if the server is accessible"),
                    _("• Ensure you have proper permissions")
                ]
            
            # Create suggestions box
            suggestions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            suggestions_box.set_margin_top(12)
            
            for suggestion in suggestions:
                label = Gtk.Label(label=suggestion)
                label.set_halign(Gtk.Align.START)
                label.set_wrap(True)
                suggestions_box.append(label)
            
            msg = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=heading,
                body=body
            )
            msg.set_extra_child(suggestions_box)
            
            # Add technical details if available
            if error_message and error_message.strip():
                detail_label = Gtk.Label(label=error_message)
                detail_label.add_css_class("dim-label")
                detail_label.set_wrap(True)
                detail_label.set_margin_top(8)
                suggestions_box.append(detail_label)
            
            msg.add_response("ok", _("OK"))
            msg.set_default_response("ok")
            msg.set_close_response("ok")
            msg.present()
            
        except Exception as e:
            logger.error(f"Failed to show manage files error dialog: {e}")

    def _cleanup_and_quit(self):
        """Clean up all connections and quit - SIMPLIFIED VERSION"""
        if self._is_quitting:
            logger.debug("Already quitting, ignoring duplicate request")
            return
                
        logger.info("Starting cleanup before quit...")
        self._is_quitting = True
        
        # Get list of all terminals to disconnect
        connections_to_disconnect = []
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                connections_to_disconnect.append((conn, term))
        
        if not connections_to_disconnect:
            # No connections to clean up, quit immediately
            self._do_quit()
            return
        
        # Show progress dialog and perform cleanup on idle so the dialog is visible immediately
        total = len(connections_to_disconnect)
        self._show_cleanup_progress(total)
        # Schedule cleanup to run after the dialog has a chance to render
        GLib.idle_add(self._perform_cleanup_and_quit, connections_to_disconnect, priority=GLib.PRIORITY_DEFAULT_IDLE)
        # Force-quit watchdog (last resort)
        try:
            GLib.timeout_add_seconds(5, self._do_quit)
        except Exception:
            pass

    def _perform_cleanup_and_quit(self, connections_to_disconnect):
        """Disconnect terminals with UI progress, then quit. Runs on idle."""
        try:
            total = len(connections_to_disconnect)
            for index, (connection, terminal) in enumerate(connections_to_disconnect, start=1):
                try:
                    logger.debug(f"Disconnecting {connection.nickname} ({index}/{total})")
                    # Always try to cancel any pending SSH spawn quickly first
                    if hasattr(terminal, 'process_pid') and terminal.process_pid:
                        try:
                            import os, signal
                            os.kill(terminal.process_pid, signal.SIGTERM)
                        except Exception:
                            pass
                    # Skip normal disconnect if terminal not connected to avoid hangs
                    if hasattr(terminal, 'is_connected') and not terminal.is_connected:
                        logger.debug("Terminal not connected; skipped disconnect")
                    else:
                        self._disconnect_terminal_safely(terminal)
                finally:
                    # Update progress even if a disconnect fails
                    self._update_cleanup_progress(index, total)
                    # Yield to main loop to keep UI responsive
                    GLib.MainContext.default().iteration(False)
        except Exception as e:
            logger.error(f"Cleanup during quit encountered an error: {e}")
        finally:
            # Final sweep of any lingering processes (but skip terminal cleanup since we already did that)
            try:
                from .terminal import SSHProcessManager
                # Only clean up processes, not terminals
                process_manager = SSHProcessManager()
                with process_manager.lock:
                    # Make a copy of PIDs to avoid modifying the dict during iteration
                    pids = list(process_manager.processes.keys())
                    for pid in pids:
                        process_manager._terminate_process_by_pid(pid)
                    # Clear all tracked processes
                    process_manager.processes.clear()
                    # Clear terminal references
                    process_manager.terminals.clear()
            except Exception as e:
                logger.debug(f"Final SSH cleanup failed: {e}")
            # Clear active terminals and hide progress
            self.active_terminals.clear()
            self._hide_cleanup_progress()
            # Quit on next idle to flush UI updates
            GLib.idle_add(self._do_quit)
        return False  # Do not repeat

    def _show_cleanup_progress(self, total_connections):
        """Show cleanup progress dialog"""
        self._progress_dialog = Gtk.Window()
        self._progress_dialog.set_title("Closing Connections")
        self._progress_dialog.set_transient_for(self)
        self._progress_dialog.set_modal(True)
        self._progress_dialog.set_default_size(350, 120)
        self._progress_dialog.set_resizable(False)
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        
        # Progress bar
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_fraction(0)
        box.append(self._progress_bar)
        
        # Status label
        self._progress_label = Gtk.Label()
        self._progress_label.set_text(f"Closing {total_connections} connection(s)...")
        box.append(self._progress_label)
        
        self._progress_dialog.set_child(box)
        self._progress_dialog.present()

    def _update_cleanup_progress(self, completed, total):
        """Update cleanup progress"""
        if hasattr(self, '_progress_bar') and self._progress_bar:
            fraction = completed / total if total > 0 else 1.0
            self._progress_bar.set_fraction(fraction)
            
        if hasattr(self, '_progress_label') and self._progress_label:
            self._progress_label.set_text(f"Closed {completed} of {total} connection(s)...")

    def _hide_cleanup_progress(self):
        """Hide cleanup progress dialog"""
        if hasattr(self, '_progress_dialog') and self._progress_dialog:
            try:
                self._progress_dialog.close()
                self._progress_dialog = None
                self._progress_bar = None
                self._progress_label = None
            except Exception as e:
                logger.debug(f"Error closing progress dialog: {e}")

    def _show_reconnecting_message(self, connection):
        """Show a small modal indicating reconnection is in progress"""
        try:
            # Avoid duplicate dialogs
            if hasattr(self, '_reconnect_dialog') and self._reconnect_dialog:
                return

            self._reconnect_dialog = Gtk.Window()
            self._reconnect_dialog.set_title(_("Reconnecting"))
            self._reconnect_dialog.set_transient_for(self)
            self._reconnect_dialog.set_modal(True)
            self._reconnect_dialog.set_default_size(320, 100)
            self._reconnect_dialog.set_resizable(False)

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(16)
            box.set_margin_bottom(16)
            box.set_margin_start(16)
            box.set_margin_end(16)

            spinner = Gtk.Spinner()
            spinner.set_hexpand(False)
            spinner.set_vexpand(False)
            spinner.start()
            box.append(spinner)

            label = Gtk.Label()
            label.set_text(_("Reconnecting to {}...").format(getattr(connection, "nickname", "")))
            label.set_halign(Gtk.Align.START)
            label.set_hexpand(True)
            box.append(label)

            self._reconnect_spinner = spinner
            self._reconnect_label = label
            self._reconnect_dialog.set_child(box)
            self._reconnect_dialog.present()
        except Exception as e:
            logger.debug(f"Failed to show reconnecting message: {e}")

    def _hide_reconnecting_message(self):
        """Hide the reconnection progress dialog if shown"""
        try:
            if hasattr(self, '_reconnect_dialog') and self._reconnect_dialog:
                self._reconnect_dialog.close()
            self._reconnect_dialog = None
            self._reconnect_spinner = None
            self._reconnect_label = None
        except Exception as e:
            logger.debug(f"Failed to hide reconnecting message: {e}")

    def _disconnect_terminal_safely(self, terminal):
        """Safely disconnect a terminal"""
        try:
            # Try multiple disconnect methods in order of preference
            if hasattr(terminal, 'disconnect'):
                terminal.disconnect()
            elif hasattr(terminal, 'close_connection'):
                terminal.close_connection()
            elif hasattr(terminal, 'close'):
                terminal.close()
                
            # Force any remaining processes to close
            if hasattr(terminal, 'force_close'):
                terminal.force_close()
                
        except Exception as e:
            logger.error(f"Error disconnecting terminal: {e}")

    def _do_quit(self):
        """Actually quit the application - FINAL STEP"""
        try:
            logger.info("Quitting application")
            
            # Save window geometry
            self._save_window_state()
            
            # Get the application and quit
            app = self.get_application()
            if app:
                app.quit()
            else:
                # Fallback: close the window directly
                self.close()
                
        except Exception as e:
            logger.error(f"Error during final quit: {e}")
            # Force exit as last resort
            import sys
            sys.exit(0)
        
        return False  # Don't repeat timeout

    def _save_window_state(self):
        """Save window state before quitting"""
        try:
            width, height = self.get_default_size()
            sidebar_width = getattr(self.split_view, 'get_sidebar_width', lambda: 250)()
            self.config.save_window_geometry(width, height, sidebar_width)
            logger.debug(f"Saved window geometry: {width}x{height}, sidebar: {sidebar_width}")
        except Exception as e:
            logger.error(f"Failed to save window state: {e}")
            self.welcome_view.set_visible(False)
            self.tab_view.set_visible(True)
            # Update tab titles in case they've changed
            self._update_tab_titles()
    
    def _update_tab_titles(self):
        """Update tab titles"""
        for page in self.tab_view.get_pages():
            child = page.get_child()
            if hasattr(child, 'connection'):
                page.set_title(child.connection.nickname)
    
    def on_connection_saved(self, dialog, connection_data):
        """Handle connection saved from dialog"""
        try:
            if dialog.is_editing:
                # Update existing connection
                old_connection = dialog.connection
                is_connected = old_connection in self.active_terminals
                
                # Store the current terminal instance if connected
                terminal = self.active_terminals.get(old_connection) if is_connected else None
                
                try:
                    logger.info(
                        "Window.on_connection_saved(edit): saving '%s' with %d forwarding rules",
                        old_connection.nickname, len(connection_data.get('forwarding_rules', []) or [])
                    )
                except Exception:
                    pass
                
                # Detect if anything actually changed; avoid unnecessary writes/prompts
                def _norm_str(v):
                    try:
                        s = ('' if v is None else str(v)).strip()
                        # Treat keyfile placeholders as empty
                        if s.lower().startswith('select key file') or 'select key file or leave empty' in s.lower():
                            return ''
                        return s
                    except Exception:
                        return ''
                def _norm_rules(rules):
                    try:
                        return list(rules or [])
                    except Exception:
                        return []
                existing = {
                    'nickname': _norm_str(getattr(old_connection, 'nickname', '')),
                    'host': _norm_str(getattr(old_connection, 'host', '')),
                    'username': _norm_str(getattr(old_connection, 'username', '')),
                    'port': int(getattr(old_connection, 'port', 22) or 22),
                    'auth_method': int(getattr(old_connection, 'auth_method', 0) or 0),
                    'keyfile': _norm_str(getattr(old_connection, 'keyfile', '')),
                    'key_select_mode': int(getattr(old_connection, 'key_select_mode', 0) or 0),
                    'password': _norm_str(getattr(old_connection, 'password', '')),
                    'key_passphrase': _norm_str(getattr(old_connection, 'key_passphrase', '')),
                    'x11_forwarding': bool(getattr(old_connection, 'x11_forwarding', False)),
                    'forwarding_rules': _norm_rules(getattr(old_connection, 'forwarding_rules', [])),
                    'local_command': _norm_str(getattr(old_connection, 'local_command', '') or (getattr(old_connection, 'data', {}).get('local_command') if hasattr(old_connection, 'data') else '')),
                    'remote_command': _norm_str(getattr(old_connection, 'remote_command', '') or (getattr(old_connection, 'data', {}).get('remote_command') if hasattr(old_connection, 'data') else '')),
                }
                incoming = {
                    'nickname': _norm_str(connection_data.get('nickname')),
                    'host': _norm_str(connection_data.get('host')),
                    'username': _norm_str(connection_data.get('username')),
                    'port': int(connection_data.get('port') or 22),
                    'auth_method': int(connection_data.get('auth_method') or 0),
                    'keyfile': _norm_str(connection_data.get('keyfile')),
                    'key_select_mode': int(connection_data.get('key_select_mode') or 0),
                    'password': _norm_str(connection_data.get('password')),
                    'key_passphrase': _norm_str(connection_data.get('key_passphrase')),
                    'x11_forwarding': bool(connection_data.get('x11_forwarding', False)),
                    'forwarding_rules': _norm_rules(connection_data.get('forwarding_rules')),
                    'local_command': _norm_str(connection_data.get('local_command')),
                    'remote_command': _norm_str(connection_data.get('remote_command')),
                }
                # Determine if anything meaningful changed by comparing canonical SSH config blocks
                try:
                    existing_block = self.connection_manager.format_ssh_config_entry(existing)
                    incoming_block = self.connection_manager.format_ssh_config_entry(incoming)
                    # Also include auth_method/password/key_select_mode delta in change detection
                    pw_changed_flag = bool(connection_data.get('password_changed', False))
                    ksm_changed = (existing.get('key_select_mode', 0) != incoming.get('key_select_mode', 0))
                    changed = (existing_block != incoming_block) or (existing['auth_method'] != incoming['auth_method']) or pw_changed_flag or ksm_changed or (existing['password'] != incoming['password'])
                except Exception:
                    # Fallback to dict comparison if formatter fails
                    changed = existing != incoming

                # Extra guard: if key_select_mode or auth_method differs from the object's current value, force changed
                try:
                    if int(connection_data.get('key_select_mode', -1)) != int(getattr(old_connection, 'key_select_mode', -1)):
                        changed = True
                    if int(connection_data.get('auth_method', -1)) != int(getattr(old_connection, 'auth_method', -1)):
                        changed = True
                except Exception:
                    pass

                # Always force update when editing connections - skip change detection entirely for forwarding rules
                logger.info("Editing connection '%s' - forcing update to ensure forwarding rules are synced", existing['nickname'])

                logger.debug(f"Updating connection '{old_connection.nickname}'")
                
                # Update connection in manager first
                if not self.connection_manager.update_connection(old_connection, connection_data):
                    logger.error("Failed to update connection in SSH config")
                    return
                
                # Update connection attributes in memory (ensure forwarding rules kept)
                old_connection.nickname = connection_data['nickname']
                old_connection.host = connection_data['host']
                old_connection.username = connection_data['username']
                old_connection.port = connection_data['port']
                old_connection.keyfile = connection_data['keyfile']
                old_connection.password = connection_data['password']
                old_connection.key_passphrase = connection_data['key_passphrase']
                old_connection.auth_method = connection_data['auth_method']
                # Persist key selection mode in-memory so the dialog reflects it without restart
                try:
                    old_connection.key_select_mode = int(connection_data.get('key_select_mode', getattr(old_connection, 'key_select_mode', 0)) or 0)
                except Exception:
                    pass
                old_connection.x11_forwarding = connection_data['x11_forwarding']
                old_connection.forwarding_rules = list(connection_data.get('forwarding_rules', []))
                # Update commands
                try:
                    old_connection.local_command = connection_data.get('local_command', '')
                    old_connection.remote_command = connection_data.get('remote_command', '')
                except Exception:
                    pass
                
                # The connection has already been updated in-place, so we don't need to reload from disk
                # The forwarding rules are already updated in the connection_data
                
                # Persist per-connection metadata not stored in SSH config (auth method, etc.)
                try:
                    meta_key = old_connection.nickname
                    self.config.set_connection_meta(meta_key, {
                        'auth_method': connection_data.get('auth_method', 0)
                    })
                except Exception:
                    pass

                # Update UI
                if old_connection in self.connection_rows:
                    # Get the row before potentially modifying the dictionary
                    row = self.connection_rows[old_connection]
                    # Remove the old connection from the dictionary
                    del self.connection_rows[old_connection]
                    # Add it back with the updated connection object
                    self.connection_rows[old_connection] = row
                    # Update the display
                    row.update_display()
                else:
                    # If the connection is not in the rows, rebuild the list
                    self._rebuild_connections_list()
                
                logger.info(f"Updated connection: {old_connection.nickname}")
                
                # If the connection is active, ask if user wants to reconnect
                if is_connected and terminal is not None:
                    # Store the terminal in the connection for later use
                    old_connection._terminal_instance = terminal
                    self._prompt_reconnect(old_connection)
                
            else:
                # Create new connection
                connection = Connection(connection_data)
                # Ensure the in-memory object has the chosen auth_method immediately
                try:
                    connection.auth_method = int(connection_data.get('auth_method', 0))
                except Exception:
                    connection.auth_method = 0
                # Ensure key selection mode is applied immediately
                try:
                    connection.key_select_mode = int(connection_data.get('key_select_mode', 0) or 0)
                except Exception:
                    connection.key_select_mode = 0
                # Add the new connection to the manager's connections list
                self.connection_manager.connections.append(connection)
                

                
                # Save the connection to SSH config and emit the connection-added signal
                if self.connection_manager.update_connection(connection, connection_data):
                    # Reload from SSH config and rebuild list immediately
                    try:
                        self.connection_manager.load_ssh_config()
                        self._rebuild_connections_list()
                    except Exception:
                        pass
                    # Persist per-connection metadata then reload config
                    try:
                        self.config.set_connection_meta(connection.nickname, {
                            'auth_method': connection_data.get('auth_method', 0)
                        })
                        try:
                            self.connection_manager.load_ssh_config()
                            self._rebuild_connections_list()
                        except Exception:
                            pass
                    except Exception:
                        pass
                    # Sync forwarding rules from a fresh reload to ensure UI matches disk
                    try:
                        reloaded_new = self.connection_manager.find_connection_by_nickname(connection.nickname)
                        if reloaded_new:
                            connection.forwarding_rules = list(reloaded_new.forwarding_rules or [])
                            logger.info("New connection '%s' has %d rules after write", connection.nickname, len(connection.forwarding_rules))
                    except Exception:
                        pass
                    # Manually add the connection to the UI since we're not using the signal
                    # Row list was rebuilt from config; no manual add required
                    logger.info(f"Created new connection: {connection_data['nickname']}")
                else:
                    logger.error("Failed to save connection to SSH config")
                
        except Exception as e:
            logger.error(f"Failed to save connection: {e}")
            # Show error dialog
            error_dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=_("Failed to save connection"),
                secondary_text=str(e)
            )
            error_dialog.present()
    
    def _rebuild_connections_list(self):
        """Rebuild the sidebar connections list from manager state, avoiding duplicates."""
        try:
            # Clear listbox children
            child = self.connection_list.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                self.connection_list.remove(child)
                child = nxt
            # Clear mapping
            self.connection_rows.clear()
            # Re-add from manager
            for conn in self.connection_manager.get_connections():
                self.add_connection_row(conn)
        except Exception:
            pass
    def _prompt_reconnect(self, connection):
        """Show a dialog asking if user wants to reconnect with new settings"""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=_("Settings Changed"),
            secondary_text=_("The connection settings have been updated.\n"
                           "Would you like to reconnect with the new settings?")
        )
        dialog.connect("response", self._on_reconnect_response, connection)
        dialog.present()
    
    def _on_reconnect_response(self, dialog, response_id, connection):
        """Handle response from reconnect prompt"""
        dialog.destroy()
        
        # Only proceed if user clicked Yes and the connection is still active
        if response_id != Gtk.ResponseType.YES or connection not in self.active_terminals:
            # Clean up the stored terminal instance if it exists
            if hasattr(connection, '_terminal_instance'):
                delattr(connection, '_terminal_instance')
            return
            
        # Get the terminal instance either from active_terminals or the stored instance
        terminal = self.active_terminals.get(connection) or getattr(connection, '_terminal_instance', None)
        if not terminal:
            logger.warning("No terminal instance found for reconnection")
            return
            
        # Set controlled reconnect flag
        self._is_controlled_reconnect = True

        # Show reconnecting feedback
        self._show_reconnecting_message(connection)
        
        try:
            # Disconnect first (defer to avoid blocking)
            logger.debug("Disconnecting terminal before reconnection")
            def _safe_disconnect():
                try:
                    terminal.disconnect()
                    logger.debug("Terminal disconnected, scheduling reconnect")
                    # Store the connection temporarily in active_terminals if not present
                    if connection not in self.active_terminals:
                        self.active_terminals[connection] = terminal
                    # Reconnect after disconnect completes
                    GLib.timeout_add(1000, self._reconnect_terminal, connection)  # Increased delay
                except Exception as e:
                    logger.error(f"Error during disconnect: {e}")
                    GLib.idle_add(self._show_reconnect_error, connection, str(e))
                return False
            
            # Defer disconnect to avoid blocking the UI thread
            GLib.idle_add(_safe_disconnect)
            
        except Exception as e:
            logger.error(f"Error during reconnection: {e}")
            # Remove from active terminals if reconnection fails
            if connection in self.active_terminals:
                del self.active_terminals[connection]
                
            # Show error to user
            error_dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=_("Reconnection Failed"),
                secondary_text=_("Failed to reconnect with the new settings. Please try connecting again manually.")
            )
            error_dialog.present()
            
        finally:
            # Clean up the stored terminal instance
            if hasattr(connection, '_terminal_instance'):
                delattr(connection, '_terminal_instance')
                
            # Reset the flag after a delay to ensure it's not set during normal operations
            GLib.timeout_add(1000, self._reset_controlled_reconnect)
    
    def _reset_controlled_reconnect(self):
        """Reset the controlled reconnect flag"""
        self._is_controlled_reconnect = False
    
    def _reconnect_terminal(self, connection):
        """Reconnect a terminal with updated connection settings"""
        if connection not in self.active_terminals:
            logger.warning(f"Connection {connection.nickname} not found in active terminals")
            return False  # Don't repeat the timeout
            
        terminal = self.active_terminals[connection]
        
        try:
            logger.debug(f"Attempting to reconnect terminal for {connection.nickname}")
            
            # Reconnect with new settings
            if not terminal._connect_ssh():
                logger.error("Failed to reconnect with new settings")
                # Show error to user
                GLib.idle_add(self._show_reconnect_error, connection)
                return False
                
            logger.info(f"Successfully reconnected terminal for {connection.nickname}")
            
        except Exception as e:
            logger.error(f"Error reconnecting terminal: {e}", exc_info=True)
            GLib.idle_add(self._show_reconnect_error, connection, str(e))
            
        return False  # Don't repeat the timeout
        
    def _show_reconnect_error(self, connection, error_message=None):
        """Show an error message when reconnection fails"""
        # Ensure reconnecting feedback is hidden
        self._hide_reconnecting_message()
        # Remove from active terminals if reconnection fails
        if connection in self.active_terminals:
            del self.active_terminals[connection]
            
        # Update UI to show disconnected state
        if connection in self.connection_rows:
            self.connection_rows[connection].update_status()
        
        # Show error dialog
        error_dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=_("Reconnection Failed"),
            secondary_text=error_message or _("Failed to reconnect with the new settings. Please try connecting again manually.")
        )
        error_dialog.present()
        
        # Clean up the dialog when closed
        error_dialog.connect("response", lambda d, r: d.destroy())