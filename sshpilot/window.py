"""
Main Window for SSH Pilot
Primary UI with connection list, tabs, and terminal management
"""

from __future__ import annotations

import asyncio
import copy
import os
import logging
import re
import shlex
import sys
import time
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Tuple

if TYPE_CHECKING:
    from .command_blocks import CommandBlocksPanel, CommandBlockStore
    from .terminal import TerminalWidget


import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
try:
    gi.require_version('Vte', '3.91')
    _HAS_VTE = True
except Exception:
    _HAS_VTE = False

gi.require_version('PangoFT2', '1.0')
from gi.repository import Gtk, Adw, Gio, GLib, Gdk
import subprocess
import threading

# Feature detection for libadwaita versions across distros
HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')
HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')
HAS_TIMED_ANIMATION = hasattr(Adw, 'TimedAnimation')

from gettext import gettext as _

from .connection_manager import ConnectionManager, Connection, ConnectionState
from .config import Config
from .key_manager import KeyManager
from .update_checker import check_for_updates_async
from .connection_display import (
    get_connection_alias,
    get_connection_host,
    format_connection_host_display,
)
from .connection_sort import (
    CONNECTION_SORT_PRESETS,
    DEFAULT_CONNECTION_SORT,
    apply_connection_sort as apply_sort_to_manager,
)
# Port forwarding UI is now integrated into connection_dialog.py
# ConnectionDialog is imported lazily at its use site (show_connection_dialog) so
# the connection-dialog module stays off the startup import path.
from .file_manager_integration import (
    should_hide_external_terminal_options,
    should_hide_file_manager_options,
)
# SshCopyIdRunner/SshCopyIdWindow and ScpWindowController are imported lazily
# (see the sshcopyid_runner / scp_controller properties and their use sites) so
# the sshcopyid_window and scp_window modules stay off the startup import path.
from .groups import GroupManager
from .session_manager import SessionManager
from .sidebar import (
    GroupRow,
    ConnectionRow,
    build_sidebar,
    reset_connection_list_drag_session,
)
from .tag_groups import compute_tag_groups

from .welcome_page import WelcomePage
from .actions import WindowActions, register_window_actions
from .window_broadcast import WindowBroadcastMixin
from .window_session import WindowSessionMixin
from .window_help import WindowHelpMixin
from .window_file_manager import WindowFileManagerMixin
from .window_tabs import WindowTabsMixin
from .window_dialogs import WindowConfigDialogsMixin
from . import shutdown
from .search_utils import connection_matches
from .shortcut_utils import get_primary_modifier_label
from .platform_utils import is_macos, get_config_dir
from .context_menu import IconContextMenu
from .plugins.api import Capability
from .plugins.registry import capabilities_for
from .ssh_password_exec import run_ssh_with_password
from .remote_path_utils import (
    _format_ssh_target,
    _normalize_remote_path,
    _quote_remote_path_for_shell,
)

logger = logging.getLogger(__name__)


def _is_terminal_widget(widget) -> bool:
    from .terminal import TerminalWidget
    return isinstance(widget, TerminalWidget)


_tips_banner_css_installed = False


def _ensure_tips_banner_css() -> None:
    """Install the accent (Adwaita blue) styling for the terminal tips banner.

    The banner is a plain Gtk.Box (``.tips-banner``) inside a Gtk.Revealer, so we
    paint the box and recolor its inline buttons. @accent_bg_color /
    @accent_fg_color follow the user's accent + light/dark theme. Installed once
    per display, mirroring split_view's CSS helper.
    """
    global _tips_banner_css_installed
    if _tips_banner_css_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(b"""
.tips-banner {
    background-color: @accent_bg_color;
    background-image: none;
    color: @accent_fg_color;
    padding: 6px 12px;
}
""")
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_USER,
    )
    _tips_banner_css_installed = True


def list_remote_files(
    host: str,
    user: str,
    remote_path: str,
    *,
    port: int = 22,
    password: Optional[str] = None,
    known_hosts_path: Optional[str] = None,
    extra_ssh_opts: Optional[List[str]] = None,
    use_publickey: bool = False,
    inherit_env: Optional[Dict[str, str]] = None,
    saved_passphrase: Optional[str] = None,
    keyfile: Optional[str] = None,
    key_mode: Optional[int] = None,
) -> Tuple[List[Tuple[str, bool]], Optional[str]]:
    """List remote files via SSH for the provided path.

    Returns (entries, error_message) where entries contain ``(name, is_directory)`` tuples.
    """
    if not host:
        return [], _('Missing host information.')

    target = _format_ssh_target(host, user)
    safe_path = _normalize_remote_path(remote_path)

    command_path = _quote_remote_path_for_shell(safe_path)
    # -L dereferences symlinks when classifying, so a symlink that points to a
    # directory is marked with a trailing "/" (and thus shown/navigated as a
    # folder). -p alone leaves symlinked dirs unmarked. See issue #1002.
    list_command = f"LC_ALL=C ls -1pL --color=never -- {command_path}"
    wrapped_command = (
        "set -f; "
        "printf '__SSHPILOT_BEGIN__\\n'; "
        f"{list_command}; "
        "status=$?; "
        "printf '__SSHPILOT_STATUS__%s\\n' \"$status\"; "
        "printf '__SSHPILOT_END__\\n'; "
        "exit $status"
    )

    env = (inherit_env or os.environ).copy()
    
    # Set up askpass environment if we have a keyfile (askpass will handle passphrase retrieval/prompting)
    # Check if askpass is already set up (inherited from caller)
    has_inherited_askpass = bool(
        inherit_env
        and str(inherit_env.get('SSH_ASKPASS_REQUIRE') or '').lower() == 'force'
    )
    
    # Set up askpass if we have a keyfile and not using password auth
    # The askpass script will retrieve from storage or show GUI dialog if needed
    if keyfile and not password and not has_inherited_askpass:
        try:
            from .askpass_utils import (
                get_ssh_env_with_forced_askpass,
                get_scp_ssh_options,
            )
        except Exception:
            get_ssh_env_with_forced_askpass = None  # type: ignore
            get_scp_ssh_options = None  # type: ignore

        if get_ssh_env_with_forced_askpass is not None:
            try:
                askpass_env = get_ssh_env_with_forced_askpass()
                if isinstance(askpass_env, dict):
                    env.update(askpass_env)
            except Exception:
                logger.debug('SCP: Unable to initialize askpass environment', exc_info=True)

        if keyfile and '-i' not in (extra_ssh_opts or []):
            if extra_ssh_opts is None:
                extra_ssh_opts = []
            extra_ssh_opts.extend(['-i', keyfile])

        if key_mode == 1 and extra_ssh_opts and 'IdentitiesOnly=yes' not in ' '.join(extra_ssh_opts):
            if extra_ssh_opts is None:
                extra_ssh_opts = []
            extra_ssh_opts.extend(['-o', 'IdentitiesOnly=yes'])

        if get_scp_ssh_options is not None:
            try:
                passphrase_opts = list(get_scp_ssh_options())
            except Exception:
                passphrase_opts = []
            if extra_ssh_opts is None:
                extra_ssh_opts = []
            for idx in range(0, len(passphrase_opts) - 1, 2):
                flag = passphrase_opts[idx]
                value = passphrase_opts[idx + 1]
                if not flag or not value:
                    continue
                already = False
                for opt_idx in range(0, len(extra_ssh_opts) - 1, 2):
                    if extra_ssh_opts[opt_idx] == flag and extra_ssh_opts[opt_idx + 1] == value:
                        already = True
                        break
                if not already:
                    extra_ssh_opts.extend([flag, value])
    elif not has_inherited_askpass:
        # Only remove askpass environment if it wasn't inherited (e.g., when identity agent is disabled)
        env.pop('SSH_ASKPASS', None)
        env.pop('SSH_ASKPASS_REQUIRE', None)

    try:
        # sshpass only for password-method listing. Key-based (use_publickey),
        # even with a stored password, must not use sshpass — it hides residual
        # keyboard-interactive prompts (same rule as resolve_native_auth).
        if password and not use_publickey:
            result = run_ssh_with_password(
                host,
                user,
                password,
                port=port,
                argv_tail=['sh', '-lc', wrapped_command],
                known_hosts_path=known_hosts_path,
                extra_ssh_opts=extra_ssh_opts or [],
                inherit_env=env,
                use_publickey=False,
            )
        else:
            sshbin = shutil.which('ssh') or '/usr/bin/ssh'
            cmd = [sshbin, '-p', str(port)]
            if extra_ssh_opts:
                cmd.extend(extra_ssh_opts)
            if known_hosts_path:
                cmd += ['-o', f'UserKnownHostsFile={known_hosts_path}']
            else:
                cmd += ['-o', 'StrictHostKeyChecking=accept-new']
            cmd.append(target)
            cmd.extend(['sh', '-lc', wrapped_command])
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=env,
            )

        stdout_lines = result.stdout.splitlines()
        begin_idx = next((idx for idx, line in enumerate(stdout_lines)
                          if line.strip() == '__SSHPILOT_BEGIN__'), None)
        status_idx = next((idx for idx, line in enumerate(stdout_lines)
                           if line.startswith('__SSHPILOT_STATUS__')), None)
        if begin_idx is None or status_idx is None or status_idx < begin_idx:
            logger.warning('SCP: Unexpected remote listing output for %s', safe_path)
            stderr = result.stderr.strip() or _('Unable to parse remote listing output.')
            return [], stderr
        try:
            status_line = stdout_lines[status_idx]
            status_code = int(status_line.replace('__SSHPILOT_STATUS__', '').strip() or '0')
        except ValueError:
            status_code = result.returncode

        listing_lines = stdout_lines[begin_idx + 1:status_idx]
        if status_code != 0:
            stderr = result.stderr.strip() or _('Failed to list remote directory.')
            logger.warning('SCP: Remote list failed (%s): %s', safe_path, stderr)
            return [], stderr
        entries: List[Tuple[str, bool]] = []
        for raw_line in listing_lines:
            line = raw_line.rstrip()
            if not line:
                continue
            is_dir = line.endswith('/')
            name = line[:-1] if is_dir else line
            entries.append((name, is_dir))
        return entries, None
    except Exception as exc:
        logger.error('SCP: Error listing remote files: %s', exc)
        return [], str(exc)


def resolve_app_modal_parent(from_widget=None) -> "Gtk.Window":
    """Return the primary app window to use as a modal dialog parent.

    Use this before showing any ``Adw.MessageDialog`` (or similar modal) from
    code that runs inside a secondary window — e.g. :class:`FileManagerWindow`,
    a plugin page tab, or a progress window — so the dialog stacks correctly on
    Wayland.

    Resolution order:

    1. ``Gtk.Application.window`` (the live :class:`MainWindow`)
    2. Any registered window whose class name is ``MainWindow``
    3. If ``from_widget`` is set: embedded root (``_embedded_parent.get_root()``),
       ``get_transient_for()``, ``get_root()``, or ``from_widget`` itself when it
       is a :class:`Gtk.Window`
    4. ``Gtk.Application.get_active_window()``

    Raises :class:`RuntimeError` if no suitable parent exists.

    See also :func:`present_for_modal_dialog` and :func:`show_ssh_password_dialog`.
    Pair with ``present_for_modal_dialog(parent)`` before ``dialog.present()``.
    """
    app = None
    if from_widget is not None:
        try:
            app = from_widget.get_application()
        except Exception:
            app = None
    if app is None:
        app = Gtk.Application.get_default()

    if app is not None:
        main_win = getattr(app, "window", None)
        if main_win is not None and isinstance(main_win, Gtk.Window):
            return main_win
        for win in app.get_windows():
            if win.__class__.__name__ == "MainWindow":
                return win

    if from_widget is not None:
        embedded_parent = getattr(from_widget, "_embedded_parent", None)
        if embedded_parent is not None:
            try:
                root = embedded_parent.get_root()
                if root is not None:
                    return root
            except Exception:
                pass
        try:
            transient = from_widget.get_transient_for()
            if transient is not None:
                return transient
        except Exception:
            pass
        try:
            root = from_widget.get_root()
            if root is not None:
                return root
        except Exception:
            pass
        if isinstance(from_widget, Gtk.Window):
            return from_widget

    if app is not None:
        try:
            active = app.get_active_window()
            if active is not None and isinstance(active, Gtk.Window):
                return active
        except Exception:
            pass

    raise RuntimeError("No modal parent window available")


def present_for_modal_dialog(window: Gtk.Window) -> None:
    """Raise *window* before showing a modal child so it stacks on top (Wayland).

    Calls ``unminimize()`` and ``present()`` on *window*. Always invoke this on
    the parent returned by :func:`resolve_app_modal_parent` immediately before
    presenting a modal dialog.
    """
    try:
        window.unminimize()
    except Exception:
        pass
    try:
        window.present()
    except Exception as exc:
        logger.debug("Failed to present modal parent window: %s", exc)


def show_ssh_password_dialog(
    *,
    from_widget=None,
    parent_window: Optional[Gtk.Window] = None,
    display_name: str = "",
    host: Optional[str] = None,
    username: Optional[str] = None,
    connection: Any = None,
    connection_manager: Optional[Any] = None,
    heading: Optional[str] = None,
    body: Optional[str] = None,
    store_label: Optional[str] = None,
    on_store: Optional[Any] = None,
) -> Optional[str]:
    """Show the standard in-app SSH **password** dialog (blocking).

    This is the single supported entry point for prompting the user for an SSH
    login password from core features, plugins (advanced), and secondary windows
    (file manager, authorized-keys editor, external SFTP mount, …). Do **not**
    roll a custom ``Adw.MessageDialog`` for passwords — use this helper so
    Wayland stacking, copy, and keyring storage behave consistently.

    The dialog is modal, parented on :class:`MainWindow` (via
    :func:`resolve_app_modal_parent`), and blocks until the user dismisses it
    (nested ``GLib.MainLoop``). **Must be called on the GTK main thread.**

    Parameters
    ----------
    from_widget
        Any widget or window tied to the caller (plugin page, file-manager pane,
        progress dialog, …). Used to locate :class:`MainWindow` when
        *parent_window* is omitted. Pass the widget that logically triggered the
        prompt.
    parent_window
        Explicit parent. When set, skips :func:`resolve_app_modal_parent` but
        still calls :func:`present_for_modal_dialog`. Prefer omitting this and
        passing *from_widget* so the main window is chosen automatically.
    display_name
        Shown in the default body text (e.g. connection nickname or
        ``user@host``). Ignored when *connection* supplies a nickname and
        *display_name* is empty.
    host, username
        Used for the optional **Store password** checkbox (via
        *connection_manager*). When *connection* is given, missing *host* /
        *username* are filled from ``hostname`` / ``host`` / ``nickname`` and
        ``username`` on the connection object.
    connection
        Optional connection record (built-in ``Connection`` or any object with
        ``nickname``, ``username``, ``hostname`` / ``host``). Convenient way to
        pass display and storage fields together.
    connection_manager
        When the user checks **Store password**, calls
        ``connection_manager.store_password(host, username, password)``. Pass
        ``self.connection_manager`` from :class:`MainWindow` or
        ``ctx.connection_manager`` from a plugin context.
    heading, body
        Optional overrides for dialog title and message (e.g. auth-retry text).
    store_label, on_store
        Custom storage hook for the **Store** checkbox. When *on_store* is given,
        the checkbox is labelled *store_label* and ``on_store(password)`` is
        called when the user ticks it — instead of the built-in
        ``connection_manager.store_password`` path. Used to persist a sudo
        password under its own keyring schema without touching the SSH-password
        store.

    Returns
    -------
    str or None
        The entered password, or ``None`` if the user cancelled or submitted an
        empty password.

    Examples
    --------
    From a built-in secondary window (file manager pattern)::

        password = show_ssh_password_dialog(
            from_widget=self,
            connection=self._connection,
            connection_manager=self._connection_manager,
        )

    From a plugin page (UI thread; ``ctx.connection_manager`` escape hatch)::

        password = show_ssh_password_dialog(
            from_widget=page_widget,
            display_name=info.nickname,
            host=info.host,
            username=info.username,
            connection_manager=ctx.connection_manager,
        )

    See :meth:`MainWindow.prompt_ssh_password` when you already hold a reference
    to the main window. For **key passphrases** prompted outside the askpass
    helper process, use :meth:`MainWindow.prompt_ssh_passphrase` instead.
    """
    storage_host = host
    storage_user = username
    prompt_name = display_name

    if connection is not None:
        storage_user = storage_user or getattr(connection, "username", None)
        storage_host = (
            storage_host
            or getattr(connection, "hostname", None)
            or getattr(connection, "host", None)
            or getattr(connection, "nickname", None)
        )
        if not prompt_name:
            nickname = getattr(connection, "nickname", None)
            user_label = storage_user or ""
            host_label = storage_host or ""
            prompt_name = (
                str(nickname)
                if nickname
                else (f"{user_label}@{host_label}" if user_label else str(host_label))
            )

    if parent_window is not None:
        parent = parent_window
    else:
        parent = resolve_app_modal_parent(from_widget)

    present_for_modal_dialog(parent)
    return _show_password_passphrase_dialog(
        parent,
        prompt_type="password",
        display_name=prompt_name,
        host=storage_host,
        username=storage_user,
        connection=connection,
        connection_manager=connection_manager,
        heading=heading,
        body=body,
        store_label=store_label,
        on_store=on_store,
    )


def _show_password_passphrase_dialog(
    parent_window,
    prompt_type: str = "password",
    display_name: str = "",
    key_path: Optional[str] = None,
    host: Optional[str] = None,
    username: Optional[str] = None,
    connection: Optional[Any] = None,
    connection_manager: Optional[Any] = None,
    *,
    heading: Optional[str] = None,
    body: Optional[str] = None,
    store_label: Optional[str] = None,
    on_store: Optional[Any] = None,
) -> Optional[str]:
    """Show a graphical password or passphrase dialog.
    
    Parameters
    ----------
    parent_window : Gtk.Window
        Parent window for the dialog
    prompt_type : str
        Either "password" or "passphrase"
    display_name : str
        Display name for the connection (e.g., "user@host")
    key_path : Optional[str]
        Path to the key file (for passphrase prompts)
    host : Optional[str]
        Host name for storing password (for password prompts)
    username : Optional[str]
        Username for storing password (for password prompts)
    connection_manager : Optional[Any]
        Connection manager instance for storing passwords
    heading, body
        Optional overrides for the dialog title and message text.
    
    Returns
    -------
    Optional[str]
        The entered password/passphrase, or None if cancelled
    """
    password_result = [None]  # Use list to allow modification in nested function
    store_checked = [False]  # Use list to allow modification in nested function
    main_loop = GLib.MainLoop()
    
    # Determine dialog heading and body text
    if heading is None:
        if prompt_type == "passphrase":
            heading = _("Passphrase Required")
        else:
            heading = _("Password Required")
    if body is None:
        if prompt_type == "passphrase":
            if key_path:
                key_name = os.path.basename(key_path)
                body = _("Please enter the passphrase for key {key_name}:").format(key_name=key_name)
            else:
                body = _("Please enter your passphrase:")
        elif display_name:
            body = _("Please enter your password for {display_name}:").format(display_name=display_name)
        else:
            body = _("Please enter your password:")
    if prompt_type == "passphrase":
        placeholder = _("Passphrase")
        default_store_label = _("Store passphrase")
    else:
        placeholder = _("Password")
        default_store_label = _("Store password")
    if not store_label:
        store_label = default_store_label
    
    # Create password/passphrase dialog
    dialog = Adw.MessageDialog(
        transient_for=parent_window,
        modal=True,
        heading=heading,
        body=body,
    )
    
    # Create a container box for entry and checkbox
    content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content_box.set_margin_top(12)
    content_box.set_margin_bottom(12)
    content_box.set_margin_start(12)
    content_box.set_margin_end(12)
    
    # Add password entry
    password_entry = Gtk.PasswordEntry()
    password_entry.set_property("placeholder-text", placeholder)
    content_box.append(password_entry)
    
    # Add checkbox to store password/passphrase
    store_checkbox = Gtk.CheckButton(label=store_label)
    store_checkbox.set_active(False)
    content_box.append(store_checkbox)

    persists_secrets = True
    try:
        from .secret_storage import get_secret_manager
        persists_secrets = get_secret_manager().persists_secrets()
    except Exception:
        persists_secrets = True
    if not persists_secrets:
        store_checkbox.set_visible(False)
        no_store_label = Gtk.Label(
            label=_(
                "Secret storage is set to SSH Agent Only — passwords and passphrases "
                "are not saved by sshPilot."
            ),
        )
        no_store_label.set_wrap(True)
        no_store_label.set_xalign(0)
        for css in ("dim-label", "caption"):
            try:
                no_store_label.add_css_class(css)
            except Exception:
                pass
        content_box.append(no_store_label)
    
    # Add container to dialog's extra child area
    dialog.set_extra_child(content_box)
    
    # Add responses
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", _("OK"))
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")
    
    # Handle Enter key - try multiple approaches for maximum compatibility
    def on_entry_activate(_entry):
        """Handle Enter key press in password entry"""
        dialog.emit("response", "ok")
    
    # Try to set activates-default property (works for Gtk.Entry)
    try:
        password_entry.set_property("activates-default", True)
    except (TypeError, AttributeError):
        pass
    
    # Also connect to activate signal as fallback
    try:
        password_entry.connect("activate", on_entry_activate)
    except (TypeError, AttributeError):
        # Fallback to key controller if activate signal is not available
        key_controller = Gtk.EventControllerKey()
        def on_key_pressed(_controller, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
                dialog.emit("response", "ok")
                return True
            return False
        key_controller.connect("key-pressed", on_key_pressed)
        password_entry.add_controller(key_controller)
    
    # Focus password entry when dialog is shown
    def on_dialog_shown(_dialog):
        password_entry.grab_focus()
    dialog.connect("notify::visible", lambda d, _: on_dialog_shown(d) if d.get_visible() else None)
    
    def on_response(_dialog, response: str) -> None:
        if response == "ok":
            entered_password = password_entry.get_text()
            if entered_password:
                password_result[0] = entered_password
                store_checked[0] = store_checkbox.get_active()
                
                # Store password/passphrase if checkbox is checked
                if store_checked[0]:
                    if on_store is not None:
                        # Caller-supplied storage hook (e.g. sudo-password keyring).
                        try:
                            on_store(entered_password)
                        except Exception as e:
                            logger.debug(f"Failed to store via on_store hook: {e}")
                    elif prompt_type == "passphrase" and key_path:
                        # Store passphrase
                        try:
                            from .askpass_utils import store_passphrase
                            store_passphrase(key_path, entered_password)
                        except Exception as e:
                            logger.debug(f"Failed to store passphrase: {e}")
                    elif prompt_type == "password" and connection_manager:
                        try:
                            if connection is not None and hasattr(
                                    connection_manager, 'store_connection_password'):
                                connection_manager.store_connection_password(
                                    connection, entered_password, username=username)
                            elif host and username:
                                from .credential_model import canonical_password_host
                                canonical = canonical_password_host(
                                    {'hostname': host, 'host': host, 'username': username})
                                store_host = canonical or host
                                connection_manager.store_password(
                                    store_host, username, entered_password)
                        except Exception as e:
                            logger.debug(f"Failed to store password: {e}")
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


def maybe_set_native_controls(header_bar: Gtk.HeaderBar, value: bool = False) -> None:
    """
    Safely set native controls on header bar, with fallback for older GTK versions.
    Only affects macOS and requires GTK 4.18+. GTK will handle title buttons by default.
    """
    # Only exists in GTK 4.18+
    is_418_plus = (
        Gtk.get_major_version() > 4 or
        (Gtk.get_major_version() == 4 and Gtk.get_minor_version() >= 18)
    )

    if sys.platform == "darwin" and is_418_plus:
        try:
            header_bar.set_use_native_controls(value)
        except AttributeError:
            pass  # extra safety in case of odd bindings
_get_connection_host = get_connection_host
_get_connection_alias = get_connection_alias
_format_connection_host_display = format_connection_host_display


def _effective_max_sidebar_width(saved_value, default: int = 400) -> int:
    """Resolve the startup max sidebar width from a saved setting value.

    Returns the saved width when it is a valid integer, otherwise ``default``.
    Kept as a module-level pure function so the parsing/fallback logic is unit
    testable without building the GTK window.
    """
    if saved_value is None:
        return default
    try:
        return int(saved_value)
    except (TypeError, ValueError):
        logger.warning("Invalid ui.max-sidebar-width %r; using default %d", saved_value, default)
        return default


class MainWindow(Adw.ApplicationWindow, WindowBroadcastMixin, WindowSessionMixin, WindowHelpMixin, WindowFileManagerMixin, WindowTabsMixin, WindowConfigDialogsMixin, WindowActions):
    """Main application window"""

    def __init__(self, *args, isolated: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Icon theme path is already registered in main.py load_resources()
        # No need to register again here
        
        self.active_terminals = {}
        self.connections = []
        self._is_quitting = False  # Flag to prevent multiple quit attempts
        self._is_controlled_reconnect = False  # Flag to track controlled reconnection
        self._internal_file_manager_windows: List[Any] = []

        # Command Blocks panel (right-side sidebar)
        self.command_block_store: Optional[CommandBlockStore] = None
        self.command_blocks_panel: Optional[CommandBlocksPanel] = None
        self.cmd_split_view = None
        self._command_sidebar_visible: bool = False
        self._cmd_blocks_toggle_btn = None
        self._headerbar_theme_menu_button = None
        
        # Update notification
        self.update_banner = None
        self._latest_version = None

        # Initialize managers
        app = self.get_application()
        app_config = getattr(app, 'config', None) if app else None
        if app_config is not None:
            self.config = app_config
        else:
            self.config = Config()
            if app is not None:
                setattr(app, 'config', self.config)
        self._config_changed_handler = None
        self._startup_tasks_scheduled = False
        self._startup_complete = False
        self._pending_focus_operations = []
        if hasattr(self.config, 'connect'):
            try:
                self._config_changed_handler = self.config.connect('setting-changed', self._on_config_setting_changed)
            except Exception:
                self._config_changed_handler = None
        effective_isolated = isolated or bool(self.config.get_setting('ssh.use_isolated_config', False))
        key_dir = Path(get_config_dir()) if effective_isolated else None
        self.connection_manager = ConnectionManager(self.config, isolated_mode=effective_isolated)

        # Menu section that plugin pages append to (built before create_menu
        # runs during setup_ui; the host mutates it on bind).
        self._plugins_menu_section = Gio.Menu()

        # Load plugins once per process (cached on the application object so a
        # second window doesn't re-activate them) before any terminal can spawn.
        # The PluginHost (event bus + UI host) is likewise process-wide; it is
        # bound to the first window's live UI at the end of setup_ui.
        try:
            from .plugins.loader import load_plugins
            from .plugins.host import PluginHost
            host = getattr(app, 'plugin_host', None) if app else None
            if host is None:
                host = PluginHost(connection_manager=self.connection_manager)
                if app is not None:
                    setattr(app, 'plugin_host', host)
            self.plugin_host = host
            if app is not None:
                setattr(app, 'plugin_host', host)
            loaded_plugins = getattr(app, 'loaded_plugins', None) if app else None
            if loaded_plugins is None:
                loaded_plugins = load_plugins(
                    app_config=self.config,
                    connection_manager=self.connection_manager,
                    plugin_host=host,
                )
                if app is not None:
                    setattr(app, 'loaded_plugins', loaded_plugins)
            self.loaded_plugins = loaded_plugins
        except Exception:
            logger.exception("Plugin loading failed")
            self.loaded_plugins = []
            self.plugin_host = None

        self.key_manager = KeyManager(key_dir)
        self.group_manager = GroupManager(self.config)
        self.session_manager = SessionManager(self.config)
        
        # UI state
        self.active_terminals: Dict[Connection, TerminalWidget] = {}  # most recent terminal per connection
        self.connection_to_terminals: Dict[Connection, List[TerminalWidget]] = {}
        self.terminal_to_connection: Dict[TerminalWidget, Connection] = {}
        self.connection_rows = {}   # connection -> [row_widget, ...] (a connection may appear in several groups)
        self._context_menu_row = None
        self._context_menu_popover = None
        # Hide hosts toggle state
        try:
            self._hide_hosts = bool(self.config.get_setting('ui.hide_hosts', False))
        except Exception:
            self._hide_hosts = False

        # Active tag filter (casefolded tag key), or None for all connections
        self._tag_filter = None

        # Remember last chosen sort preset
        try:
            stored_sort = str(self.config.get_setting('ui.connection_sort_last', DEFAULT_CONNECTION_SORT))
        except Exception:
            stored_sort = DEFAULT_CONNECTION_SORT
        if stored_sort not in CONNECTION_SORT_PRESETS:
            stored_sort = DEFAULT_CONNECTION_SORT
        self._connection_sort_last = stored_sort
        self.sort_button = None
        
        # Set up window
        self.setup_window()
        self.setup_ui()
        self.setup_connections()
        self.setup_signals()

        # Terminal manager handles terminal-related operations (import deferred so
        # terminal.py stays off the window module import path until __init__).
        from .terminal_manager import TerminalManager
        self.terminal_manager = TerminalManager(self)
        self._sshcopyid_runner = None  # built lazily via the sshcopyid_runner property
        self._scp_controller = None  # built lazily via the scp_controller property

        # Add action for activating connections
        self.activate_action = Gio.SimpleAction.new('activate-connection', None)
        self.activate_action.connect('activate', self.on_activate_connection)
        self.add_action(self.activate_action)

        # Register remaining window actions
        register_window_actions(self)
        # (Toasts disabled) Remove any toast-related actions if previously defined
        try:
            if hasattr(self, '_toast_reconnect_action'):
                self.remove_action('toast-reconnect')
        except Exception:
            pass
        
        # Connect to close request signal
        self.connect('close-request', self.on_close_request)

        # Start with welcome view (tab view setup already shows welcome initially)

        # Schedule deferred startup behaviors
        self._schedule_startup_tasks()

        logger.info("Main window initialized")

    @property
    def sshcopyid_runner(self):
        """Lazily build the ssh-copy-id runner on first use, keeping the
        sshcopyid_window module off the startup import path."""
        if self._sshcopyid_runner is None:
            from .sshcopyid_window import SshCopyIdRunner
            self._sshcopyid_runner = SshCopyIdRunner(self)
        return self._sshcopyid_runner

    @sshcopyid_runner.setter
    def sshcopyid_runner(self, value):
        # Preserve the previously-assignable public attribute (e.g. tests inject a runner).
        self._sshcopyid_runner = value

    @property
    def scp_controller(self):
        """Lazily build the SCP controller on first use, keeping the scp_window
        module off the startup import path."""
        if self._scp_controller is None:
            from .scp_window import ScpWindowController
            self._scp_controller = ScpWindowController(self)
        return self._scp_controller

    @scp_controller.setter
    def scp_controller(self, value):
        self._scp_controller = value

    def _on_config_setting_changed(self, _config, key, value):
        """Synchronize runtime state when configuration values change."""
        if key == 'command_blocks.always_show_sidebar':
            if bool(value):
                self._ensure_command_blocks_panel()
                self._toggle_command_blocks_panel(True)
            return

        if key == 'terminal.pass_through_mode':
            try:
                self._update_sidebar_accelerators()
            except Exception:
                pass
            return

        if key == 'ui.max-sidebar-width':
            try:
                max_width = int(value)
                self.update_sidebar_max_width(max_width)
            except (ValueError, TypeError) as e:
                logger.error(f"Invalid max-sidebar-width value: {e}")
            return

        if key == 'app-theme':
            try:
                self._sync_theme_menu_button()
            except Exception:
                logger.debug("Failed to sync theme toggle button", exc_info=True)
            return

    def _schedule_startup_tasks(self):
        """Schedule one-time startup behaviors such as focus and welcome state."""
        if getattr(self, '_startup_tasks_scheduled', False):
            return

        self._startup_tasks_scheduled = True

        try:
            self._install_sidebar_css()
        except Exception as e:
            logger.error(f"Failed to install sidebar CSS: {e}")

        # Apply header-bar button visibility preferences now that the buttons
        # exist (split view, commands, local terminal).
        try:
            self.update_headerbar_buttons()
        except Exception as e:
            logger.error(f"Failed to apply header-bar button visibility: {e}")

        # Check startup behavior setting and show appropriate view
        try:
            startup_behavior = self.config.get_setting('app-startup-behavior', 'welcome')
        except Exception as e:
            logger.error(f"Error handling startup behavior: {e}")
            startup_behavior = 'welcome'

        # On startup, focus the appropriate widget based on preference
        if startup_behavior in ('previous-session', 'saved-session'):
            try:
                GLib.idle_add(self._restore_startup_session, startup_behavior)
            except Exception as e:
                logger.error(f"Failed to restore session on startup: {e}")
            # Late focus pass once tabs (if any) have been created
            try:
                GLib.timeout_add(700, self._focus_connection_list_first_row)
            except Exception:
                pass
        elif startup_behavior == 'terminal':
            # Show terminal when explicitly requested. Create it SYNCHRONOUSLY
            # here (this runs during __init__, before the window is presented) so
            # the tab view is shown from the first frame — deferring it via
            # idle_add briefly flashed the welcome/start page first.
            try:
                self.terminal_manager.show_local_terminal()
            except Exception as e:
                logger.error(f"Failed to show local terminal on startup: {e}")

            def _focus_terminal_when_ready():
                try:
                    page = self.tab_view.get_selected_page() if hasattr(self, 'tab_view') else None
                    if page is None:
                        return
                    terminal_widget = page.get_child()
                    if terminal_widget is None:
                        return
                    if hasattr(terminal_widget, 'vte') and hasattr(terminal_widget.vte, 'grab_focus'):
                        terminal_widget.vte.grab_focus()
                    elif hasattr(terminal_widget, 'grab_focus'):
                        terminal_widget.grab_focus()
                except Exception as focus_error:
                    logger.debug(f"Failed to focus startup terminal: {focus_error}")

            # Queue terminal focus operation to avoid race conditions
            self._queue_focus_operation(_focus_terminal_when_ready)
        else:
            # Two calls: early (100 ms) for immediate visual feedback, and late
            # (700 ms, after _on_startup_complete at 500 ms) to win back focus
            # if anything else grabbed it during startup.
            try:
                GLib.timeout_add(100, self._focus_connection_list_first_row)
                GLib.timeout_add(700, self._focus_connection_list_first_row)
            except Exception:
                pass

        # Mark startup as complete after a short delay to allow all initialization to finish
        GLib.timeout_add(500, self._on_startup_complete)
        # Note: "hide sidebar on startup" is applied at split-view creation
        # (before the window is presented) so it never flashes visible.

        # If the previous run crashed, surface it once the UI has settled.
        GLib.timeout_add(1200, self._check_previous_crash)

    def _check_previous_crash(self):
        """If the previous run left a crash report, offer to view/report it."""
        try:
            app = self.get_application()
            crash_path = getattr(app, '_previous_crash_report', None) if app else None
            if not crash_path:
                return False
            # Consume so the prompt only appears once per crash.
            app._previous_crash_report = None
            self._pending_crash_report = crash_path

            heading = _("SSH Pilot closed unexpectedly")
            body = _(
                "An error report from your previous session was saved. "
                "Sending it helps us find and fix the problem."
            )
            if hasattr(Adw, 'AlertDialog'):
                dialog = Adw.AlertDialog(heading=heading, body=body)
                present = lambda d: d.present(self)
            else:
                dialog = Adw.MessageDialog(
                    transient_for=self, modal=True, heading=heading, body=body)
                present = lambda d: d.present()
            dialog.add_response('dismiss', _("Not Now"))
            dialog.add_response('logs', _("View Logs…"))
            dialog.add_response('report', _("Report a Problem…"))
            dialog.set_default_response('report')
            dialog.set_close_response('dismiss')
            try:
                dialog.set_response_appearance(
                    'report', Adw.ResponseAppearance.SUGGESTED)
            except Exception:
                pass
            dialog.connect('response', self._on_previous_crash_response)
            present(dialog)
        except Exception as exc:
            logger.debug("Previous-crash check failed: %s", exc, exc_info=True)
        return False  # one-shot

    def _on_previous_crash_response(self, dialog, response):
        # NB: self.activate_action is shadowed by a SimpleAction attribute
        # (window.py: self.activate_action = Gio.SimpleAction.new(...)), so the
        # GtkWidget.activate_action() method is not callable here. Invoke the
        # handler / action object directly instead.
        try:
            if response == 'report':
                self.on_report_problem_action()
            elif response == 'logs':
                action = getattr(self, 'view_logs_action', None)
                if action is not None:
                    action.activate(None)
        except Exception as exc:
            logger.debug("Crash dialog response failed: %s", exc, exc_info=True)

    def on_report_problem_action(self, action=None, param=None):
        """Copy a diagnostic bundle (incl. crash report), then confirm before
        opening the new-issue page so the user can paste it."""
        try:
            from .log_viewer import build_report_bundle
            crash_path = getattr(self, '_pending_crash_report', None)
            bundle = build_report_bundle(crash_path)
        except Exception as exc:
            logger.error("Could not build problem report: %s", exc, exc_info=True)
            bundle = ''
        copied = False
        try:
            display = self.get_display() or Gdk.Display.get_default()
            if display is not None and bundle:
                display.get_clipboard().set(bundle)
                copied = True
        except Exception as exc:
            logger.error("Could not copy report to clipboard: %s", exc)

        heading = _("Report a Problem")
        if copied:
            body = _(
                "Diagnostic information has been copied to your clipboard.\n\n"
                "Press OK to open the issue page, then paste (Ctrl+V) it into "
                "the report."
            )
        else:
            body = _(
                "Press OK to open the issue page. You can attach your logs from "
                "Help ▸ View Logs."
            )

        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog(heading=heading, body=body)
            present = lambda d: d.present(self)
        else:
            dialog = Adw.MessageDialog(
                transient_for=self, modal=True, heading=heading, body=body)
            present = lambda d: d.present()
        dialog.add_response('cancel', _("Cancel"))
        dialog.add_response('open', _("OK"))
        dialog.set_default_response('open')
        dialog.set_close_response('cancel')
        try:
            dialog.set_response_appearance('open', Adw.ResponseAppearance.SUGGESTED)
        except Exception:
            pass
        dialog.connect('response', self._on_report_problem_response)
        present(dialog)

    def _on_report_problem_response(self, dialog, response):
        if response != 'open':
            return
        try:
            Gtk.show_uri(self, 'https://github.com/mfat/sshpilot/issues/new',
                         Gdk.CURRENT_TIME)
        except Exception as exc:
            logger.error("Could not open issue tracker: %s", exc)

    def on_export_diagnostics_action(self, action=None, param=None):
        """Save a ZIP of logs + system info + redacted config for bug reports."""
        file_dialog = Gtk.FileDialog()
        file_dialog.set_title(_("Export Diagnostics"))
        file_dialog.set_initial_name(
            "sshpilot-diagnostics-%s.zip" % datetime.now().strftime('%Y%m%d-%H%M%S'))
        try:
            docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
            if docs:
                file_dialog.set_initial_folder(Gio.File.new_for_path(docs))
        except Exception:
            pass

        def _on_save(dialog, result):
            try:
                gfile = dialog.save_finish(result)
            except GLib.Error as exc:
                if getattr(exc, 'code', None) != 2:  # 2 = dismissed
                    logger.error("Export diagnostics dialog failed: %s", exc)
                return
            if gfile is None:
                return
            path = gfile.get_path()

            def _work():
                try:
                    from .log_viewer import build_diagnostics_zip
                    build_diagnostics_zip(path)
                    GLib.idle_add(self._on_export_diagnostics_done, True, None, path)
                except Exception as exc:
                    logger.error("Export diagnostics failed: %s", exc, exc_info=True)
                    GLib.idle_add(self._on_export_diagnostics_done, False, str(exc), path)

            threading.Thread(target=_work, daemon=True).start()

        file_dialog.save(self, None, _on_save)

    def _on_export_diagnostics_done(self, ok, error, path):
        if ok:
            dialog = Adw.MessageDialog(
                transient_for=self, modal=True,
                heading=_("Diagnostics Exported"),
                body=_("Saved to:\n{}\n\nSecrets are redacted. Your saved connections "
                       "and SSH config are not included; a maintainer can request them "
                       "separately if needed.").format(path),
            )
            dialog.add_response('open', _("Open Folder"))
            dialog.add_response('ok', _("OK"))
            dialog.set_default_response('ok')
            dialog.connect(
                'response',
                lambda d, r: self._open_containing_folder(path) if r == 'open' else None)
        else:
            dialog = Adw.MessageDialog(
                transient_for=self, modal=True,
                heading=_("Export Failed"),
                body=_("Could not export diagnostics:\n{}").format(error or _("Unknown error")),
            )
            dialog.add_response('ok', _("OK"))
        dialog.present()
        return False

    def _open_containing_folder(self, path):
        try:
            folder = os.path.dirname(path) or '.'
            uri = Gio.File.new_for_path(folder).get_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as exc:
            logger.debug("Could not open containing folder: %s", exc)

    def _restore_startup_session(self, startup_behavior):
        """Restore a session on startup based on the configured behavior."""
        try:
            session_manager = getattr(self, 'session_manager', None)
            if session_manager is None:
                return False

            data = None
            if startup_behavior == 'previous-session':
                data = session_manager.get_previous()
            elif startup_behavior == 'saved-session':
                name = self.config.get_setting('app-startup-session-name', '')
                if name:
                    data = session_manager.get_session(name)
                if data is None:
                    logger.info(f"Startup session '{name}' not found; showing start page")

            if data:
                self.restore_session(data, replace=True)
        except Exception as e:
            logger.error(f"Failed to restore startup session: {e}")
        return False  # Don't repeat

    def _on_startup_complete(self):
        """Called when startup is complete - process any pending focus operations"""
        self._startup_complete = True
        
        # Process any pending focus operations
        for focus_op in self._pending_focus_operations:
            try:
                focus_op()
            except Exception as e:
                logger.debug(f"Failed to execute pending focus operation: {e}")
        
        self._pending_focus_operations.clear()
        
        # Check for updates if enabled in preferences
        try:
            check_on_startup = self.config.get_setting('updates.check_on_startup', True)
            if check_on_startup:
                logger.debug("Checking for updates on startup")
                
                def on_update_check_complete(latest_version):
                    """Callback when startup update check completes.

                    Always route through the result handler (even when there is
                    no update) so the tips banner is surfaced once the update
                    banner's area is known to be free. ``from_startup=True``
                    keeps the silent check from raising a toast.
                    """
                    GLib.idle_add(
                        self._handle_update_check_result, latest_version, True
                    )

                check_for_updates_async(on_update_check_complete)
        except Exception as e:
            logger.debug(f"Failed to check for updates on startup: {e}")

        # One-time operation mode chooser
        try:
            if self._should_prompt_operation_mode():
                GLib.idle_add(self._show_operation_mode_dialog)
        except Exception as e:
            logger.debug(f"Failed to schedule operation mode prompt: {e}")

        return False  # Don't repeat
    
    def _queue_focus_operation(self, focus_func):
        """Queue a focus operation to be executed after startup is complete"""
        if self._startup_complete:
            # Startup is complete, execute immediately
            try:
                focus_func()
            except Exception as e:
                logger.debug(f"Failed to execute focus operation: {e}")
        else:
            # Queue for later execution
            self._pending_focus_operations.append(focus_func)

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

            /* optional: a subtle focus ring while the list is focused */
            row:selected:focus-within {
              /* box-shadow: 0 0 8px 2px @accent_bg_color inset; */
              /* border: 2px solid @accent_bg_color;  Adds a solid border of 2px thickness */
              border-radius: 8px;
            }
            
            /* Group styling */
            .group-expand-button {
              min-width: 16px;
              min-height: 16px;
              padding: 2px;
              border-radius: 4px;
            }
            
            .group-expand-button:hover {
              background: alpha(@accent_bg_color, 0.1);
            }
            
            /* Smooth drag indicator transitions */
            .drag-indicator {
              opacity: 0;
              transition: opacity 0.15s ease-in-out;
            }
            
            .drag-indicator.visible {
              opacity: 1;
            }
            
            /* Smooth transitions for connection rows during drag */
            .navigation-sidebar {
              transition: transform 0.1s ease-out, opacity 0.1s ease-out;
            }
            
            .navigation-sidebar.dragging {
              opacity: 0.7;
              transform: scale(0.98);
            }

            /* Gap between sidebar list rows (GtkListBox has no spacing property) */
            .navigation-sidebar row {
              margin: 4px 8px;
            }

            /* Selected sidebar row: always use the accent so selection is
               visible in dark mode by default. libadwaita's default
               navigation-sidebar selection is a neutral shade that is nearly
               invisible against the dark card background; this rule (which the
               accent-override path already emits, but only when an override is
               set) makes selection clear unconditionally.
               @accent_bg_color / @accent_fg_color follow the system accent and
               any user override. The more specific .tinted:selected rule below
               keeps the identical color for grouped rows. */
            .navigation-sidebar row:selected {
              background-color: @accent_bg_color;
              color: @accent_fg_color;
            }

            .navigation-sidebar row.tinted {
              margin: 4px 8px;
              border-radius: 10px;
              transition: background-color 0s ease;
            }

            .navigation-sidebar row.tinted:not(:selected) {
              background-color: alpha(@accent_bg_color, 0.18);
            }

            .navigation-sidebar row.tinted:hover:not(:selected) {
              background-color: alpha(@accent_bg_color, 0.24);
            }

            .navigation-sidebar row.tinted:active:not(:selected) {
              background-color: alpha(@accent_bg_color, 0.30);
            }

            .navigation-sidebar row.tinted:selected {
              background-color: @accent_bg_color;
              color: @accent_fg_color;
              box-shadow: inset 0 0 0 1px @accent_bg_color;
            }

            .navigation-sidebar row.tinted:selected:hover {
              background-color: shade(@accent_bg_color, 0.95);
            }

            .navigation-sidebar row.tinted:selected:active {
              background-color: shade(@accent_bg_color, 0.90);
            }

            /* Accent-bar mode: the coloured left bar is the highlight, so a
               selected row uses a neutral overlay instead of the accent fill
               (which would swamp the bar). Every bar-mode row carries
               .color-bar, and this out-specifies the accent `row:selected`
               rule above at the same provider priority. */
            .navigation-sidebar row.color-bar:selected {
              background-color: alpha(@window_fg_color, 0.10);
              color: @window_fg_color;
              box-shadow: none;
            }

            .navigation-sidebar row.color-bar:selected:hover {
              background-color: alpha(@window_fg_color, 0.13);
            }

            .navigation-sidebar row.color-bar:selected:active {
              background-color: alpha(@window_fg_color, 0.16);
            }

            /* Reorder placeholder: a slim transparent gap row whose child
               DragIndicator draws the accent bar; the list parts around it. */
            .drop-placeholder-row {
              background: transparent;
              min-height: 0;
              padding: 0;
            }

            /* Group drop target highlight */
            .drop-target-group {
              background: alpha(@accent_bg_color, 0.25);
              border-radius: 8px;
              box-shadow: 0 0 0 2px @accent_bg_color inset,
                          0 2px 8px alpha(@accent_bg_color, 0.4);
              transition: background-color 0.15s ease-in-out,
                          box-shadow 0.15s ease-in-out;
            }
            
            /* Drop target indicator styling */
            .drop-target-indicator {
              background: alpha(@accent_bg_color, 0.9);
              color: white;
              border-radius: 12px;
              padding: 4px 12px;
              margin: 4px 8px;
              font-weight: bold;
              font-size: 0.9em;
              animation: drop-indicator-bounce 0.6s ease-in-out;
            }

            @keyframes drop-indicator-bounce {
              0% {
                transform: translateY(-10px) scale(0.8);
                opacity: 0;
              }
              60% {
                transform: translateY(2px) scale(1.05);
                opacity: 1;
              }
              100% {
                transform: translateY(0) scale(1);
                opacity: 1;
              }
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


    def _first_connection_row(self) -> Optional[Gtk.ListBoxRow]:
        """Return the first row in the connection list that is a connection.

        Skips group header rows so search results that start with a matching
        group still resolve to the first matching host."""
        if not getattr(self, 'connection_list', None):
            return None
        index = 0
        while True:
            row = self.connection_list.get_row_at_index(index)
            if row is None:
                return None
            if hasattr(row, 'connection'):
                return row
            index += 1

    def _on_connection_list_nav_key(self, controller, keyval, keycode, state):
        """Keyboard handling while the connection list has focus.

        - Arrow Up from the first row returns focus to the search entry (when
          the search bar is open) so the user can keep editing the query.
        - Space selects the focused row, Ctrl/⌘+Space toggles it in the
          multi-selection (the rows are activatable, so without this the
          row's default Space binding would activate it, i.e. connect).
        - Typing a printable character starts a search (type-ahead): the search
          bar opens, takes focus, and receives the character.
        """
        if keyval in (Gdk.KEY_space, Gdk.KEY_KP_Space):
            row = self.connection_list.get_focus_child()
            if row is None:
                return False
            toggle_mask = (
                Gdk.ModifierType.CONTROL_MASK
                | getattr(Gdk.ModifierType, 'META_MASK', 0)
            )
            try:
                if state & toggle_mask:
                    # Toggle the focused row in the multi-selection.
                    if row in self.connection_list.get_selected_rows():
                        self.connection_list.unselect_row(row)
                    else:
                        self.connection_list.select_row(row)
                else:
                    self._select_only_row(row)
            except Exception:
                logger.debug("Space selection handling failed", exc_info=True)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            if (
                getattr(self, 'search_container', None)
                and self.search_container.get_visible()
                and getattr(self, 'search_entry', None)
                and getattr(self, 'connection_list', None)
            ):
                first_row = self.connection_list.get_row_at_index(0)
                focused = self.connection_list.get_focus_child()
                if first_row is not None and focused is first_row:
                    self.search_entry.grab_focus()
                    text = self.search_entry.get_text()
                    if text:
                        self.search_entry.select_region(0, len(text))
                    return True
            return False

        # Type-ahead: a printable key starts/continues a search.
        if not getattr(self, 'search_entry', None):
            return False
        shortcut_modifiers = (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | getattr(Gdk.ModifierType, 'META_MASK', 0)
            | getattr(Gdk.ModifierType, 'SUPER_MASK', 0)
        )
        if state & shortcut_modifiers:
            return False
        unicode_point = Gdk.keyval_to_unicode(keyval)
        if not unicode_point:
            return False
        char = chr(unicode_point)
        # Skip control chars and Space (Space toggles row selection in the list).
        if not char.isprintable() or char == ' ':
            return False
        self.activate_search_entry()
        self.search_entry.set_text(self.search_entry.get_text() + char)
        self.search_entry.set_position(-1)
        return True

    def _focus_is_in_connection_list(self) -> bool:
        """Return True if keyboard focus is currently on/within the connection list."""
        if not getattr(self, 'connection_list', None):
            return False
        try:
            widget = self.get_focus()
        except Exception:
            return False
        while widget is not None:
            if widget is self.connection_list:
                return True
            widget = widget.get_parent()
        return False

    def _on_focus_visible_changed(self, *args) -> None:
        """Keep the keyboard focus ring visible while navigating the connection list.

        GTK hides the window's focus-visible state after a few seconds of
        keyboard inactivity. While the user is navigating the connection list
        that reads as the selection being lost, so re-assert it as long as
        focus remains inside the list."""
        try:
            if self.get_focus_visible():
                return
            if self._focus_is_in_connection_list():
                self.set_focus_visible(True)
        except Exception:
            pass

    def _select_only_row(self, row: Optional[Gtk.ListBoxRow]) -> None:
        """Select only the provided row, clearing any other selections."""
        if not row or not getattr(self, 'connection_list', None):
            return

        try:
            if hasattr(self.connection_list, 'unselect_all'):
                self.connection_list.unselect_all()
        except Exception:
            pass

        try:
            self.connection_list.select_row(row)
        except Exception:
            pass

    def _get_selected_connection_rows(self) -> List[Gtk.ListBoxRow]:
        """Return all selected rows that represent connections."""
        if not getattr(self, 'connection_list', None):
            return []

        try:
            selected_rows = list(self.connection_list.get_selected_rows())
        except Exception:
            selected_row = self.connection_list.get_selected_row()
            selected_rows = [selected_row] if selected_row else []

        return [row for row in selected_rows if hasattr(row, 'connection')]

    def _get_selected_group_rows(self) -> List[Gtk.ListBoxRow]:
        """Return all selected rows that represent groups."""
        if not getattr(self, 'connection_list', None):
            return []

        try:
            selected_rows = list(self.connection_list.get_selected_rows())
        except Exception:
            selected_row = self.connection_list.get_selected_row()
            selected_rows = [selected_row] if selected_row else []

        return [row for row in selected_rows if hasattr(row, 'group_id')]

    def _get_target_connection_rows(self, prefer_context: bool = False) -> List[Gtk.ListBoxRow]:
        """Return rows targeted by the current action, respecting context menus."""
        rows = self._get_selected_connection_rows()
        context_row = getattr(self, '_context_menu_row', None)

        if context_row and hasattr(context_row, 'connection'):
            if rows and context_row in rows:
                return rows
            if prefer_context or not rows:
                return [context_row]

        return rows

    def _connections_from_rows(self, rows: List[Gtk.ListBoxRow]) -> List[Connection]:
        """Return unique connections represented by the provided rows."""
        connections: List[Connection] = []
        seen_ids = set()
        for row in rows:
            connection = getattr(row, 'connection', None)
            if connection and id(connection) not in seen_ids:
                seen_ids.add(id(connection))
                connections.append(connection)
        return connections

    def _get_target_connections(self, prefer_context: bool = False) -> List[Connection]:
        """Return connection objects targeted by the current action."""
        rows = self._get_target_connection_rows(prefer_context=prefer_context)
        return self._connections_from_rows(rows)

    def _page_for_child(self, child):
        """Return the Adw.TabPage for ``child`` in the main tab_view, or None.

        Unlike ``tab_view.get_page(child)``, this never emits the
        ``child_belongs_to_this_view`` CRITICAL assertion when ``child`` isn't in
        the main view — e.g. a terminal that has been embedded in a split-view
        pane (which lives in that pane's own inner tab view, not tab_view).
        """
        try:
            pages = self.tab_view.get_pages()
            for i in range(pages.get_n_items()):
                page = pages.get_item(i)
                if page is not None and page.get_child() is child:
                    return page
        except Exception:
            pass
        return None

    def _rows_for_connection(self, connection) -> List[Gtk.ListBoxRow]:
        """Return every visible row representing ``connection``.

        A connection can be shown in more than one group, so several rows may
        map to the same connection object.
        """
        rows = self.connection_rows.get(connection)
        if not rows:
            return []
        if isinstance(rows, list):
            return list(rows)
        return [rows]

    def _primary_row_for_connection(self, connection) -> Optional[Gtk.ListBoxRow]:
        """Return the first row representing ``connection`` (or ``None``)."""
        rows = self._rows_for_connection(connection)
        return rows[0] if rows else None

    def _determine_neighbor_connection_row(
        self, target_rows: List[Gtk.ListBoxRow]
    ) -> Optional[Gtk.ListBoxRow]:
        """Find the closest remaining connection row after deleting target_rows."""
        if not target_rows or not getattr(self, 'connection_list', None):
            return None

        try:
            all_rows = list(self.connection_list)
        except Exception:
            # If iteration fails, fall back to default behavior.
            return None

        if not all_rows:
            return None

        index_map = {row: idx for idx, row in enumerate(all_rows)}
        target_indexes = sorted(
            index_map[row]
            for row in target_rows
            if row in index_map
        )

        if not target_indexes:
            return None

        max_index = target_indexes[-1]
        min_index = target_indexes[0]

        # Try to find the next connection row after the targeted range.
        for idx in range(max_index + 1, len(all_rows)):
            row = all_rows[idx]
            if hasattr(row, 'connection') and row not in target_rows:
                return row

        # Fall back to previous connection rows before the targeted range.
        for idx in range(min_index - 1, -1, -1):
            row = all_rows[idx]
            if hasattr(row, 'connection') and row not in target_rows:
                return row

        return None

    def _disconnect_connection_terminals(self, connection: Connection) -> None:
        """Disconnect all tracked terminals for a connection."""
        try:
            for term in list(self.connection_to_terminals.get(connection, [])):
                try:
                    if hasattr(term, 'disconnect'):
                        term.disconnect()
                except Exception:
                    pass

            term = self.active_terminals.get(connection)
            if term and hasattr(term, 'disconnect'):
                try:
                    term.disconnect()
                except Exception:
                    pass
        except Exception:
            pass

    def _prompt_delete_connections(
        self,
        connections: List[Connection],
        neighbor_row: Optional[Gtk.ListBoxRow] = None,
    ) -> None:
        """Show a confirmation dialog for deleting one or more connections."""
        unique_connections: List[Connection] = []
        seen_ids = set()
        for connection in connections:
            if connection and id(connection) not in seen_ids:
                seen_ids.add(id(connection))
                unique_connections.append(connection)

        if not unique_connections:
            return

        active_connections = [
            connection
            for connection in unique_connections
            if getattr(connection, 'is_connected', False)
            or bool(self.connection_to_terminals.get(connection, []))
        ]

        if active_connections:
            heading = _('Remove host?') if len(unique_connections) == 1 else _('Remove connections?')
            body = _('Close connections and remove host?') if len(unique_connections) == 1 else _(
                'Close connections and remove the selected hosts?'
            )
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=heading,
                body=body,
            )
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('close_remove', _('Close and Remove'))
            dialog.set_response_appearance('close_remove', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('close_remove')
            dialog.set_close_response('cancel')
        else:
            heading = _('Delete Connection?') if len(unique_connections) == 1 else _('Delete Connections?')
            if len(unique_connections) == 1:
                nickname = unique_connections[0].nickname if hasattr(unique_connections[0], 'nickname') else ''
                body = _('Are you sure you want to delete "{}"?').format(nickname)
            else:
                body = _('Are you sure you want to delete the selected connections?')

            dialog = Adw.MessageDialog.new(self, heading, body)
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('delete', _('Delete'))
            dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('cancel')
            dialog.set_close_response('cancel')

        payload = {
            'connections': unique_connections,
            'neighbor_row': neighbor_row,
        }
        dialog.connect('response', self.on_delete_connection_response, payload)
        dialog.present()


    def _on_connection_list_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in the connection list"""

        # Handle Enter key specifically
        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            selected_row = self.connection_list.get_selected_row()
            if selected_row and hasattr(selected_row, 'connection'):
                self._return_to_tab_view_if_welcome()
                connection = selected_row.connection
                self._focus_most_recent_tab_or_open_new(connection)
                return True  # Consume the event to prevent row-activated
            return False  # Allow group rows to be handled by row-activated

        # Handle deletion keys to remove selected connections
        if keyval in (
            Gdk.KEY_Delete,
            Gdk.KEY_KP_Delete,
            Gdk.KEY_BackSpace,
        ):
            target_rows = self._get_target_connection_rows()
            connections = self._connections_from_rows(target_rows)

            if connections:
                neighbor_row = self._determine_neighbor_connection_row(target_rows)
                self._prompt_delete_connections(connections, neighbor_row)
                return True

            return False
        return False


        
        # Sidebar toggle action registered via register_window_actions

    def setup_window(self):
        """Configure main window properties"""
        self.set_title('SSH Pilot')
        self.set_icon_name('io.github.mfat.sshpilot')
        
        # Load window geometry
        geometry = self.config.get_window_geometry()
        self.set_default_size(geometry['width'], geometry['height'])
        self.set_resizable(True)
        
        # Connect window state signals
        self.connect('notify::default-width', self.on_window_size_changed)
        self.connect('notify::default-height', self.on_window_size_changed)
        # Ensure initial focus after the window is mapped
        try:
            self.connect('map', lambda *a: GLib.timeout_add(200, self._focus_connection_list_first_row))
        except Exception:
            pass

        # Tab navigation shortcuts are handled by application actions (see sshpilot/main.py)

    def setup_ui(self):
        """Set up the user interface"""
        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.set_hexpand(True)
        main_box.set_vexpand(True)
        
        # Create update notification banner (hidden by default)
        # Use overlay to position dismiss button on top of banner
        banner_overlay = Gtk.Overlay()
        banner_overlay.set_visible(False)  # Hidden by default
        
        self.update_banner = Adw.Banner()
        self.update_banner.set_revealed(False)
        banner_overlay.set_child(self.update_banner)
        
        # Create dismiss button with text, positioned at the left
        dismiss_button = Gtk.Button()
        dismiss_button.set_label('Dismiss')
        dismiss_button.set_halign(Gtk.Align.START)
        dismiss_button.set_valign(Gtk.Align.CENTER)
        dismiss_button.set_margin_start(12)
        dismiss_button.connect('clicked', self._on_update_banner_dismiss)
        banner_overlay.add_overlay(dismiss_button)
        
        self.update_banner_container = banner_overlay
        self.update_banner_dismiss_button = dismiss_button
        # Note: Update banner will be added to content area in setup_content_area()
        # to ensure it appears below the header bar

        # Create terminal tips banner (hidden by default). Shown in the same
        # area as the update banner — below the header bar, above the content —
        # instead of floating over the terminal where it would mask output.
        # The update banner takes priority over tips (see show_terminal_tip and
        # _show_update_banner).
        # A Gtk.Revealer holding a single inline row: the extra buttons are real
        # children of the banner body, so no Gtk.Overlay hack is needed to show
        # them (Adw.Banner only exposes one action button). The revealer gives us
        # the same slide-in/out animation Adw.Banner.set_revealed() provided.
        _ensure_tips_banner_css()
        self.tips_revealer = Gtk.Revealer()
        # SLIDE_DOWN slides the banner in from the top edge and collapses it back
        # up on dismiss (matching Adw.Banner's feel).
        self.tips_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.tips_revealer.set_transition_duration(250)
        self.tips_revealer.set_reveal_child(False)

        # No outer margins: the accent background must span the full width like
        # the previous Adw.Banner. Horizontal padding comes from the .tips-banner
        # CSS instead, so there are no gaps on the sides.
        tips_body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tips_body.add_css_class('tips-banner')

        # Leading-edge button cluster: "Next tip" (cycles) + "Dismiss".
        self.tips_next_button = Gtk.Button(label=_('Next tip'))
        self.tips_next_button.set_valign(Gtk.Align.CENTER)
        self.tips_next_button.connect('clicked', self._on_tips_banner_next)
        tips_dismiss_button = Gtk.Button(label=_('Dismiss'))
        tips_dismiss_button.set_valign(Gtk.Align.CENTER)
        tips_dismiss_button.connect('clicked', self._on_tips_banner_dismiss)
        tips_body.append(self.tips_next_button)
        tips_body.append(tips_dismiss_button)

        # Tip text fills the middle.
        self.tips_label = Gtk.Label()
        self.tips_label.set_hexpand(True)
        self.tips_label.set_halign(Gtk.Align.CENTER)
        self.tips_label.set_wrap(True)
        self.tips_label.set_justify(Gtk.Justification.CENTER)
        tips_body.append(self.tips_label)

        # Trailing button: "Don't show again".
        tips_dont_show_button = Gtk.Button(label=_('Don\'t show again'))
        tips_dont_show_button.set_valign(Gtk.Align.CENTER)
        tips_dont_show_button.connect('clicked', self._on_tips_banner_dont_show_again)
        tips_body.append(tips_dont_show_button)

        self.tips_revealer.set_child(tips_body)
        self.tips_banner_container = self.tips_revealer

        # Create header bar (content pane — window controls on the right with split views)
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class('flat')
        self.header_bar.set_show_start_title_buttons(True)
        self.header_bar.set_show_end_title_buttons(True)
        # Empty title so Adw doesn't repeat the window title beside tab actions.
        self.header_bar.set_title_widget(Gtk.Box())

        # Safely configure native window controls (macOS only, GTK 4.18+)
        maybe_set_native_controls(self.header_bar, False)
        
        
        # Add sidebar toggle button to the left side of header bar
        self.sidebar_toggle_button = Gtk.ToggleButton()
        self.sidebar_toggle_button.set_can_focus(False)  # Remove focus from sidebar toggle
        
        # Sidebar always starts visible
        sidebar_visible = True
        
        from sshpilot import icon_utils
        icon_utils.set_button_icon(self.sidebar_toggle_button, 'sidebar-show-symbolic')
        # Show the effective (possibly user-customized) shortcut in the tooltip.
        sidebar_accels = self._get_safe_current_shortcuts().get('toggle_sidebar') or ['F9']

        def _accel_label(accel):
            try:
                ok, keyval, mods = Gtk.accelerator_parse(accel)
                if ok and keyval:
                    return Gtk.accelerator_get_label(keyval, mods)
            except Exception:
                pass
            return accel

        accel_labels = ', '.join(_accel_label(a) for a in sidebar_accels)
        self.sidebar_toggle_button.set_tooltip_text(f'Hide Sidebar ({accel_labels})')
        # Button should not appear pressed when sidebar is visible
        self.sidebar_toggle_button.set_active(False)
        self.sidebar_toggle_button.connect('toggled', self.on_sidebar_toggle)
        self.header_bar.pack_start(self.sidebar_toggle_button)

        # Add local terminal button right after the sidebar toggle
        self.local_terminal_button = Gtk.Button()
        icon_utils.set_button_icon(self.local_terminal_button, 'utilities-terminal-symbolic')
        self.local_terminal_button.set_tooltip_text(
            f'Open Local Terminal ({get_primary_modifier_label()}+Shift+T)'
        )
        self.local_terminal_button.add_css_class('flat')
        self.local_terminal_button.set_action_name('app.local-terminal')
        self.header_bar.pack_start(self.local_terminal_button)
        # Toggled by Preferences ▸ Interface ▸ Header Bar.
        self._headerbar_local_terminal_button = self.local_terminal_button

        # Add tab button to header bar (will be created later in setup_content_area)
        # This will be added after the tab view is created
        
        # Add header bar to main container only when using traditional split views
        if not (HAS_NAV_SPLIT or HAS_OVERLAY_SPLIT):
            main_box.append(self.header_bar)
        
        # Honor the saved max-sidebar-width on startup (previously it was read but
        # ignored here, so the saved width only took effect after being changed
        # mid-session); fall back to 400 when unset/invalid.
        saved_max_width = self.config.get_setting('ui.max-sidebar-width', None)
        effective_max_width = _effective_max_sidebar_width(saved_max_width)

        # Try OverlaySplitView first as it's more reliable
        if HAS_OVERLAY_SPLIT:
            self.split_view = Adw.OverlaySplitView()
            try:
                self.split_view.set_sidebar_width_fraction(0.25)
                self.split_view.set_min_sidebar_width(180)
                self.split_view.set_max_sidebar_width(effective_max_width)
            except Exception:
                pass
            self.split_view.set_vexpand(True)
            self._split_variant = 'overlay'
            logger.debug("Using OverlaySplitView")
        elif HAS_NAV_SPLIT:
            self.split_view = Adw.NavigationSplitView()
            try:
                self.split_view.set_sidebar_width_fraction(0.25)
                self.split_view.set_min_sidebar_width(200)
                self.split_view.set_max_sidebar_width(effective_max_width)
            except Exception:
                pass
            self.split_view.set_vexpand(True)
            self._split_variant = 'navigation'
            logger.debug("Using NavigationSplitView")
        else:
            self.split_view = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
            self.split_view.set_wide_handle(True)
            self.split_view.set_vexpand(True)
            self._split_variant = 'paned'
            logger.debug("Using Gtk.Paned fallback")
        
        # Initial sidebar visibility. Apply "hide on startup" HERE — before the
        # window is presented — so it never flashes visible then collapses.
        try:
            start_hidden = bool(self.config.get_setting('ui.sidebar_hide_on_startup', False))
        except Exception:
            start_hidden = False
        sidebar_visible = not start_hidden
        # Track sidebar visibility state for NavigationSplitView (which doesn't have get_show_sidebar)
        self._sidebar_visible = sidebar_visible
        # Keep the header toggle button in sync (active == hidden).
        if hasattr(self, 'sidebar_toggle_button'):
            try:
                self.sidebar_toggle_button.set_active(start_hidden)
            except Exception:
                pass

        # For OverlaySplitView, we need to explicitly set the sidebar state
        if HAS_OVERLAY_SPLIT:
            try:
                self.split_view.set_show_sidebar(sidebar_visible)
                logger.debug(f"Set OverlaySplitView sidebar visible={sidebar_visible}")
            except Exception as e:
                logger.error(f"Failed to set OverlaySplitView sidebar: {e}")
        elif HAS_NAV_SPLIT and start_hidden:
            try:
                self._toggle_sidebar_visibility(False)
            except Exception:
                pass
        
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

        # The window UI now exists (tab_view, toast_overlay, the plugins menu
        # section). Bind the plugin host so plugin pages, toasts, events, and
        # terminal control go live. Idempotent: only the first window binds.
        try:
            if getattr(self, 'plugin_host', None) is not None:
                self.plugin_host.bind_window(self)
        except Exception:
            logger.exception("Plugin host bind_window failed")

    def _set_sidebar_widget(self, widget: Gtk.Widget) -> None:
        if HAS_OVERLAY_SPLIT:
            try:
                self.split_view.set_sidebar(widget)
                return
            except Exception:
                pass
        elif HAS_NAV_SPLIT:
            try:
                # NavigationSplitView requires the sidebar to be a NavigationPage
                # According to docs: https://gnome.pages.gitlab.gnome.org/libadwaita/doc/1.2/class.NavigationSplitView.html
                sidebar_page = Adw.NavigationPage.new(widget, _("Connections"))
                self.split_view.set_sidebar(sidebar_page)
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
        elif HAS_NAV_SPLIT:
            try:
                # NavigationSplitView content should be a NavigationPage directly
                # According to docs: https://gnome.pages.gitlab.gnome.org/libadwaita/doc/1.2/class.NavigationSplitView.html
                # Both sidebar and content must be AdwNavigationPage objects
                content_page = Adw.NavigationPage.new(widget, _("Terminal"))
                self.split_view.set_content(content_page)
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
            if (HAS_NAV_SPLIT or HAS_OVERLAY_SPLIT) and hasattr(self.split_view, 'get_max_sidebar_width'):
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

    def update_headerbar_buttons(self):
        """Show/hide the toggleable header-bar buttons per preferences
        (Settings ▸ Interface ▸ Header Bar)."""
        mapping = (
            ('split_view_button', 'ui.headerbar_show_split_view', False),
            ('_cmd_blocks_toggle_btn', 'ui.headerbar_show_commands', True),
            ('_headerbar_theme_menu_button', 'ui.headerbar_show_theme_toggle', True),
            ('_headerbar_local_terminal_button', 'ui.headerbar_show_local_terminal', True),
        )
        for attr, key, default in mapping:
            btn = getattr(self, attr, None)
            if btn is None:
                continue
            try:
                btn.set_visible(bool(self.config.get_setting(key, default)))
            except Exception:
                logger.debug("Failed to apply header-bar button visibility for %s", attr, exc_info=True)

    def update_sidebar_display(self):
        """Update sidebar display based on current preferences."""
        if not hasattr(self, 'connection_list'):
            return
        
        show_user_hostname = self.config.get_setting('ui.sidebar_show_user_hostname', True)
        show_group_count = self.config.get_setting('ui.sidebar_show_group_count', True)
        show_status = self.config.get_setting('ui.sidebar_show_connection_status', True)
        show_connection_icon = self.config.get_setting('ui.sidebar_show_connection_icon', True)
        show_group_icon = self.config.get_setting('ui.sidebar_show_group_icon', True)
        flat_rows = self.config.get_setting('ui.sidebar_flat_rows', False)
        
        # Update all rows in the connection list
        row = self.connection_list.get_first_child()
        while row:
            if hasattr(row, 'apply_row_style'):
                row.apply_row_style(flat_rows)
            # Update ConnectionRow elements
            if hasattr(row, 'connection_icon'):
                row.connection_icon.set_visible(show_connection_icon)
            if hasattr(row, 'host_label'):
                row.host_label.set_visible(show_user_hostname)
            if hasattr(row, 'status_icon'):
                # update_status() applies both the icon and visibility, honoring
                # the show_status pref AND keeping idle (UNKNOWN) rows iconless.
                if hasattr(row, 'update_status'):
                    row.update_status()
                else:
                    row.status_icon.set_visible(show_status)
            if hasattr(row, '_update_forwarding_indicators'):
                row._update_forwarding_indicators()
            
            # Update GroupRow elements
            if hasattr(row, 'count_label'):
                row.count_label.set_visible(show_group_count)
            if hasattr(row, 'group_id') and hasattr(row, 'icon'):
                row.icon.set_visible(show_group_icon)
            
            row = row.get_next_sibling()

    def update_sidebar_max_width(self, max_width: int):
        """Update the maximum sidebar width for both NavigationSplitView and OverlaySplitView."""
        try:
            if HAS_NAV_SPLIT and hasattr(self.split_view, 'set_max_sidebar_width'):
                self.split_view.set_max_sidebar_width(max_width)
                logger.debug(f"Updated NavigationSplitView max-sidebar-width to {max_width} sp")
            elif HAS_OVERLAY_SPLIT and hasattr(self.split_view, 'set_max_sidebar_width'):
                self.split_view.set_max_sidebar_width(max_width)
                logger.debug(f"Updated OverlaySplitView max-sidebar-width to {max_width} sp")
        except Exception as e:
            logger.error(f"Failed to update max-sidebar-width: {e}")


    
    def _generate_duplicate_nickname(self, base_nickname: str) -> str:
        """Generate a unique nickname for a duplicated connection."""
        try:
            existing_names = {
                str(getattr(conn, 'nickname', '')).strip()
                for conn in self.connection_manager.get_connections()
                if getattr(conn, 'nickname', None)
            }
        except Exception:
            existing_names = set()
        existing_lower = {name.lower() for name in existing_names if name}

        base = (base_nickname or '').strip()
        if not base:
            base = _('Connection')

        copy_label = _('Copy')
        # The nickname is used verbatim as the ssh Host alias, and the app's own
        # validator rejects whitespace (and parens make an invalid host token —
        # see #953). So the suffix must be whitespace/paren-free: use a hyphen
        # separator and a whitespace-free copy token, e.g. "Name-Copy",
        # "Name-Copy-2".
        copy_token = re.sub(r"\s+", "-", copy_label.strip()) or "Copy"
        # Strip an existing copy suffix in either the legacy " (Copy[ N])" form
        # or the new "-Copy[-N]" form so re-duplicating doesn't stack suffixes.
        pattern = re.compile(
            r"(?:\s*\(\s*" + re.escape(copy_label) + r"(?:\s+\d+)?\s*\)"
            r"|[-_]+" + re.escape(copy_token) + r"(?:[-_]+\d+)?)\s*$",
            re.IGNORECASE,
        )
        base_clean = pattern.sub('', base).strip() or base

        def is_unique(name: str) -> bool:
            return name.lower() not in existing_lower

        candidate = f"{base_clean}-{copy_token}"
        if is_unique(candidate):
            return candidate

        index = 2
        while True:
            candidate = f"{base_clean}-{copy_token}-{index}"
            if is_unique(candidate):
                return candidate
            index += 1

    def _show_duplicate_connection_error(self, connection: Optional[Connection], error: Exception) -> None:
        """Display an error dialog when duplication fails."""
        try:
            nickname = (getattr(connection, 'nickname', '') or _('Connection')).strip()
            heading = _('Duplicate Failed')
            body = _('Failed to duplicate connection "{name}".\n\n{details}').format(
                name=nickname,
                details=str(error) or _('An unknown error occurred.')
            )
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=heading,
                body=body,
            )
            dialog.add_response('close', _('Close'))
            dialog.set_close_response('close')
            dialog.present()
        except Exception:
            pass

    def duplicate_connection(self, connection: Optional[Connection]) -> Optional[Connection]:
        """Duplicate an existing connection, persist it, and select the new entry."""
        if connection is None:
            return None

        try:
            try:
                base_data = getattr(connection, 'data', None)
                new_data = copy.deepcopy(base_data) if isinstance(base_data, dict) else {}
            except Exception:
                new_data = {}
            if not isinstance(new_data, dict):
                new_data = {}

            for key in list(new_data.keys()):
                if key.startswith('__') or key in {'aliases', 'password_changed'}:
                    new_data.pop(key, None)

            new_nickname = self._generate_duplicate_nickname(getattr(connection, 'nickname', ''))
            new_data['nickname'] = new_nickname

            # Plugin protocols: the data dict is authoritative (no ssh_config
            # attribute fixups apply); persist through the non-SSH store.
            if getattr(connection, 'protocol', 'ssh') != 'ssh':
                new_connection = Connection(new_data)
                if not self.connection_manager.update_connection(new_connection, dict(new_data)):
                    raise RuntimeError(_('Failed to save duplicated connection.'))
                original_groups = self.group_manager.get_connection_groups(connection.nickname)
                original_group_id = original_groups[0] if original_groups else None
                if original_group_id and original_group_id in getattr(self.group_manager, 'groups', {}):
                    try:
                        self.group_manager.move_connection(new_nickname, original_group_id)
                    except Exception:
                        pass
                self.rebuild_connection_list()
                return new_connection

            host_value = (
                getattr(connection, 'hostname', '')
                or getattr(connection, 'host', '')
                or new_data.get('hostname', '')
                or new_data.get('host', '')
            )
            host_value = str(host_value).strip()
            if not host_value:
                host_value = new_nickname
            new_data['hostname'] = host_value
            new_data.pop('host', None)

            new_data['username'] = str(getattr(connection, 'username', new_data.get('username', '')) or '')

            try:
                new_data['port'] = int(getattr(connection, 'port', new_data.get('port', 22)) or 22)
            except Exception:
                new_data['port'] = 22

            try:
                new_data['auth_method'] = int(getattr(connection, 'auth_method', new_data.get('auth_method', 0)) or 0)
            except Exception:
                new_data['auth_method'] = 0

            keyfile_value = getattr(connection, 'keyfile', new_data.get('keyfile', '')) or ''
            if isinstance(keyfile_value, str) and keyfile_value.strip().lower().startswith('select key file'):
                keyfile_value = ''
            new_data['keyfile'] = keyfile_value

            certificate_value = getattr(connection, 'certificate', new_data.get('certificate', '')) or ''
            if isinstance(certificate_value, str) and certificate_value.strip().lower().startswith('select certificate'):
                certificate_value = ''
            new_data['certificate'] = certificate_value

            new_data['key_passphrase'] = getattr(connection, 'key_passphrase', new_data.get('key_passphrase', '')) or ''

            try:
                new_data['key_select_mode'] = int(getattr(connection, 'key_select_mode', new_data.get('key_select_mode', 0)) or 0)
            except Exception:
                new_data['key_select_mode'] = 0

            new_data['password'] = getattr(connection, 'password', new_data.get('password', '')) or ''
            new_data['x11_forwarding'] = bool(getattr(connection, 'x11_forwarding', new_data.get('x11_forwarding', False)))
            new_data['pubkey_auth_no'] = bool(getattr(connection, 'pubkey_auth_no', new_data.get('pubkey_auth_no', False)))
            new_data['forward_agent'] = bool(getattr(connection, 'forward_agent', new_data.get('forward_agent', False)))

            proxy_jump_value = getattr(connection, 'proxy_jump', new_data.get('proxy_jump', []))
            if isinstance(proxy_jump_value, str):
                proxy_jump_value = [h.strip() for h in re.split(r'[\s,]+', proxy_jump_value) if h.strip()]
            else:
                proxy_jump_value = list(proxy_jump_value or [])
            new_data['proxy_jump'] = proxy_jump_value

            new_data['proxy_command'] = getattr(connection, 'proxy_command', new_data.get('proxy_command', '')) or ''
            new_data['pre_command'] = getattr(connection, 'pre_command', new_data.get('pre_command', '')) or ''
            new_data['local_command'] = getattr(connection, 'local_command', new_data.get('local_command', '')) or ''
            new_data['remote_command'] = getattr(connection, 'remote_command', new_data.get('remote_command', '')) or ''
            new_data['extra_ssh_config'] = getattr(connection, 'extra_ssh_config', new_data.get('extra_ssh_config', '')) or ''

            forwarding_rules = getattr(connection, 'forwarding_rules', new_data.get('forwarding_rules', []))
            try:
                new_data['forwarding_rules'] = copy.deepcopy(list(forwarding_rules or []))
            except Exception:
                new_data['forwarding_rules'] = []

            source_path = getattr(connection, 'source', new_data.get('source'))
            if source_path:
                new_data['source'] = source_path
            else:
                new_data.pop('source', None)

            new_connection = Connection(new_data)
            if self.connection_manager.isolated_mode:
                new_connection.isolated_config = True
                new_connection.config_root = self.connection_manager.ssh_config_path
                new_connection.data['isolated_mode'] = True
                if self.connection_manager.ssh_config_path:
                    new_connection.data['config_root'] = self.connection_manager.ssh_config_path
            try:
                new_connection.auth_method = int(new_data.get('auth_method', 0) or 0)
            except Exception:
                new_connection.auth_method = 0
            try:
                new_connection.key_select_mode = int(new_data.get('key_select_mode', 0) or 0)
            except Exception:
                new_connection.key_select_mode = 0
            new_connection.forwarding_rules = list(new_data.get('forwarding_rules', []))
            new_connection.proxy_jump = list(new_data.get('proxy_jump', []))
            new_connection.forward_agent = bool(new_data.get('forward_agent', False))
            new_connection.extra_ssh_config = new_data.get('extra_ssh_config', '')
            new_connection.certificate = new_data.get('certificate', '')

            original_groups = self.group_manager.get_connection_groups(connection.nickname)
            original_group_id = original_groups[0] if original_groups else None

            self.connection_manager.connections.append(new_connection)
            try:
                if not self.connection_manager.update_connection(new_connection, new_data):
                    raise RuntimeError(_('Failed to save duplicated connection.'))
            except Exception:
                try:
                    self.connection_manager.connections.remove(new_connection)
                except ValueError:
                    pass
                raise

            self.connection_manager.load_ssh_config()

            if original_group_id and original_group_id in getattr(self.group_manager, 'groups', {}):
                self.group_manager.move_connection(new_nickname, original_group_id)
                try:
                    self.group_manager.reorder_connection_in_group(new_nickname, connection.nickname, 'below')
                except Exception:
                    pass
                # Mirror any additional group memberships of the original
                for extra_group_id in original_groups[1:]:
                    if extra_group_id in getattr(self.group_manager, 'groups', {}):
                        self.group_manager.copy_connection_to_group(new_nickname, extra_group_id)
            else:
                self.group_manager.move_connection(new_nickname, None)
                try:
                    root_connections = self.group_manager.root_connections
                    if new_nickname in root_connections and connection.nickname in root_connections:
                        root_connections.remove(new_nickname)
                        insert_at = root_connections.index(connection.nickname) + 1
                        root_connections.insert(insert_at, new_nickname)
                        self.group_manager._save_groups()
                except Exception:
                    pass

            self.rebuild_connection_list()

            duplicated = self.connection_manager.find_connection_by_nickname(new_nickname)
            row = self._primary_row_for_connection(duplicated) if duplicated else None
            if row is not None:
                self._select_only_row(row)
                try:
                    self.connection_list.scroll_to_row(row)
                except Exception:
                    pass
                try:
                    self.connection_list.grab_focus()
                except Exception:
                    pass
            return duplicated
        except Exception as error:
            self._show_duplicate_connection_error(connection, error)
            logger.error(f"Failed to duplicate connection: {error}", exc_info=True)
            return None

    @staticmethod
    def _expand_sidebar_toolbar_button(button: Gtk.Widget) -> Gtk.Widget:
        """Give a sidebar toolbar control an equal share of the row width."""
        button.set_hexpand(True)
        button.set_halign(Gtk.Align.FILL)
        return button

    def setup_sidebar(self):
        """Set up the sidebar with connection list"""
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Ensure sidebar box expands to use full allocated width from NavigationSplitView
        sidebar_box.set_hexpand(True)
        sidebar_box.set_vexpand(True)
        
        # Sidebar header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_hexpand(True)
        header.set_homogeneous(True)
        header.set_margin_start(12)
        header.set_margin_end(12)
        header.set_margin_top(12)
        header.set_margin_bottom(6)
        
        # # Title
        # title_label = Gtk.Label()
        # title_label.set_markup('<b>Connections</b>')
        # title_label.set_halign(Gtk.Align.START)
        # title_label.set_hexpand(True)
        # header.append(title_label)
        
        # Add connection button
        from sshpilot import icon_utils
        add_button = icon_utils.new_button_from_icon_name('list-add-symbolic')
        add_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(add_button)
        add_button.set_tooltip_text(
            f'Add Connection ({get_primary_modifier_label()}+Shift+N)'
        )
        add_button.connect('clicked', self.on_add_connection_clicked)
        try:
            add_button.set_can_focus(False)
        except Exception:
            pass
        header.append(add_button)

        # Search button
        self.search_button = icon_utils.new_button_from_icon_name('system-search-symbolic')
        self.search_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(self.search_button)
        # Platform-aware shortcut in tooltip
        shortcut = 'Cmd+F' if is_macos() else 'Ctrl+F'
        self.search_button.set_tooltip_text(f'Search Connections ({shortcut})')
        self.search_button.connect('clicked', lambda *_: self.focus_search_entry())
        try:
            self.search_button.set_can_focus(False)
        except Exception:
            pass
        header.append(self.search_button)

        # Hide/Show hostnames button (eye icon)
        def _update_eye_icon(btn):
            try:
                icon = 'view-conceal-symbolic' if self._hide_hosts else 'view-reveal-symbolic'
                icon_utils.set_button_icon(btn, icon)
                btn.set_tooltip_text('Show hostnames' if self._hide_hosts else 'Hide hostnames')
            except Exception:
                pass

        hide_button = icon_utils.new_button_from_icon_name('view-reveal-symbolic')
        hide_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(hide_button)
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
                for rows in self.connection_rows.values():
                    for row in (rows if isinstance(rows, list) else [rows]):
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

        # Tag filter dropdown: pick a tag to show only connections carrying it.
        tag_button = Gtk.MenuButton()
        tag_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(tag_button)
        tag_button.set_icon_name('tag-symbolic')
        tag_button.set_tooltip_text(_('Filter by tag'))

        filter_action = Gio.SimpleAction.new_stateful(
            'filter-tag', GLib.VariantType.new('s'), GLib.Variant('s', '')
        )

        def _on_filter_tag(action, param):
            try:
                action.set_state(param)
                self._tag_filter = param.get_string() or None
                self.rebuild_connection_list()
            except Exception:
                logger.error("Failed to apply tag filter", exc_info=True)

        filter_action.connect('activate', _on_filter_tag)
        self.add_action(filter_action)

        def _build_tag_menu(btn):
            # Rebuilt on every popup so new/renamed tags always show.
            try:
                menu = Gio.Menu()
                all_item = Gio.MenuItem.new(_('All Connections'), None)
                all_item.set_action_and_target_value(
                    'win.filter-tag', GLib.Variant('s', '')
                )
                menu.append_item(all_item)

                tag_map = {}
                for conn in self.connection_manager.get_connections():
                    try:
                        tag_map[conn.nickname] = self.config.get_connection_tags(conn.nickname)
                    except Exception:
                        pass
                tags_section = Gio.Menu()
                for display_tag, nicknames in compute_tag_groups(tag_map):
                    item = Gio.MenuItem.new(
                        f'{display_tag} ({len(nicknames)})', None
                    )
                    item.set_action_and_target_value(
                        'win.filter-tag', GLib.Variant('s', display_tag.casefold())
                    )
                    tags_section.append_item(item)
                menu.append_section(None, tags_section)
                btn.set_menu_model(menu)
            except Exception:
                logger.error("Failed to build tag filter menu", exc_info=True)

        tag_button.set_create_popup_func(_build_tag_menu)
        try:
            tag_button.set_can_focus(False)
        except Exception:
            pass
        header.append(tag_button)

        sort_button = self._build_sort_button()
        self._expand_sidebar_toolbar_button(sort_button)
        header.append(sort_button)

        preferences_button = self._build_preferences_button()
        self._expand_sidebar_toolbar_button(preferences_button)
        header.append(preferences_button)

        # Menu button (packed on content header bar in setup_content_area)
        self.menu_button = Gtk.MenuButton()
        self.menu_button.add_css_class('flat')
        self.menu_button.set_can_focus(False)
        # MenuButton uses set_icon_name() which goes through icon theme
        # We'll use set_icon_name() - the icon theme should find our bundled icon
        self.menu_button.set_icon_name('open-menu-symbolic')
        self.menu_button.set_tooltip_text('Menu')
        self.menu_button.set_menu_model(self.create_menu())

        header_handle = Gtk.WindowHandle()
        header_handle.set_hexpand(True)
        header_handle.set_child(header)
        sidebar_box.append(header_handle)

        # Search container
        search_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        search_container.add_css_class('search-container')
        search_container.set_margin_start(2)
        search_container.set_margin_end(2)
        search_container.set_margin_bottom(6)
        
        # Search entry for filtering connections
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_('Search connections'))
        self.search_entry.connect('search-changed', self.on_search_changed)
        self.search_entry.connect('stop-search', self.on_search_stopped)
        search_key = Gtk.EventControllerKey()
        # Use the capture phase so Down/Up/Enter are handled before the
        # SearchEntry's internal text widget consumes them (otherwise arrow
        # keys move the cursor and Enter triggers default activation).
        search_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        search_key.connect('key-pressed', self._on_search_entry_key_pressed)
        self.search_entry.add_controller(search_key)
        # Prevent search entry from being the default focus widget
        self.search_entry.set_can_focus(True)
        self.search_entry.set_focus_on_click(False)
        search_container.append(self.search_entry)
        
        # Store reference to search container for showing/hiding
        self.search_container = search_container
        
        # Hide search container by default
        search_container.set_visible(False)
        
        sidebar_box.append(search_container)

        # Connection list
        self.connection_scrolled = Gtk.ScrolledWindow()
        self.connection_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.connection_scrolled.set_vexpand(True)
        self.connection_scrolled.set_hexpand(True)
        
        self.connection_list = Gtk.ListBox()
        self.connection_list.add_css_class("navigation-sidebar")
        self.connection_list.set_hexpand(True)
        self.connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        try:
            self.connection_list.set_can_focus(True)
        except Exception:
            pass
        
        
        # Connect signals
        self.connection_list.connect('row-selected', self.on_connection_selected)  # For button sensitivity
        self.connection_list.connect('row-activated', self.on_connection_activated)  # For Enter key/double-click

        # GTK auto-hides the window's focus-visible state after a few seconds of
        # keyboard inactivity, which makes a keyboard-selected connection row
        # look deselected (the focus ring vanishes) even though it still holds
        # focus and selection. Re-assert it while focus stays in the list.
        self.connect('notify::focus-visible', self._on_focus_visible_changed)

        # Arrow Up from the first row hops back to the search entry (capture
        # phase so we intercept before the ListBox's own boundary handling).
        nav_key = Gtk.EventControllerKey()
        nav_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        nav_key.connect('key-pressed', self._on_connection_list_nav_key)
        self.connection_list.add_controller(nav_key)
        
        # Make sure the connection list is focusable and can receive key events
        self.connection_list.set_focusable(True)
        self.connection_list.set_can_focus(True)
        # Manage focus manually so double-click activation can hand control to the terminal
        self.connection_list.set_focus_on_click(False)
        self.connection_list.set_activate_on_single_click(False)  # Require double-click to activate
        
        # Set connection list as the default focus widget for the sidebar
        # Queue this operation to avoid race conditions during startup
        def _set_sidebar_focus():
            if self.connection_list.get_parent() == sidebar_box:
                sidebar_box.set_focus_child(self.connection_list)
        
        self._queue_focus_operation(_set_sidebar_focus)
        
        # Set up drag and drop for reordering
        build_sidebar(self)

        # Right-click context menu using simple gesture without coordinate detection
        try:
            # Use a simple gesture but avoid all coordinate-based operations
            context_click = Gtk.GestureClick()
            context_click.set_button(Gdk.BUTTON_SECONDARY)  # Only handle right-click
            # Capture phase so this gesture handles the right-click before the
            # ListBox's own row handling.
            context_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

            def _build_and_show_menu(row):
                # Build a Gtk.PopoverMenu from the shared sidebar context menu helper.
                # Reset any batch-target snapshot from a previous menu.
                self._context_menu_connections = None
                menu = IconContextMenu()

                def _on_popover_closed(popover, *_):
                    # Only clear context state if this is still the active
                    # popover — a newer right-click may have already replaced
                    # it. Always unparent so popovers don't accumulate.
                    if getattr(self, '_context_menu_popover', None) is popover:
                        self._context_menu_popover = None
                        self._context_menu_row = None
                        self._context_menu_connection = None
                        self._context_menu_connections = None
                    try:
                        popover.unparent()
                    except Exception:
                        pass

                if getattr(row, 'is_tag_group', False):
                    # Virtual tag groups: rename the tag or open members in
                    # split view — no edit/delete/run (nothing to mutate).
                    # The Untagged section is not a real tag: no rename.
                    untagged = bool(getattr(row, 'group_info', {}).get('untagged'))
                    menu.add_section(
                        None if untagged else menu.add_item('document-edit-symbolic', _('Rename Tag…'), lambda: self.on_rename_tag_action(row)),
                        menu.add_item('view-grid-symbolic', _('Open in Split View'), lambda: self._open_tag_group_split(row)),
                    )
                elif hasattr(row, 'group_id'):
                    menu.add_section(
                        menu.add_item('document-edit-symbolic', _('Edit Group'), lambda: self.on_edit_group_action(None, None)),
                        menu.add_item('view-grid-symbolic', _('Open in Split View'), lambda: self.on_open_group_in_split_view_action(None, None)),
                        menu.add_item('utilities-terminal-symbolic', _('Run Command…'), lambda: self.on_run_command_action()),
                        menu.add_item('user-trash-symbolic', _('Delete Group'), lambda: self.on_delete_group_action(None, None)),
                    )
                else:
                    conn = getattr(row, 'connection', None)
                    # The right-click gesture has already collapsed the
                    # selection to the clicked row unless it was part of a
                    # multi-selection, so the selection reflects intent here.
                    # Dedupe by connection: the same connection may be selected
                    # through several rows (real group + tag group).
                    try:
                        selected_conns = self._connections_from_rows(
                            self._get_selected_connection_rows()
                        )
                    except Exception:
                        selected_conns = [conn] if conn else []
                    multi = len(selected_conns) > 1
                    # Snapshot the targets for the lifetime of this menu so
                    # batch actions operate on exactly what was selected when
                    # the menu opened, even if the selection changes before
                    # the item callback runs.
                    self._context_menu_connections = list(selected_conns) if multi else None

                    # Protocol capabilities decide which per-host actions make
                    # sense (all-capable for SSH, narrower for plugin protocols).
                    conn_caps = capabilities_for(conn) if conn else frozenset()
                    all_remote_command = bool(selected_conns) and all(
                        Capability.REMOTE_COMMAND in capabilities_for(c)
                        for c in selected_conns
                    )

                    if multi:
                        # Multi-selection: only actions that operate on all
                        # selected connections; per-host dialogs are hidden.
                        menu.add_section(
                            menu.add_item('list-add-symbolic', _('Open New Connections'), lambda: self.on_open_new_connection_action(None, None)),
                            menu.add_item('view-grid-symbolic', _('Open in Split View'), lambda: self.on_open_in_split_view_action(None, None)),
                            menu.add_item('utilities-terminal-symbolic', _('Run Command on Hosts…'), lambda: self.on_run_command_action()) if all_remote_command else None,
                        )
                    else:
                        menu.add_section(
                            menu.add_item('list-add-symbolic', _('Open New Connection'), lambda: self.on_open_new_connection_action(None, None)),
                            menu.add_item('document-edit-symbolic', _('Edit Connection'), lambda: self.on_edit_connection_action(None, None)),
                            menu.add_item('view-grid-symbolic', _('Open in Split View'), lambda: self.on_open_in_split_view_action(None, None)),
                            menu.add_item('utilities-terminal-symbolic', _('Run Command on Host…'), lambda: self.on_run_command_action()) if Capability.REMOTE_COMMAND in conn_caps else None,
                            menu.add_item('edit-copy-symbolic', _('Duplicate Connection'), lambda: self.on_duplicate_connection_action(None, None)),
                            menu.add_item('edit-copy-symbolic', _('Copy Address'), lambda: self._copy_connection_address()),
                        )

                    def _has_wol_mac(c):
                        try:
                            meta = self.config.get_connection_meta(c.nickname) if c else {}
                            return bool((meta or {}).get('wol_mac', '').strip())
                        except Exception:
                            return False

                    wol_item = None
                    if any(_has_wol_mac(c) for c in (selected_conns or [conn])):
                        wol_item = menu.add_item('network-wireless-symbolic', _('Wake on LAN'), lambda: self.on_wake_on_lan_action(None, None))
                    if multi:
                        menu.add_section(wol_item)
                    else:
                        menu.add_section(
                            menu.add_item('folder-symbolic', _('Manage Files'), lambda: self.on_manage_files_action(None, None)) if (Capability.FILE_TRANSFER in conn_caps and not should_hide_file_manager_options()) else None,
                            menu.add_item('dialog-password-symbolic', _('Copy Key to Server'), lambda: self.on_copy_key_to_server_action(None, None)) if Capability.KEY_DEPLOYMENT in conn_caps else None,
                            menu.add_item('dialog-password-symbolic', _('Manage authorized_keys…'), lambda: self.on_manage_authorized_keys_action(None, None)) if Capability.KEY_DEPLOYMENT in conn_caps else None,
                            wol_item,
                            # System terminal rides build_native_command(), an SSH-only path.
                            menu.add_item('utilities-terminal-symbolic', _('Open in System Terminal'), lambda: self.on_open_in_system_terminal_action(None, None)) if (getattr(conn, 'protocol', 'ssh') == 'ssh' and not should_hide_external_terminal_options()) else None,
                        )

                    def _conn_groups(c):
                        try:
                            return self.group_manager.get_connection_groups(c.nickname) if c else []
                        except Exception:
                            return []

                    current_groups = _conn_groups(conn)
                    any_grouped = any(_conn_groups(c) for c in selected_conns) if multi else bool(current_groups)
                    row_group_id = getattr(row, '_group_id', None)
                    ungroup_label = _('Remove from Group') if (not multi and row_group_id and len(current_groups) > 1) else _('Ungroup')
                    menu.add_section(
                        menu.add_item('folder-symbolic', _('Move to Group'), lambda: self.on_move_to_group_action(None, None)),
                        menu.add_item('list-add-symbolic', _('Copy to Group'), lambda: self.on_copy_to_group_action(None, None)),
                        menu.add_item('edit-undo-symbolic', ungroup_label, lambda: self.on_move_to_ungrouped_action(None, None)) if any_grouped else None,
                    )

                    try:
                        pin_targets = selected_conns if multi else ([conn] if conn else [])
                        all_pinned = bool(pin_targets) and all(
                            self.config.is_pinned(c.nickname) for c in pin_targets
                        )
                        if all_pinned:
                            menu.add_section(
                                menu.add_item('starred-symbolic', _('Unpin from Start Page'), lambda: self._toggle_pin_connections(pin_targets)),
                            )
                        else:
                            menu.add_section(
                                menu.add_item('non-starred-symbolic', _('Pin to Start Page'), lambda: self._toggle_pin_connections(pin_targets)),
                            )
                    except Exception:
                        pass

                    # Plugin-contributed connection actions (single SSH host),
                    # e.g. "Docker Console". The menu is rebuilt per right-click,
                    # so actions registered at activate time appear here.
                    try:
                        if not multi and conn and getattr(conn, 'protocol', 'ssh') == 'ssh':
                            ph = getattr(self, 'plugin_host', None)
                            actions = ph.ui.connection_actions() if ph is not None else []
                            if actions:
                                nick = getattr(conn, 'nickname', '')
                                menu.add_section(*[
                                    menu.add_item(
                                        a.icon_name or 'application-x-executable-symbolic',
                                        a.label,
                                        lambda cb=a.callback, nk=nick: cb(nk),
                                    )
                                    for a in actions
                                ])
                    except Exception:
                        logger.debug("Failed to add plugin connection actions", exc_info=True)

                    menu.add_section(
                        menu.add_item('user-trash-symbolic', _('Delete'), lambda: self.on_delete_connection_action(None, None)),
                    )

                popover = menu.show(row, on_closed=_on_popover_closed)
                self._context_menu_popover = popover

                # Disable the autohide modal grab. An autohide popover grabs all
                # input while open, so the next right-click on another row is
                # swallowed to dismiss this popover and never reaches our gesture
                # (a "dead click"). Without the grab, every right-click reaches the
                # handler, which closes this menu and opens the next one in a
                # single click. We handle dismissal ourselves (see below).
                try:
                    popover.set_autohide(False)
                except Exception:
                    pass

                # Escape closes the menu (autohide normally provides this).
                try:
                    key_ctrl = Gtk.EventControllerKey()

                    def _on_menu_key(_c, keyval, _code, _state):
                        if keyval == Gdk.KEY_Escape:
                            popover.popdown()
                            return True
                        return False

                    key_ctrl.connect('key-pressed', _on_menu_key)
                    popover.add_controller(key_ctrl)
                except Exception:
                    pass

            def _on_right_click(gesture, n_press, x, y):
                try:
                    logger.debug("Simple right-click detected - showing context menu for selected row")

                    # Try to detect the clicked row, but fall back to selected row if detection fails
                    row = self._pick_connection_list_row(x, y)
                    if row is not None:
                        logger.debug("Using clicked row for context menu")

                    # Fallback to selected row if click detection failed
                    if not row:
                        try:
                            row = self.connection_list.get_selected_row()
                            if row:
                                logger.debug("Using currently selected row for context menu (fallback)")
                            else:
                                # If no selection, use first row
                                first_visible = self.connection_list.get_row_at_index(0)
                                if first_visible:
                                    row = first_visible
                                    logger.debug("Using first row for context menu (no selection)")
                        except Exception as e:
                            logger.debug(f"Failed to get selected row: {e}")

                    if not row:
                        logger.debug("No row available for context menu")
                        return

                    # Claim the event sequence so the right-click stops here and
                    # is not also processed by the ListBox's own handling.
                    try:
                        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                    except Exception:
                        pass

                    # Dismiss and detach any context menu still open from a
                    # previous right-click before showing the new one. The menu is
                    # non-autohide, so this right-click reaches us instead of being
                    # swallowed to close the old popover, letting us replace it in a
                    # single click.
                    prev_popover = getattr(self, '_context_menu_popover', None)
                    if prev_popover is not None:
                        self._context_menu_popover = None
                        try:
                            prev_popover.popdown()
                        except Exception:
                            pass
                        try:
                            if prev_popover.get_parent() is not None:
                                prev_popover.unparent()
                        except Exception:
                            pass

                    # Highlight the right-clicked row so the UI reflects which
                    # connection the context menu applies to. Mirror standard
                    # file-manager behavior: right-clicking a row that isn't part
                    # of the current selection selects just that row; right-
                    # clicking within an existing multi-selection preserves it.
                    try:
                        already_selected = row in self.connection_list.get_selected_rows()
                    except Exception:
                        already_selected = False
                    if not already_selected:
                        self._select_only_row(row)

                    # Move the keyboard focus (focus ring) to the right-clicked row
                    # too, otherwise it stays on a previously keyboard-focused row.
                    try:
                        row.grab_focus()
                    except Exception:
                        pass

                    # Set context menu data
                    self._context_menu_row = row
                    self._context_menu_connection = getattr(row, 'connection', None)
                    # Safe for tag rows too: the only consumer that acts on a
                    # synthetic tag id is split view (which we want); edit/
                    # delete/run all bail on groups.get('tag::…') -> None.
                    self._context_menu_group_row = row if hasattr(row, 'group_id') else None

                    _build_and_show_menu(row)

                except Exception as e:
                    logger.error(f"Failed to create context menu: {e}")
            
            context_click.connect('pressed', _on_right_click)
            self.connection_list.add_controller(context_click)

            # Because the context menu is non-autohide (see _build_and_show_menu),
            # it no longer dismisses itself when the user clicks away. Close it on
            # any primary/middle press elsewhere in the window so it behaves like a
            # normal context menu. Presses on the popover's own menu items land on
            # a separate surface and do not reach this controller, so selecting an
            # item still works. Runs in the capture phase but never claims the
            # event, so normal click handling proceeds.
            def _dismiss_context_menu_on_press(gesture, n_press, x, y):
                pop = getattr(self, '_context_menu_popover', None)
                if pop is None:
                    return
                self._context_menu_popover = None
                try:
                    pop.popdown()
                except Exception:
                    pass
                try:
                    if pop.get_parent() is not None:
                        pop.unparent()
                except Exception:
                    pass

            dismiss_click = Gtk.GestureClick()
            dismiss_click.set_button(0)  # any button
            dismiss_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

            def _on_dismiss_press(gesture, n_press, x, y):
                # The right-click handler manages replacing the menu itself; only
                # dismiss here for non-secondary buttons.
                try:
                    if gesture.get_current_button() == Gdk.BUTTON_SECONDARY:
                        return
                except Exception:
                    pass
                _dismiss_context_menu_on_press(gesture, n_press, x, y)

            dismiss_click.connect('pressed', _on_dismiss_press)
            self.add_controller(dismiss_click)

            middle_click = Gtk.GestureClick()
            middle_click.set_button(Gdk.BUTTON_MIDDLE)

            def _on_middle_click(gesture, n_press, x, y):
                if n_press != 1:
                    return


                row = self._pick_connection_list_row(x, y)

                if not row:
                    try:
                        row = self.connection_list.get_selected_row()
                    except Exception:
                        row = None

                if not row or not hasattr(row, 'connection'):
                    return

                previous_row = getattr(self, '_context_menu_row', None)
                previous_connection = getattr(self, '_context_menu_connection', None)
                previous_connections = getattr(self, '_context_menu_connections', None)

                try:
                    self._context_menu_row = row
                    self._context_menu_connection = row.connection
                    # Middle-click targets only the clicked row; a multi-select
                    # snapshot from a still-open context menu must not win.
                    self._context_menu_connections = None
                    self.on_open_new_connection_action(None, None)
                    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                finally:
                    self._context_menu_row = previous_row
                    self._context_menu_connection = previous_connection
                    self._context_menu_connections = previous_connections

            middle_click.connect('pressed', _on_middle_click)
            self.connection_list.add_controller(middle_click)
        except Exception:
            pass
        
        # Add keyboard controller for Ctrl/⌘+Enter to open new connection
        try:
            key_controller = Gtk.ShortcutController()
            key_controller.set_scope(Gtk.ShortcutScope.LOCAL)
            
            def _on_ctrl_enter(widget, *args):
                try:
                    self._open_new_connection_tabs(
                        self._connections_from_rows(
                            self._get_selected_connection_rows()
                        )
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to open new connection with {get_primary_modifier_label()}+Enter: {e}"
                    )
                return True
            
            trigger = '<Meta>Return' if is_macos() else '<Primary>Return'
            
            key_controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(trigger),
                Gtk.CallbackAction.new(_on_ctrl_enter)
            ))
            
            self.connection_list.add_controller(key_controller)
        except Exception as e:
            logger.debug(
                f"Failed to add {get_primary_modifier_label()}+Enter shortcut: {e}"
            )
        
        self.connection_scrolled.set_child(self.connection_list)
        sidebar_box.append(self.connection_scrolled)
        
        # Sidebar toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_hexpand(True)
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
        
        # Import icon_utils for toolbar buttons
        from sshpilot import icon_utils
        
        # Connection toolbar buttons
        self.connection_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.connection_toolbar.set_hexpand(True)
        self.connection_toolbar.set_homogeneous(True)
        
        # Edit button
        self.edit_button = icon_utils.new_button_from_icon_name('document-edit-symbolic')
        self.edit_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(self.edit_button)
        self.edit_button.set_tooltip_text('Edit Connection')
        self.edit_button.set_sensitive(False)
        self.edit_button.connect('clicked', self.on_edit_connection_clicked)
        self.connection_toolbar.append(self.edit_button)

        # Copy key to server button (ssh-copy-id)
        self.copy_key_button = icon_utils.new_button_from_icon_name('dialog-password-symbolic')
        self.copy_key_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(self.copy_key_button)
        self.copy_key_button.set_tooltip_text(
            f'Copy public key to server for passwordless login ({get_primary_modifier_label()}+Shift+K)'
        )
        self.copy_key_button.set_sensitive(False)
        self.copy_key_button.connect('clicked', self.on_copy_key_to_server_clicked)
        self.connection_toolbar.append(self.copy_key_button)

        # SCP transfer button
        self.scp_button = icon_utils.new_button_from_icon_name('vertical-arrows-long-symbolic')
        self.scp_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(self.scp_button)
        self.scp_button.set_tooltip_text('Transfer files with scp')
        self.scp_button.set_sensitive(False)
        self.scp_button.connect('clicked', self.on_scp_button_clicked)
        self.connection_toolbar.append(self.scp_button)

        # Manage files button (visibility controlled dynamically)
        self.manage_files_button = icon_utils.new_button_from_icon_name('folder-symbolic')
        self.manage_files_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(self.manage_files_button)
        primary_label = get_primary_modifier_label()
        self.manage_files_button.set_tooltip_text(
            f"Open file manager for remote server ({primary_label}+Shift+O)"
        )
        self.manage_files_button.set_sensitive(False)
        self.manage_files_button.connect('clicked', self.on_manage_files_button_clicked)
        self.manage_files_button.set_visible(not should_hide_file_manager_options())
        self.connection_toolbar.append(self.manage_files_button)
        
        # System terminal button (only when external terminals are available)
        if not should_hide_external_terminal_options():
            self.system_terminal_button = icon_utils.new_button_from_icon_name('utilities-terminal-symbolic')
            self.system_terminal_button.add_css_class('flat')
            self._expand_sidebar_toolbar_button(self.system_terminal_button)
            self.system_terminal_button.set_tooltip_text('Open connection in system terminal')
            self.system_terminal_button.set_sensitive(False)
            self.system_terminal_button.connect('clicked', self.on_system_terminal_button_clicked)
            self.connection_toolbar.append(self.system_terminal_button)
        
        # Delete button
        self.delete_button = icon_utils.new_button_from_icon_name('user-trash-symbolic')
        self.delete_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(self.delete_button)
        self.delete_button.set_tooltip_text('Delete Connection')
        self.delete_button.set_sensitive(False)
        self.delete_button.connect('clicked', self.on_delete_connection_clicked)
        self.connection_toolbar.append(self.delete_button)
        
        # Group toolbar buttons
        self.group_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.group_toolbar.set_hexpand(True)
        self.group_toolbar.set_homogeneous(True)
        
        # Rename group button
        self.rename_group_button = icon_utils.new_button_from_icon_name('document-edit-symbolic')
        self.rename_group_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(self.rename_group_button)
        self.rename_group_button.set_tooltip_text('Rename Group')
        self.rename_group_button.set_sensitive(False)
        self.rename_group_button.connect('clicked', self.on_rename_group_clicked)
        self.group_toolbar.append(self.rename_group_button)
        
        # Delete group button
        self.delete_group_button = icon_utils.new_button_from_icon_name('user-trash-symbolic')
        self.delete_group_button.add_css_class('flat')
        self._expand_sidebar_toolbar_button(self.delete_group_button)
        self.delete_group_button.set_tooltip_text('Delete Group')
        self.delete_group_button.set_sensitive(False)
        self.delete_group_button.connect('clicked', self.on_delete_group_clicked)
        self.group_toolbar.append(self.delete_group_button)
        
        # Add both toolbars to main toolbar
        toolbar.append(self.connection_toolbar)
        toolbar.append(self.group_toolbar)
        
        sidebar_box.append(toolbar)

        # Sidebar header: title + window controls (GNOME split-view pattern)
        self.sidebar_header_bar = Adw.HeaderBar()
        self.sidebar_header_bar.add_css_class('flat')
        if HAS_NAV_SPLIT or HAS_OVERLAY_SPLIT:
            self.sidebar_header_bar.set_show_start_title_buttons(True)
            self.sidebar_header_bar.set_show_end_title_buttons(True)

        sidebar_title_label = Gtk.Label(label='SSH Pilot')
        sidebar_title_label.add_css_class('title')
        sidebar_title_label.set_xalign(0.0)
        self.sidebar_header_bar.set_title_widget(sidebar_title_label)

        sidebar_toolbar_view = Adw.ToolbarView()
        sidebar_toolbar_view.add_css_class('sidebar')
        sidebar_toolbar_view.add_top_bar(self.sidebar_header_bar)
        sidebar_toolbar_view.set_content(sidebar_box)

        self._set_sidebar_widget(sidebar_toolbar_view)
        logger.debug("Set sidebar widget")

    def _copy_connection_address(self):
        conn = getattr(self, '_context_menu_connection', None)
        if conn:
            host = getattr(conn, 'hostname', '') or getattr(conn, 'host', '')
            if host:
                self.get_clipboard().set(host)

    def _toggle_pin_connection(self, conn):
        """Pin or unpin a connection from the start page."""
        if conn is None:
            return
        self._toggle_pin_connections([conn])

    def _toggle_pin_connections(self, conns):
        """Pin or unpin a batch of connections from the start page.

        If every connection is already pinned, all are unpinned; otherwise
        all are pinned (the same aggregate rule the context menu label uses).
        """
        conns = [c for c in (conns or []) if c is not None]
        if not conns:
            return
        try:
            all_pinned = all(self.config.is_pinned(c.nickname) for c in conns)
            for c in conns:
                if all_pinned:
                    self.config.unpin_connection(c.nickname)
                else:
                    self.config.pin_connection(c.nickname)
            if len(conns) == 1:
                msg = _("Unpinned from start page") if all_pinned else _("Pinned to start page")
            elif all_pinned:
                msg = _("Unpinned {n} connections from start page").format(n=len(conns))
            else:
                msg = _("Pinned {n} connections to start page").format(n=len(conns))
            if hasattr(self, 'toast_overlay') and self.toast_overlay:
                self.toast_overlay.add_toast(Adw.Toast.new(msg))
            if hasattr(self, 'welcome_view') and self.welcome_view:
                self.welcome_view.refresh_pinned()
        except Exception as e:
            logger.error(f"Failed to toggle pin for {len(conns)} connection(s): {e}")

    def _pick_connection_list_row(
        self, x: float, y: float
    ) -> Optional[Gtk.ListBoxRow]:
        """Return the ListBoxRow under a pointer event on the connection list.

        The coordinates come from a gesture attached to ``connection_list``
        itself, so they are already in the ListBox's content space. ``pick()``
        resolves the row directly with no scroll adjustment, which is why both
        the right-click and middle-click handlers must share this path: any
        manual vadjustment math would double-count the scroll offset and select
        a row further down the list (see issue #1013).
        """
        try:
            widget = self.connection_list.pick(x, y, Gtk.PickFlags.DEFAULT)
        except Exception as e:
            logger.debug(f"Failed to pick connection list row: {e}")
            return None

        while widget is not None:
            if isinstance(widget, Gtk.ListBoxRow):
                return widget
            if widget == self.connection_list:
                break
            widget = widget.get_parent()

        return None

    # ------------------------------------------------------------------
    # Connection sorting helpers
    # ------------------------------------------------------------------

    def _apply_app_theme(self, theme: str) -> None:
        """Apply application light/dark/system theme and persist app-theme."""
        theme_key = str(theme).lower()
        if theme_key not in {'default', 'light', 'dark'}:
            theme_key = 'default'

        style_manager = Adw.StyleManager.get_default()
        if theme_key == 'light':
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        elif theme_key == 'dark':
            style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        else:
            style_manager.set_color_scheme(Adw.ColorScheme.DEFAULT)

        self.config.set_setting('app-theme', theme_key)
        self._sync_theme_menu_button()

    def _create_theme_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        menu.append(_('Follow System'), 'win.set-app-theme::default')
        menu.append(_('Light'), 'win.set-app-theme::light')
        menu.append(_('Dark'), 'win.set-app-theme::dark')
        return menu

    def _sync_theme_menu_button(self) -> None:
        btn = getattr(self, '_headerbar_theme_menu_button', None)
        if btn is None:
            return

        labels = {
            'default': _('Follow System'),
            'light': _('Light'),
            'dark': _('Dark'),
        }
        saved = str(self.config.get_setting('app-theme', 'default'))
        current = labels.get(saved, labels['default'])
        btn.set_tooltip_text(_('Application theme: {theme}').format(theme=current))

    def _build_sort_button(self):
        from sshpilot import icon_utils
        button = icon_utils.new_button_from_icon_name("view-sort-ascending-symbolic")
        button.add_css_class('flat')
        button.set_can_focus(False)
        button.connect("clicked", self._on_sort_button_clicked)
        self.sort_button = button
        self._update_sort_button()
        return button

    def _build_preferences_button(self):
        from sshpilot import icon_utils
        button = icon_utils.new_button_from_icon_name("org.gnome.Settings-system-symbolic")
        button.add_css_class('flat')
        button.set_can_focus(False)
        button.set_tooltip_text(_("Settings"))
        button.connect("clicked", lambda *_: self.show_preferences())
        return button

    def _next_sort_preset_id(self, current_id: str) -> str:
        if current_id == "name-desc":
            return "name-asc"
        return "name-desc"

    def _update_sort_button(self):
        if not self.sort_button:
            return

        from sshpilot import icon_utils
        preset_id = self._connection_sort_last or DEFAULT_CONNECTION_SORT
        preset = CONNECTION_SORT_PRESETS.get(preset_id, CONNECTION_SORT_PRESETS[DEFAULT_CONNECTION_SORT])
        icon_utils.set_button_icon(self.sort_button, preset.icon_name)

        next_preset_id = self._next_sort_preset_id(preset_id)
        next_preset = CONNECTION_SORT_PRESETS.get(next_preset_id)
        if next_preset:
            tooltip = _("Sort {current} — click for {next}").format(
                current=preset.title, next=next_preset.title
            )
        else:
            tooltip = _("Sort {title}").format(title=preset.title)

        try:
            self.sort_button.set_tooltip_text(tooltip)
        except Exception:
            pass

    def _on_sort_button_clicked(self, *_args):
        current = self._connection_sort_last or DEFAULT_CONNECTION_SORT
        next_preset = self._next_sort_preset_id(current)
        self.apply_connection_sort_preset(next_preset)

    def apply_connection_sort_preset(self, preset_id: str):
        preset = CONNECTION_SORT_PRESETS.get(preset_id)
        if not preset:
            preset_id = DEFAULT_CONNECTION_SORT
            preset = CONNECTION_SORT_PRESETS[preset_id]

        changed = apply_sort_to_manager(
            self.group_manager,
            self.connection_manager.get_connections(),
            preset_id,
        )

        self._connection_sort_last = preset_id
        try:
            self.config.set_setting('ui.connection_sort_last', preset_id)
        except Exception:
            pass

        self._update_sort_button()
        if changed:
            self.rebuild_connection_list()

        self._notify_sort_result(preset, changed)

    def _notify_sort_result(self, preset, changed: bool):
        toast_overlay = getattr(self, "toast_overlay", None)
        if not toast_overlay:
            return

        if changed:
            message = _("Connections sorted — {title}").format(title=preset.title)
        else:
            message = _("Already sorted as {title}").format(title=preset.title)

        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        toast_overlay.add_toast(toast)

    def setup_content_area(self):
        """Set up the main content area with tab overview and pinned Start tab."""
        # Create tab view
        self.tab_view = Adw.TabView()
        self.tab_view.set_hexpand(True)
        self.tab_view.set_vexpand(True)

        # Disable Adw.TabView's built-in Alt+1..9 / Alt+0 tab-selection shortcuts.
        # They otherwise shadow split-view "Alt+N focus pane" whenever more than
        # one tab is open (the tab_view is an ancestor of the split-view tab, so
        # its handler runs first). Tab switching is via Ctrl+PageUp/Down.
        try:
            self.tab_view.set_shortcuts(
                self.tab_view.get_shortcuts()
                & ~Adw.TabViewShortcuts.ALT_DIGITS
                & ~Adw.TabViewShortcuts.ALT_ZERO
            )
        except Exception:
            logger.debug("Could not adjust Adw.TabView shortcuts", exc_info=True)

        # Provide widget-scoped Alt+Arrow navigation helpers for tab-specific focus
        try:
            tab_nav = Gtk.ShortcutController()
            tab_nav.set_scope(Gtk.ShortcutScope.LOCAL)

            def _on_tab_step(step: int):
                def _handler(widget, *args):
                    try:
                        self._select_tab_relative(step)
                    except Exception:
                        pass
                    return True

                return _handler

            tab_nav.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Alt>Right'),
                Gtk.CallbackAction.new(_on_tab_step(1))
            ))
            tab_nav.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string('<Alt>Left'),
                Gtk.CallbackAction.new(_on_tab_step(-1))
            ))

            self.tab_view.add_controller(tab_nav)
        except Exception:
            pass

        # Connect tab signals
        self.tab_view.connect('close-page', self.on_tab_close)
        self.tab_view.connect('page-attached', self.on_tab_attached)
        self.tab_view.connect('page-detached', self.on_tab_detached)
        # Track selected tab to keep row selection in sync
        self.tab_view.connect('notify::selected-page', self.on_tab_selected)

        # Context-aware right-click tab menu (official AdwTabView mechanism:
        # set_menu_model + setup-menu signal). A single menu model holds every
        # item; the setup-menu handler enables only the actions relevant to the
        # right-clicked tab type, and each item is hidden when its action is
        # disabled (hidden-when="action-disabled"). The model/popover is built
        # once and cached by AdwTabBox, so per-tab differences must come from
        # action state, not from swapping the model.
        self._tab_menu_page = None
        self._tab_menu_xy = None
        self._build_tab_context_menus()
        self.tab_view.set_menu_model(self._tab_menu_model)
        self.tab_view.connect('setup-menu', self._on_tab_setup_menu)

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
        
        # Create tab overview
        self.tab_overview = Adw.TabOverview()
        self.tab_overview.set_view(self.tab_view)
        self.tab_overview.set_enable_new_tab(False)
        self.tab_overview.set_enable_search(True)
        # Hide window buttons in tab overview
        self.tab_overview.set_show_start_title_buttons(False)
        self.tab_overview.set_show_end_title_buttons(False)
        
        # Create tab bar
        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.tab_view)
        self.tab_bar.set_autohide(False)
        self.tab_bar.set_expand_tabs(False)
        # Blend with the flat header bar above it (no headerbar fill / seam).
        self.tab_bar.add_css_class('inline')

        # H/V layout toggles after the last tab (tab bar end action)
        from .split_view import create_layout_toggle_buttons

        self._layout_h_btn, self._layout_v_btn, self._layout_toggle_updating = (
            create_layout_toggle_buttons(
                lambda: self._apply_tab_layout_mode('horizontal'),
                lambda: self._apply_tab_layout_mode('vertical'),
            )
        )
        self._layout_h_btn.set_visible(False)
        self._layout_v_btn.set_visible(False)

        self.tab_button = Adw.TabButton()
        self.tab_button.set_view(self.tab_view)
        self.tab_button.connect('clicked', self.on_tab_button_clicked)
        self.tab_button.set_visible(False)  # Hidden by default, shown when tabs exist

        layout_end_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        layout_end_box.append(self._layout_h_btn)
        layout_end_box.append(self._layout_v_btn)
        layout_end_box.append(self.tab_button)
        self.tab_bar.set_end_action_widget(layout_end_box)

        # Double-click on a tab to rename it inline
        rename_gesture = Gtk.GestureClick()
        rename_gesture.set_button(1)
        rename_gesture.connect('pressed', self._on_tab_bar_pressed)
        self.tab_bar.add_controller(rename_gesture)

        # Suppress the context menu on the pinned Start tab. This capture-phase
        # gesture runs before AdwTabBox's own (bubble-phase) right-click handler,
        # so we can clear the view's menu model just-in-time when the click lands
        # on the Start tab — AdwTabBox.do_popup early-returns on a NULL model.
        menu_guard = Gtk.GestureClick()
        menu_guard.set_button(Gdk.BUTTON_SECONDARY)
        menu_guard.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        menu_guard.connect('pressed', self._on_tab_bar_secondary_press)
        self.tab_bar.add_controller(menu_guard)

        # Create tab content box
        self.tab_content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.tab_content_box.append(self.tab_bar)
        self.tab_content_box.append(self.tab_view)
        if hasattr(self.tab_view, 'add_css_class'):
            self.tab_view.add_css_class('terminal-bg')
        
        # Set the tab content box as the child of the tab overview
        self.tab_overview.set_child(self.tab_content_box)

        self._create_start_tab()
        self._update_tab_button_visibility()
        
        # Split-view button — opens a new split-view tab
        from sshpilot import icon_utils as _iu
        self.split_view_button = Gtk.Button()
        _iu.set_button_icon(self.split_view_button, 'view-grid-symbolic')
        self.split_view_button.set_tooltip_text(_('New Split View'))
        self.split_view_button.add_css_class('flat')
        self.split_view_button.connect('clicked', self.on_open_split_view_clicked)
        self.header_bar.pack_start(self.split_view_button)

        # Command blocks toggle button (right sidebar)
        from sshpilot import icon_utils as _cmd_icon_utils

        self._headerbar_theme_menu_button = Gtk.MenuButton()
        _cmd_icon_utils.set_button_icon(self._headerbar_theme_menu_button, 'dark-mode-symbolic')
        self._headerbar_theme_menu_button.add_css_class('flat')
        self._headerbar_theme_menu_button.set_tooltip_text(_('Application theme'))
        self._headerbar_theme_menu_button.set_menu_model(self._create_theme_menu())

        self._cmd_blocks_toggle_btn = Gtk.ToggleButton()
        _cmd_icon_utils.set_button_icon(self._cmd_blocks_toggle_btn, 'system-run-symbolic')
        self._cmd_blocks_toggle_btn.add_css_class('flat')
        self._cmd_blocks_toggle_btn.set_tooltip_text(_('Commands'))
        self._updating_cmd_toggle = False

        def _on_cmd_toggle_btn_toggled(btn):
            if self._updating_cmd_toggle:
                return
            self._toggle_command_blocks_panel(btn.get_active())

        self._cmd_blocks_toggle_btn.connect('toggled', _on_cmd_toggle_btn_toggled)
        self.header_bar.pack_end(self._cmd_blocks_toggle_btn)
        self.header_bar.pack_end(self._headerbar_theme_menu_button)
        self.header_bar.pack_end(self.menu_button)
        self._sync_theme_menu_button()

        # Create broadcast command banner (custom banner-like widget)
        self.broadcast_banner = Gtk.Revealer()
        self.broadcast_banner.set_reveal_child(False)
        self.broadcast_banner.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.broadcast_hide_timeout_id: Optional[int] = None
        self.broadcast_entry_dirty = False
        self._suppress_broadcast_entry_changed = False
        
        # Create banner content box
        banner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        banner_box.add_css_class('banner')
        banner_box.set_can_focus(True)
        banner_box.set_focusable(True)
        
        # Create banner header with title and send button
        banner_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        banner_header.set_margin_start(12)
        banner_header.set_margin_end(12)
        banner_header.set_margin_top(8)
        banner_header.set_margin_bottom(4)
        
        # Banner title
        banner_title = Gtk.Label(label=_("Broadcast Command"))
        banner_title.set_xalign(0)
        banner_title.add_css_class('title-4')
        banner_header.append(banner_title)
        
        # Send button
        self.broadcast_send_button = Gtk.Button()
        self.broadcast_send_button.set_label(_("Send"))
        self.broadcast_send_button.add_css_class('suggested-action')
        self.broadcast_send_button.connect('clicked', self.on_broadcast_send_clicked)
        banner_header.append(self.broadcast_send_button)
        
        banner_box.append(banner_header)
        
        # Create banner content with entry and cancel button
        banner_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        banner_content.set_margin_start(12)
        banner_content.set_margin_end(12)
        banner_content.set_margin_bottom(8)
        
        # Create command entry
        self.broadcast_entry = Gtk.Entry()
        self.broadcast_entry.set_placeholder_text(_("e.g., ls -la"))
        self.broadcast_entry.set_hexpand(True)
        self.broadcast_entry.connect('activate', self.on_broadcast_entry_activate)
        self.broadcast_entry.connect('changed', self.on_broadcast_entry_changed)

        # Add ESC key handling to the entry
        entry_controller = Gtk.EventControllerKey()
        entry_controller.connect('key-pressed', self.on_broadcast_entry_key_pressed)
        self.broadcast_entry.add_controller(entry_controller)

        focus_controller = Gtk.EventControllerFocus()
        focus_controller.connect('enter', self.on_broadcast_entry_focus_enter)
        focus_controller.connect('leave', self.on_broadcast_entry_focus_leave)
        self.broadcast_entry.add_controller(focus_controller)

        banner_content.append(self.broadcast_entry)
        
        # Create cancel button
        self.broadcast_cancel_button = Gtk.Button()
        self.broadcast_cancel_button.set_label(_("Cancel"))
        self.broadcast_cancel_button.connect('clicked', self.on_broadcast_cancel_clicked)
        banner_content.append(self.broadcast_cancel_button)
        
        banner_box.append(banner_content)
        
        # Set the banner box as the revealer's child
        self.broadcast_banner.set_child(banner_box)
        
        # Add global ESC key handling to the entire banner
        banner_controller = Gtk.EventControllerKey()
        banner_controller.connect('key-pressed', self.on_broadcast_banner_key_pressed)
        banner_box.add_controller(banner_controller)

        if HAS_OVERLAY_SPLIT:
            content_box = Adw.ToolbarView()
            content_box.add_top_bar(self.header_bar)
            # Create content wrapper with banner below header bar
            content_wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content_wrapper.append(self.update_banner_container)
            content_wrapper.append(self.tips_banner_container)
            content_wrapper.append(self.broadcast_banner)
            content_wrapper.append(self.tab_overview)
            # Wrap only the content area (below the header bar) so the command
            # blocks sidebar opens inside the terminal pane, not across the full window.
            self._wrap_content_with_command_panel(content_wrapper, set_as_window_content=False)
            content_box.set_content(
                self.cmd_split_view if self.cmd_split_view is not None else content_wrapper
            )
            main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            main_box.append(content_box)
            self._set_content_widget(main_box)
            logger.debug("Set content widget for OverlaySplitView")
        elif HAS_NAV_SPLIT:
            content_box = Adw.ToolbarView()
            content_box.add_top_bar(self.header_bar)
            # Create content wrapper with banner below header bar
            content_wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content_wrapper.append(self.update_banner_container)
            content_wrapper.append(self.tips_banner_container)
            content_wrapper.append(self.broadcast_banner)
            content_wrapper.append(self.tab_overview)
            # Same: scope the sidebar to the content pane only.
            self._wrap_content_with_command_panel(content_wrapper, set_as_window_content=False)
            content_box.set_content(
                self.cmd_split_view if self.cmd_split_view is not None else content_wrapper
            )
            main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            main_box.append(content_box)
            self._set_content_widget(main_box)
            logger.debug("Set content widget for NavigationSplitView")
        else:
            # For non-split views, create a vertical box to contain banners and content
            main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            main_box.append(self.update_banner_container)
            main_box.append(self.tips_banner_container)
            main_box.append(self.broadcast_banner)
            main_box.append(self.tab_overview)
            self._set_content_widget(main_box)
            logger.debug("Set content widget for other split view types")

    def _ensure_command_block_store(self) -> Optional[CommandBlockStore]:
        """Create the command-block store on first use."""
        if self.command_block_store is not None:
            return self.command_block_store
        app = self.get_application()
        config = getattr(app, 'config', None) if app else None
        if config is None:
            config = getattr(self, 'config', None)
        if config is None:
            return None
        from .command_blocks import CommandBlockStore
        self.command_block_store = CommandBlockStore(config)
        return self.command_block_store

    def _ensure_command_blocks_panel(self) -> Optional[CommandBlocksPanel]:
        """Build the command blocks sidebar on first open (or when always shown)."""
        if self.command_blocks_panel is not None:
            return self.command_blocks_panel
        if self.cmd_split_view is None:
            return None
        store = self._ensure_command_block_store()
        if store is None:
            return None
        from .command_blocks import CommandBlocksPanel
        self.command_blocks_panel = CommandBlocksPanel(self, store)
        self.cmd_split_view.set_sidebar(self.command_blocks_panel)
        logger.debug("Command blocks panel created")
        return self.command_blocks_panel

    def _wrap_content_with_command_panel(self, content_widget: Gtk.Widget, *, set_as_window_content: bool = True) -> None:
        """Build a right-side OverlaySplitView for the command blocks panel around content_widget.

        When set_as_window_content is True (default) the resulting split view is
        installed as the window content via _set_content_widget.  Pass False when
        the caller wants to place the split view itself (e.g. as the content of an
        inner ToolbarView so the sidebar sits next to the terminal only).
        """
        if not HAS_OVERLAY_SPLIT:
            if set_as_window_content:
                self._set_content_widget(content_widget)
            logger.warning("Command blocks panel requires Adw.OverlaySplitView; panel disabled")
            return
        try:
            app = self.get_application()
            config = getattr(app, 'config', None) if app else None
            if config is None:
                config = getattr(self, 'config', None)
            self.cmd_split_view = Adw.OverlaySplitView()
            self.cmd_split_view.set_sidebar_position(Gtk.PackType.END)
            _always_show = bool(config.get_setting('command_blocks.always_show_sidebar', False)) if config else False
            self.cmd_split_view.set_show_sidebar(_always_show)
            self._command_sidebar_visible = _always_show
            if _always_show and getattr(self, '_cmd_blocks_toggle_btn', None) is not None:
                self._updating_cmd_toggle = True
                self._cmd_blocks_toggle_btn.set_active(True)
                self._updating_cmd_toggle = False
            try:
                self.cmd_split_view.set_min_sidebar_width(240)
                self.cmd_split_view.set_max_sidebar_width(400)
                self.cmd_split_view.set_sidebar_width_fraction(0.28)
            except Exception:
                pass
            self.cmd_split_view.set_hexpand(True)
            self.cmd_split_view.set_vexpand(True)

            if _always_show:
                self._ensure_command_blocks_panel()

            self.cmd_split_view.set_content(content_widget)
            if set_as_window_content:
                self._set_content_widget(self.cmd_split_view)
            logger.debug("Command blocks split view created")
        except Exception as exc:
            logger.error("Failed to create command blocks panel: %s", exc)
            if set_as_window_content:
                self._set_content_widget(content_widget)

    def _toggle_command_blocks_panel(self, visible: bool | None = None) -> None:
        """Show or hide the command blocks right sidebar."""
        if self.cmd_split_view is None:
            return
        try:
            if visible is None:
                visible = not self.cmd_split_view.get_show_sidebar()
            if visible:
                self._ensure_command_blocks_panel()
            self.cmd_split_view.set_show_sidebar(visible)
            self._command_sidebar_visible = visible
            if self._cmd_blocks_toggle_btn is not None:
                self._updating_cmd_toggle = True
                self._cmd_blocks_toggle_btn.set_active(visible)
                self._updating_cmd_toggle = False
            if visible and self.command_blocks_panel is not None:
                GLib.idle_add(self.command_blocks_panel.focus_search)
            elif not visible:
                terminal = self._get_active_terminal_widget()
                if terminal is not None:
                    self._focus_terminal_widget(terminal)
        except Exception as exc:
            logger.debug("_toggle_command_blocks_panel: %s", exc)

    def create_menu(self):
        """Create application menu"""
        menu = Gio.Menu()

        new_section = Gio.Menu()
        new_section.append('New Connection', 'app.new-connection')
        new_section.append('Create Group', 'win.create-group')
        new_section.append('Local Terminal', 'app.local-terminal')
        menu.append_section(None, new_section)

        server_section = Gio.Menu()
        server_section.append('Copy Key to Server', 'app.new-key')
        server_section.append('Broadcast Command', 'app.broadcast-command')
        if not should_hide_file_manager_options():
            server_section.append('Manage Files', 'win.open-file-manager')
        menu.append_section(None, server_section)

        ssh_section = Gio.Menu()
        ssh_section.append('SSH Config Editor', 'app.edit-ssh-config')
        ssh_section.append('Known Hosts Editor', 'win.edit-known-hosts')
        ssh_section.append('Manage Local authorized_keys…', 'win.manage-local-authorized-keys')
        menu.append_section(None, ssh_section)

        submenu_section = Gio.Menu()

        sessions_menu = Gio.Menu()
        sessions_menu.append('Save Session…', 'win.save-session')
        sessions_menu.append('Open Session…', 'win.open-session')
        sessions_menu.append('Manage Sessions…', 'win.manage-sessions')
        submenu_section.append_submenu('Sessions', sessions_menu)

        import_export_menu = Gio.Menu()
        import_export_menu.append('Export Configuration', 'win.export-config')
        import_export_menu.append('Import Configuration', 'win.import-config')
        submenu_section.append_submenu('Import/Export', import_export_menu)

        # Plugin-contributed pages live in the Tools submenu. The section
        # object is shared/mutable, so items the plugin host appends after
        # this menu is built still appear.
        plugins_section = getattr(self, '_plugins_menu_section', None)
        if plugins_section is not None:
            tools_menu = Gio.Menu()
            tools_menu.append_section(None, plugins_section)
            submenu_section.append_submenu('Tools', tools_menu)

        menu.append_section(None, submenu_section)

        settings_section = Gio.Menu()
        settings_section.append('Settings', 'app.preferences')
        menu.append_section(None, settings_section)

        help_section = Gio.Menu()
        # Help submenu with platform-aware keyboard shortcuts overlay
        help_menu = Gio.Menu()
        help_menu.append('Keyboard Shortcuts', 'app.shortcuts')
        help_menu.append('Documentation', 'app.help')
        help_menu.append('Check for Updates', 'win.check-for-updates')
        help_menu.append('View Logs…', 'win.view-logs')
        help_menu.append('Report a Problem…', 'win.report-problem')
        help_menu.append('Export Diagnostics…', 'win.export-diagnostics')
        help_section.append_submenu('Help', help_menu)
        help_section.append('About', 'app.about')
        menu.append_section(None, help_section)

        quit_section = Gio.Menu()
        quit_section.append('Quit', 'app.quit')
        menu.append_section(None, quit_section)

        return menu

    def setup_connections(self):
        """Load and display existing connections with grouping"""
        self.rebuild_connection_list()
        
        # Select first connection if available
        connections = self.connection_manager.get_connections()
        if connections:
            first_row = self.connection_list.get_row_at_index(0)
            if first_row:
                self._select_only_row(first_row)
                # Defer focus to the list to ensure keyboard navigation works immediately
                GLib.idle_add(self._focus_connection_list_first_row)
    
    def rebuild_connection_list(self):
        """Rebuild the connection list with groups"""
        reset_connection_list_drag_session(self)

        # Save current scroll position
        scroll_position = None
        if hasattr(self, 'connection_scrolled') and self.connection_scrolled:
            vadj = self.connection_scrolled.get_vadjustment()
            if vadj:
                scroll_position = vadj.get_value()
        
        # Clear existing rows
        child = self.connection_list.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.connection_list.remove(child)
            child = next_child
        self.connection_rows.clear()
        
        # Get all connections
        connections = self.connection_manager.get_connections()
        # Attach tags so the search filter and freshly built rows see them.
        for conn in connections:
            try:
                conn.tags = self.config.get_connection_tags(conn.nickname)
            except Exception:
                conn.tags = []
        connections_dict = {conn.nickname: conn for conn in connections}
        search_text = ''
        if hasattr(self, 'search_entry') and self.search_entry:
            search_text = self.search_entry.get_text().strip().lower()

        tag_filter = getattr(self, '_tag_filter', None)
        if tag_filter:
            matches = [
                c for c in connections
                if tag_filter in {str(t).casefold() for t in (getattr(c, 'tags', None) or [])}
                and (not search_text or connection_matches(c, search_text))
            ]
            for conn in sorted(matches, key=lambda c: c.nickname.lower()):
                self.add_connection_row(conn)
            self._ungrouped_area_row = None
            # Restore scroll position
            if scroll_position is not None and hasattr(self, 'connection_scrolled') and self.connection_scrolled:
                vadj = self.connection_scrolled.get_vadjustment()
                if vadj:
                    GLib.idle_add(lambda: vadj.set_value(scroll_position))
            return

        if search_text:
            displayed_connections = set()

            matched_groups: List[Dict[str, Any]] = []
            try:
                for group_info in self.group_manager.get_all_groups():
                    group_name = group_info.get('name', '')
                    if search_text in group_name.lower():
                        group_id = group_info.get('id')
                        if group_id and group_id in getattr(self.group_manager, 'groups', {}):
                            matched_groups.append(
                                copy.deepcopy(self.group_manager.groups[group_id])
                            )
            except Exception as error:
                logger.error(f"Error gathering matching groups: {error}")
                matched_groups = []

            for group_info in matched_groups:
                group_row = GroupRow(group_info, self.group_manager, connections_dict)
                group_row.connect('group-toggled', self._on_group_toggled)
                self.connection_list.append(group_row)

                for conn_nickname in group_info.get('connections', []):
                    if conn_nickname in connections_dict:
                        conn = connections_dict[conn_nickname]
                        self.add_connection_row(
                            conn,
                            indent_level=1,
                            display_group_id=group_info.get('id'),
                        )
                        displayed_connections.add(conn_nickname)

            matches = [
                c for c in connections
                if connection_matches(c, search_text)
                and c.nickname not in displayed_connections
            ]
            for conn in sorted(matches, key=lambda c: c.nickname.lower()):
                self.add_connection_row(conn)
                displayed_connections.add(conn.nickname)
            self._ungrouped_area_row = None
            # Restore scroll position
            if scroll_position is not None and hasattr(self, 'connection_scrolled') and self.connection_scrolled:
                vadj = self.connection_scrolled.get_vadjustment()
                if vadj:
                    GLib.idle_add(lambda: vadj.set_value(scroll_position))
            return


        # Get group hierarchy
        hierarchy = self.group_manager.get_group_hierarchy()

        # Build the list with groups
        self._build_grouped_list(hierarchy, connections_dict, 0)

        # Add ungrouped connections at the end. A connection is only ungrouped
        # when it does not belong to any group (it may belong to several).
        ungrouped_nicks = [
            conn.nickname for conn in connections
            if not self.group_manager.get_connection_groups(conn.nickname)
        ]

        if ungrouped_nicks:
            # Keep root connection order in sync
            updated = False
            for nick in ungrouped_nicks:
                if nick not in self.group_manager.root_connections:
                    self.group_manager.root_connections.append(nick)
                    updated = True

            existing = set(ungrouped_nicks)
            if any(nick not in existing for nick in self.group_manager.root_connections):
                self.group_manager.root_connections = [
                    nick for nick in self.group_manager.root_connections
                    if nick in existing
                ]
                updated = True

            if updated:
                self.group_manager._save_groups()

            for nick in self.group_manager.root_connections:
                conn = connections_dict.get(nick)
                if conn:
                    self.add_connection_row(conn)


        # Store reference to ungrouped area (hidden by default)
        self._ungrouped_area_row = None
        
        # Restore scroll position
        if scroll_position is not None and hasattr(self, 'connection_scrolled') and self.connection_scrolled:
            vadj = self.connection_scrolled.get_vadjustment()
            if vadj:
                GLib.idle_add(lambda: vadj.set_value(scroll_position))
    def _build_grouped_list(self, hierarchy, connections_dict, level):
        """Recursively build the grouped connection list.

        Returns the list of top-level GroupRows created at this level so the
        caller can register them as children for in-place expand/collapse.
        """
        created_rows = []
        for group_info in hierarchy:
            # Add group row
            group_row = GroupRow(group_info, self.group_manager, connections_dict)
            group_row.connect('group-toggled', self._on_group_toggled)
            if hasattr(group_row, "set_indentation"):
                group_row.set_indentation(level)
            self.connection_list.append(group_row)

            # Build direct connection rows once, then let expand/collapse toggle
            # their visibility in place to avoid full-list flicker.
            for conn_nickname in group_info.get('connections', []):
                if conn_nickname in connections_dict:
                    conn = connections_dict[conn_nickname]
                    row = self.add_connection_row(
                        conn,
                        level + 1,
                        display_group_id=group_info.get('id'),
                    )
                    if row is not None and hasattr(group_row, "add_member_row"):
                        group_row.add_member_row(row)

            # Recursively add child groups and register them directly so a
            # parent collapse hides nested subgroups in place.
            if group_info.get('children'):
                child_group_rows = self._build_grouped_list(
                    group_info['children'], connections_dict, level + 1
                )
                if hasattr(group_row, "add_child_group_row"):
                    for child_row in child_group_rows:
                        group_row.add_child_group_row(child_row)

            if hasattr(group_row, "apply_descendant_visibility"):
                group_row.apply_descendant_visibility(True)

            created_rows.append(group_row)
        return created_rows

    def _on_group_toggled(self, group_row, group_id, expanded):
        """Handle group expand/collapse"""
        if hasattr(group_row, "apply_descendant_visibility"):
            group_row.apply_descendant_visibility(True)

        # Reselect the toggled group so focus doesn't jump to another row
        for row in self.connection_list:
            if hasattr(row, "group_id") and row.group_id == group_id:
                self._select_only_row(row)
                break
    
    def add_connection_row(
        self,
        connection: Connection,
        indent_level: int = 0,
        display_group_id: Optional[str] = None,
        in_tag_section: bool = False,
    ):
        """Add a connection row to the list with optional indentation"""
        row = ConnectionRow(
            connection,
            self.group_manager,
            self.config,
            file_manager_callback=self._open_manage_files_for_connection,
            display_group_id=display_group_id,
            in_tag_section=in_tag_section,
        )
        
        # Apply indentation preference for grouped connections
        row.set_indentation(indent_level)
        
        self.connection_list.append(row)
        # A connection can appear under multiple groups, so keep a list of rows
        self.connection_rows.setdefault(connection, []).append(row)
        
        # Apply current hide-hosts setting to new row
        if hasattr(row, 'apply_hide_hosts'):
            row.apply_hide_hosts(getattr(self, '_hide_hosts', False))

        return row

    def on_search_changed(self, entry):
        """Handle search text changes and update connection list."""
        self.rebuild_connection_list()
        first_row = self.connection_list.get_row_at_index(0)
        if first_row:
            self._select_only_row(first_row)

    def on_search_stopped(self, entry):
        """Handle search stop (Esc key)."""
        entry.set_text('')
        self.rebuild_connection_list()
        # Hide the search container
        if hasattr(self, 'search_container') and self.search_container:
            self.search_container.set_visible(False)
        # Return focus to connection list
        if hasattr(self, 'connection_list') and self.connection_list:
            self.connection_list.grab_focus()

    def _on_search_entry_key_pressed(self, controller, keyval, keycode, state):
        """Handle key presses in search entry."""
        if keyval == Gdk.KEY_Down:
            # Move focus into the connection list and select the first result so
            # the user can navigate matches with the arrow keys. (Arrow Up from
            # the first result returns here — see _on_connection_list_nav_key.)
            if hasattr(self, 'connection_list') and self.connection_list:
                first_row = self.connection_list.get_row_at_index(0)
                if first_row:
                    self._select_only_row(first_row)
                    # Focus the row directly (not the container) so GTK4
                    # ListBox arrow-key navigation works immediately.
                    first_row.grab_focus()
                else:
                    self.connection_list.grab_focus()
            return True
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            # Enter connects to the first matching host. Picking a different
            # match is done with the arrow keys.
            row = self._first_connection_row()
            if row is not None:
                self._select_only_row(row)
                self._return_to_tab_view_if_welcome()
                self._cycle_connection_tabs_or_open(row.connection)
            return True
        return False

    def setup_signals(self):
        """Connect to manager signals"""
        # Connection manager signals - use connect_after to avoid conflict with GObject.connect
        self.connection_manager.connect_after('connection-added', self.on_connection_added)
        self.connection_manager.connect_after('connection-removed', self.on_connection_removed)
        self.connection_manager.connect_after('connection-status-changed', self.on_connection_status_changed)
        
        # Config signals
        self.config.connect('setting-changed', self.on_setting_changed)


    def _is_start_tab_page(self, page) -> bool:
        return page is not None and page is getattr(self, '_start_tab_page', None)

    def has_user_tabs(self) -> bool:
        try:
            return self.tab_view.get_n_pages() > 1
        except Exception:
            return False

    def is_start_tab_selected(self) -> bool:
        try:
            page = self.tab_view.get_selected_page()
            return self._is_start_tab_page(page)
        except Exception:
            return False

    def _pin_start_tab_page(self) -> None:
        """Pin the Start tab so the tab bar hides its close button."""
        page = getattr(self, '_start_tab_page', None)
        if page is None:
            return
        try:
            self.tab_view.set_page_pinned(page, True)
        except Exception:
            try:
                page.set_pinned(True)
            except Exception:
                logger.debug("Could not pin Start tab", exc_info=True)

    def _create_start_tab(self) -> None:
        """Add the pinned Start tab (WelcomePage) if it is not already present."""
        if getattr(self, '_start_tab_page', None) is not None:
            try:
                if self._start_tab_page in list(self.tab_view.get_pages()):
                    self._pin_start_tab_page()
                    return
            except Exception:
                pass

        self.welcome_view = WelcomePage(self)
        self._start_tab_page = self.tab_view.prepend(self.welcome_view)
        self._start_tab_page.set_title(_('Start'))
        try:
            from sshpilot import icon_utils
            self._start_tab_page.set_icon(
                icon_utils.new_gicon_from_icon_name('go-home-symbolic')
            )
        except Exception:
            pass
        self._pin_start_tab_page()
        self.tab_view.set_selected_page(self._start_tab_page)
        self._update_content_theme_for_selected_tab()

    def _update_content_theme_for_selected_tab(self) -> None:
        """Use terminal background only when a non-Start tab is selected."""
        if not hasattr(self, 'tab_view'):
            return
        try:
            if self.is_start_tab_selected():
                self.tab_view.remove_css_class('terminal-bg')
            else:
                self.tab_view.add_css_class('terminal-bg')
        except Exception:
            logger.debug("Failed to update tab content theme", exc_info=True)

    def show_start_tab(self) -> None:
        """Select the pinned Start tab."""
        self._create_start_tab()
        try:
            self.tab_view.set_selected_page(self._start_tab_page)
        except Exception:
            pass
        self._update_content_theme_for_selected_tab()
        GLib.idle_add(self._focus_connection_list_first_row)

        try:
            if (self.config.get_setting('ui.sidebar_show_when_no_tabs', False)
                    and not self.has_user_tabs()):
                self._apply_sidebar_visible(True)
        except Exception:
            logger.debug("sidebar show-when-no-tabs failed", exc_info=True)

        logger.info("Showing Start tab")

    def show_welcome_view(self):
        """Select the pinned Start tab (legacy name)."""
        self.show_start_tab()

    def show_tab_view(self):
        """Select the first user tab, or keep the current selection."""
        if self.has_user_tabs() and self.is_start_tab_selected():
            try:
                for page in self.tab_view.get_pages():
                    if not self._is_start_tab_page(page):
                        self.tab_view.set_selected_page(page)
                        break
            except Exception:
                pass
        self._update_content_theme_for_selected_tab()
        self._update_layout_toggle_state()
        logger.info("Showing tab view")

    def _focus_connection_list_first_row(self):
        """Focus the first row of the connection list so arrow-key navigation works immediately."""
        try:
            if not hasattr(self, 'connection_list') or self.connection_list is None:
                return False
            if not self.connection_list.get_parent():
                return False

            first_row = self.connection_list.get_row_at_index(0)

            # During startup: auto-select first row if nothing is selected yet.
            if not getattr(self, '_startup_complete', False):
                try:
                    selected_rows = list(self.connection_list.get_selected_rows())
                except Exception:
                    sel = self.connection_list.get_selected_row()
                    selected_rows = [sel] if sel else []
                if not selected_rows and first_row:
                    self._select_only_row(first_row)

            # Focus the first row directly — not just the ListBox container.
            # GTK4 ListBox arrow-key navigation only works when a *row* has
            # focus; grab_focus() on the container leaves no row focused.
            if first_row:
                first_row.grab_focus()
            else:
                self.connection_list.grab_focus()
        except Exception as e:
            logger.debug(f"Focus connection list failed: {e}")
        return False

    def focus_connection_list(self):
        """Focus the connection list and show a toast notification."""
        try:
            if hasattr(self, 'connection_list') and self.connection_list:
                # If sidebar is hidden, show it first
                if hasattr(self, 'sidebar_toggle_button') and self.sidebar_toggle_button:
                    if self.sidebar_toggle_button.get_active():
                        self.sidebar_toggle_button.set_active(False)

                # Close the search bar (if open) and clear the filter so the
                # full connection list is shown when focus moves here.
                if (
                    getattr(self, 'search_container', None)
                    and self.search_container.get_visible()
                ):
                    if getattr(self, 'search_entry', None):
                        self.search_entry.set_text('')
                    self.rebuild_connection_list()
                    self.search_container.set_visible(False)

                # Ensure a row is selected before focusing
                try:
                    selected_rows = list(self.connection_list.get_selected_rows())
                except Exception:
                    selected_row = self.connection_list.get_selected_row()
                    selected_rows = [selected_row] if selected_row else []
                logger.debug(f"Focus connection list - current selection count: {len(selected_rows)}")
                target_row = selected_rows[0] if selected_rows else None
                if target_row is None:
                    # Select the first row regardless of type
                    target_row = self.connection_list.get_row_at_index(0)
                    logger.debug(f"Focus connection list - first row: {target_row}")
                    if target_row:
                        self._select_only_row(target_row)
                        logger.debug(f"Focus connection list - selected first row: {target_row}")

                # Focus the row directly (not the ListBox container): GTK4
                # arrow-key navigation only works when a row holds focus, and
                # after a rebuild the container won't delegate focus to a row.
                if target_row is not None:
                    target_row.grab_focus()
                else:
                    self.connection_list.grab_focus()
                
                
                # Show toast notification
                toast = Adw.Toast.new(
                    f"Switched to connection list — ↑/↓ navigate, Enter open, {get_primary_modifier_label()}+Enter new tab"
                )
                toast.set_timeout(3)  # seconds
                if hasattr(self, 'toast_overlay'):
                    self.toast_overlay.add_toast(toast)
        except Exception as e:
            logger.error(f"Error focusing connection list: {e}")

    def activate_search_entry(self):
        """Show (if hidden) and focus the connection search entry.

        Bound to the Ctrl/Cmd+F shortcut. It only ever turns search *on* and
        focuses it so the user can always press the shortcut and start typing.
        Hiding the search bar is done exclusively via the toolbar search button
        (``focus_search_entry``)."""
        try:
            if not (hasattr(self, 'search_entry') and self.search_entry):
                return

            # If the sidebar is hidden, reveal it first
            if hasattr(self, 'sidebar_toggle_button') and self.sidebar_toggle_button:
                if self.sidebar_toggle_button.get_active():
                    self.sidebar_toggle_button.set_active(False)

            was_visible = True
            if hasattr(self, 'search_container') and self.search_container:
                was_visible = self.search_container.get_visible()
                if not was_visible:
                    self.search_container.set_visible(True)

            # Always focus and select any existing text so typing replaces it
            self.search_entry.grab_focus()
            text = self.search_entry.get_text()
            if text:
                self.search_entry.select_region(0, len(text))

            # Only show the hint toast when search was just revealed
            if not was_visible:
                toast = Adw.Toast.new(
                    "Search mode — Type to filter connections, Esc to clear and hide"
                )
                toast.set_timeout(3)  # seconds
                if hasattr(self, 'toast_overlay'):
                    self.toast_overlay.add_toast(toast)
        except Exception as e:
            logger.error(f"Failed to activate search entry: {e}")

    def focus_search_entry(self):
        """Toggle search on/off and show appropriate toast notification."""
        try:
            if hasattr(self, 'search_entry') and self.search_entry:
                # If sidebar is hidden, show it first
                if hasattr(self, 'sidebar_toggle_button') and self.sidebar_toggle_button:
                    if self.sidebar_toggle_button.get_active():
                        self.sidebar_toggle_button.set_active(False)
                
                # Toggle search container visibility
                if hasattr(self, 'search_container') and self.search_container:
                    is_visible = self.search_container.get_visible()
                    self.search_container.set_visible(not is_visible)
                    
                    if not is_visible:
                        # Search was hidden, now showing it
                        # Focus the search entry
                        self.search_entry.grab_focus()
                        
                        # Select all text if there's any
                        text = self.search_entry.get_text()
                        if text:
                            self.search_entry.select_region(0, len(text))
                        
                        # Show toast notification
                        toast = Adw.Toast.new(
                            "Search mode — Type to filter connections, Esc to clear and hide"
                        )
                        toast.set_timeout(3)  # seconds
                        if hasattr(self, 'toast_overlay'):
                            self.toast_overlay.add_toast(toast)
                    else:
                        # Search was visible, now hiding it
                        # Clear search text
                        self.search_entry.set_text('')
                        self.rebuild_connection_list()
                        
                        # Return focus to connection list
                        if hasattr(self, 'connection_list') and self.connection_list:
                            self.connection_list.grab_focus()
                        
                        # Show toast notification
                        toast = Adw.Toast.new(
                            f"Search hidden — {get_primary_modifier_label()}+F to search again"
                        )
                        toast.set_timeout(2)  # seconds
                        if hasattr(self, 'toast_overlay'):
                            self.toast_overlay.add_toast(toast)
        except Exception as e:
            logger.error(f"Failed to toggle search entry: {e}")

    def _return_to_tab_view_if_welcome(self):
        """Switch to a user tab when an action fires while Start is selected."""
        try:
            if not self.is_start_tab_selected():
                return
            if not self.has_user_tabs():
                return
            logger.debug("Leaving Start tab due to user interaction")
            self.show_tab_view()
        except Exception as exc:
            logger.debug(f"Failed to return to tab view: {exc}")

    def show_connection_dialog(
            self,
            connection: Connection = None,
            *,
            skip_group_warning: bool = False,
            force_split_from_group: bool = False,
            split_group_source: Optional[str] = None,
            split_original_nickname: Optional[str] = None,
    ):
        """Show connection dialog for adding/editing connections"""
        logger.info(f"Show connection dialog for: {connection}")

        # Refresh connection from disk to ensure latest auth method
        if connection is not None:
            try:
                self.connection_manager.load_ssh_config()
                refreshed = self.connection_manager.find_connection_by_nickname(connection.nickname)
                if refreshed:
                    connection = refreshed
            except Exception:
                pass

        if connection is not None and not skip_group_warning:
            block_info = None
            try:
                source_path = split_group_source or getattr(connection, 'source', None)
                block_info = self.connection_manager.get_host_block_details(connection.nickname, source_path)
            except Exception as e:
                logger.debug(f"Failed to inspect host block for {connection.nickname}: {e}")
            if block_info and len(block_info.get('hosts') or []) > 1:
                self._prompt_group_edit_options(connection, block_info)
                return

        split_source_for_dialog = split_group_source or (getattr(connection, 'source', None) if connection else None)
        original_token = split_original_nickname or (connection.nickname if connection else None)

        # Create connection dialog (imported lazily to keep it off startup path)
        from .connection_dialog import ConnectionDialog
        dialog = ConnectionDialog(
            self,
            connection,
            self.connection_manager,
            force_split_from_group=force_split_from_group,
            split_group_source=split_source_for_dialog,
            split_original_nickname=original_token,
        )
        dialog.connect('connection-saved', self.on_connection_saved)
        dialog.present()


    def _prompt_group_edit_options(self, connection: Connection, block_info: Dict[str, Any]):
        """Present options when editing a grouped host"""
        try:
            host_label = getattr(connection, 'nickname', '')
            other_hosts = max(0, len(block_info.get('hosts') or []) - 1)
            message = _("\"{host}\" is part of a configuration block with [{count}] other hosts. How would you like to apply your changes?").format(host=host_label, count=other_hosts)

            dialog = Adw.MessageDialog.new(self, _("Warning"), message)
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('manual', _('Manually Edit SSH Configuration'))
            dialog.add_response('split', _('Edit as Separate Connection'))
            dialog.set_response_appearance('manual', Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response('manual')
            dialog.set_close_response('cancel')

            source_path = block_info.get('source') or getattr(connection, 'source', None)
            original_name = getattr(connection, 'nickname', None)

            def on_response(dlg, response):
                dlg.destroy()
                if response == 'manual':
                    self._open_ssh_config_editor()
                elif response == 'split':
                    self.show_connection_dialog(
                        connection,
                        skip_group_warning=True,
                        force_split_from_group=True,
                        split_group_source=source_path,
                        split_original_nickname=original_name,
                    )

            dialog.connect('response', on_response)
            dialog.present()
        except Exception as e:
            logger.error(f"Failed to present group edit options: {e}")
            self.show_connection_dialog(connection, skip_group_warning=True)

    def _open_ssh_config_editor(self):
        try:
            # Single editor for the SSH config: the GtkSourceView-backed editor,
            # which self-degrades to a plain TextView if GtkSourceView is absent.
            from .text_editor import RemoteFileEditorWindow
            from .ssh_config_utils import validate_ssh_config_text

            config_path = getattr(self.connection_manager, 'ssh_config_path', None)
            if not config_path:
                from .platform_utils import get_ssh_dir
                config_path = os.path.join(get_ssh_dir(), 'config')

            config_path = os.path.abspath(os.path.expanduser(config_path))
            config_name = os.path.basename(config_path)

            # Set up file monitoring to detect when the file is saved
            file_modified_time = 0.0
            if os.path.exists(config_path):
                try:
                    file_modified_time = os.path.getmtime(config_path)
                except Exception:
                    pass

            def _reload_ssh_config():
                """Reload SSH config and refresh connection list, preserving group membership"""
                try:
                    # Capture current connections and their group memberships before reload
                    old_connections = {conn.nickname: conn for conn in self.connection_manager.get_connections()}
                    old_group_memberships = {}
                    for nickname in old_connections.keys():
                        group_id = self.group_manager.get_connection_group(nickname)
                        if group_id:
                            old_group_memberships[nickname] = group_id

                    # Reload SSH config (this creates new Connection objects)
                    self.connection_manager.load_ssh_config()
                    new_connections = {conn.nickname: conn for conn in self.connection_manager.get_connections()}

                    # Detect nickname changes by matching connections on hostname/username/port
                    # This handles the case where Host value changes but connection is otherwise the same
                    for old_nickname, old_conn in old_connections.items():
                        if old_nickname in old_group_memberships:
                            # This connection was in a group, try to find its new nickname
                            group_id = old_group_memberships[old_nickname]

                            # Try to find matching connection by hostname/username/port
                            matching_new_nickname = None
                            for new_nickname, new_conn in new_connections.items():
                                if (new_conn.hostname == old_conn.hostname and
                                    new_conn.username == old_conn.username and
                                    new_conn.port == old_conn.port):
                                    matching_new_nickname = new_nickname
                                    break

                            # If we found a match and nickname changed, update group membership
                            if matching_new_nickname and matching_new_nickname != old_nickname:
                                try:
                                    self.group_manager.rename_connection(old_nickname, matching_new_nickname)
                                    logger.info(f"Preserved group membership: '{old_nickname}' -> '{matching_new_nickname}' in group {group_id}")
                                except Exception as e:
                                    logger.error(f"Failed to preserve group membership for renamed connection: {e}")
                            # If old nickname still exists, group membership is already preserved

                    self.rebuild_connection_list()
                    logger.info("SSH config reloaded after file save")
                except Exception as e:
                    logger.error(f"Failed to refresh connections after SSH config save: {e}")

            # Monitor file for changes
            def _on_file_changed(monitor, file, other_file, event_type):
                """Handle file system changes to detect saves"""
                if event_type in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CHANGES_DONE_HINT):
                    try:
                        if os.path.exists(config_path):
                            new_mtime = os.path.getmtime(config_path)
                            nonlocal file_modified_time
                            if new_mtime > file_modified_time:
                                file_modified_time = new_mtime
                                # Reload after a short delay to ensure file is fully written
                                GLib.timeout_add(100, _reload_ssh_config)
                    except Exception as e:
                        logger.debug(f"Error checking file modification time: {e}")

            # Create file monitor
            try:
                gfile = Gio.File.new_for_path(config_path)
                file_monitor = gfile.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
                file_monitor.connect("changed", _on_file_changed)
                # Store monitor reference to keep it alive
                if not hasattr(self, '_ssh_config_monitors'):
                    self._ssh_config_monitors = []
                self._ssh_config_monitors.append(file_monitor)
            except Exception as e:
                logger.debug(f"Failed to set up file monitoring for SSH config: {e}")
                file_monitor = None

            editor = RemoteFileEditorWindow(
                parent=self,
                file_path=config_path,
                file_name=config_name,
                is_local=True,
                sftp_manager=None,
                file_manager_window=None,
                pre_save_validator=validate_ssh_config_text,
                language_id="sshconfig",
                show_outline=True,
            )

            # Set custom title for SSH config editor (the path shows as the
            # header subtitle automatically).
            editor.set_title(_("Edit SSH Config"))  # window/taskbar title
            if hasattr(editor, 'set_editor_title'):
                editor.set_editor_title(_("SSH Config"))

            # Also reload when the editor closes (fallback)
            def _on_editor_close_request(window):
                # Clean up file monitor
                if hasattr(self, '_ssh_config_monitors') and file_monitor:
                    try:
                        if file_monitor in self._ssh_config_monitors:
                            self._ssh_config_monitors.remove(file_monitor)
                        file_monitor.cancel()
                    except Exception:
                        pass
                # Final reload check when closing
                try:
                    if os.path.exists(config_path):
                        new_mtime = os.path.getmtime(config_path)
                        if new_mtime > file_modified_time:
                            _reload_ssh_config()
                except Exception:
                    pass
                return False  # Allow window to close

            editor.connect("close-request", _on_editor_close_request)
            editor.present()
        except Exception as e:
            logger.error(f"Failed to open SSH config editor: {e}")

    def show_connection_selection_for_ssh_copy(self):
        """Open the ssh-copy-id dialog with no server preselected; its
        embedded server picker handles the choice."""
        if not self.connection_manager.get_connections():
            # No connections available, show new connection dialog instead
            logger.info("No connections available, showing new connection dialog")
            self.show_connection_dialog()
            return
        try:
            from .sshcopyid_window import SshCopyIdWindow
            SshCopyIdWindow(self, None, self.key_manager, self.connection_manager)
        except Exception as e:
            logger.error(f"Failed to show SSH key copy dialog: {e}")

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
            dlg.set_child(tv)

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
                    logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {e!s}")
                    self._error("Generate & Copy failed",
                                "Could not generate a new key and copy it to the server.",
                                str(e))

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
            # Show message dialog
            try:
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("No Server Selected"),
                    body=_("Select a server first!")
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
            except Exception as e:
                logger.error(f"Failed to show error dialog: {e}")
            return
        connection = selected_row.connection
        if Capability.KEY_DEPLOYMENT not in capabilities_for(connection):
            logger.debug("ssh-copy-id unavailable: protocol %r has no key deployment",
                         getattr(connection, 'protocol', 'ssh'))
            return
        logger.info(f"Main window: Selected connection: {getattr(connection, 'nickname', 'unknown')}")
        logger.debug(f"Main window: Connection details - host: {getattr(connection, 'hostname', getattr(connection, 'host', 'unknown'))}, "
                    f"username: {getattr(connection, 'username', 'unknown')}, "
                    f"port: {getattr(connection, 'port', 22)}")

        try:
            logger.info("Main window: Creating SshCopyIdWindow")
            logger.debug("Main window: Initializing SshCopyIdWindow with key_manager and connection_manager")
            from .sshcopyid_window import SshCopyIdWindow
            win = SshCopyIdWindow(self, connection, self.key_manager, self.connection_manager)
            logger.info("Main window: SshCopyIdWindow created successfully, presenting")
            win.present()
        except Exception as e:
            logger.error(f"Main window: ssh-copy-id window failed: {e}")
            logger.debug(f"Main window: Exception details: {type(e).__name__}: {e!s}")
            # Fallback error if window cannot be created
            try:
                md = Adw.MessageDialog(transient_for=self, modal=True,
                                       heading="Error",
                                       body=f"Could not open the Copy Key window.\n\n{e}")
                md.add_response("ok", "OK")
                md.present()
            except Exception:
                pass

    def toggle_list_focus(self):
        """Toggle focus between connection list and terminal"""
        # Use the focus-ancestry check (not connection_list.has_focus()): after
        # focusing, a child *row* holds focus, so has_focus() on the ListBox
        # itself is False and the toggle would never return to the terminal.
        if self._focus_is_in_connection_list():
            # Focus the active terminal. Use the split-view-aware lookup so this
            # also returns focus to the active pane of a SplitViewTab (a
            # SplitViewTab child has no .vte attribute of its own).
            terminal = self._get_active_terminal_widget()
            if terminal is not None:
                self._focus_terminal_widget(terminal)
        else:
            # Focus connection list with toast notification
            self.focus_connection_list()

    def _select_tab_relative(self, delta: int):
        """Select tab relative to current index, wrapping around."""
        self._return_to_tab_view_if_welcome()
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

    def _move_tab_relative(self, delta: int):
        """Reorder the selected tab one position left (-1) or right (+1)."""
        try:
            page = self.tab_view.get_selected_page()
            if page is None:
                return
            if delta < 0:
                self.tab_view.reorder_backward(page)
            elif delta > 0:
                self.tab_view.reorder_forward(page)
        except Exception:
            pass

    def _close_active_tab_or_pane(self):
        """Close the current tab — but in a split-view tab, close the focused
        pane instead (Ctrl+Shift+W is context-dependent)."""
        try:
            page = self.tab_view.get_selected_page()
            if page is None or self._is_start_tab_page(page):
                return
            child = page.get_child()
            from .split_view import SplitViewTab
            if isinstance(child, SplitViewTab) and hasattr(child, 'close_focused_pane'):
                child.close_focused_pane()
                return
            self.tab_view.close_page(page)
        except Exception:
            pass


    # Signal handlers
    def on_connection_activated(self, list_box, row):
        """Handle connection activation (Enter key)"""
        self._return_to_tab_view_if_welcome()
        logger.debug(f"Connection activated - row: {row}, has connection: {hasattr(row, 'connection') if row else False}")
        if row and hasattr(row, 'connection'):
            self._cycle_connection_tabs_or_open(row.connection)
        elif row and hasattr(row, 'group_id'):
            # Handle group row activation - toggle expand/collapse
            logger.debug(f"Group row activated - toggling expand/collapse for group: {row.group_id}")
            row._toggle_expand()
            

        
    def on_connection_activate(self, list_box, row):
        """Handle connection activation (Enter key or double-click)"""
        self._return_to_tab_view_if_welcome()
        if row and hasattr(row, 'connection'):
            self._cycle_connection_tabs_or_open(row.connection)
            return True  # Stop event propagation
        return False
        
    def on_activate_connection(self, action, param):
        """Handle the activate-connection action"""
        self._return_to_tab_view_if_welcome()
        row = self.connection_list.get_selected_row()
        if row and hasattr(row, 'connection'):
            self._cycle_connection_tabs_or_open(row.connection)

    def _focus_most_recent_tab(self, connection: Connection) -> None:
        """Focus the most recent tab for a connection if one exists.

        Does nothing if the connection has no open tabs.
        """
        try:
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

            if not terms_for_conn:
                return

            target_term = self.active_terminals.get(connection)
            if target_term not in terms_for_conn:
                target_term = terms_for_conn[0]

            page = self._page_for_child(target_term)
            if page is None:
                return

            if self.tab_view.get_selected_page() != page:
                self.tab_view.set_selected_page(page)

            self.active_terminals[connection] = target_term
            self._focus_terminal_widget(target_term)
        except Exception as e:
            logger.error(f"Failed to focus most recent tab for {getattr(connection, 'nickname', '')}: {e}")


    def _focus_most_recent_tab_or_open_new(self, connection: Connection):
        """If there are open tabs for this server, focus the most recent one.
        Otherwise open a new tab for the server.
        """
        self._return_to_tab_view_if_welcome()
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
                
                page = self._page_for_child(target_term)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    # Update most-recent mapping
                    self.active_terminals[connection] = target_term
                    # Give focus to the VTE terminal so user can start typing immediately
                    self._focus_terminal_widget(target_term)
                    return

            # No existing tabs for this connection -> open a new one
            self.terminal_manager.connect_to_host(connection, force_new=False)
        except Exception as e:
            logger.error(f"Failed to focus most recent tab or open new for {getattr(connection, 'nickname', '')}: {e}")

    def _cycle_connection_tabs_or_open(self, connection: Connection):
        """If there are open tabs for this server, cycle to the next one (wrap).
        Otherwise open a new tab for the server.
        """
        self._return_to_tab_view_if_welcome()
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
                page = self._page_for_child(next_term)
                if page is not None:
                    self.tab_view.set_selected_page(page)
                    # Update most-recent mapping
                    self.active_terminals[connection] = next_term
                    self._focus_terminal_widget(next_term)
                    return

            # No existing tabs for this connection -> open a new one
            self.terminal_manager.connect_to_host(connection, force_new=False)
            try:
                self._focus_most_recent_tab(connection)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to cycle or open for {getattr(connection, 'nickname', '')}: {e}")

    def _focus_terminal_widget(self, terminal: TerminalWidget) -> None:
        """Request focus for a terminal widget, retrying on idle if needed."""

        if terminal is None:
            return

        def _focus_attempt(_source=None) -> bool:
            try:
                # Use backend's grab_focus method if available (works for both VTE and PyXterm.js)
                if hasattr(terminal, 'backend') and terminal.backend:
                    terminal.backend.grab_focus()
                # Fallback to vte for backwards compatibility
                elif hasattr(terminal, 'vte') and terminal.vte:
                    terminal.vte.grab_focus()
                elif hasattr(terminal, 'grab_focus'):
                    terminal.grab_focus()
            except Exception as focus_error:
                logger.debug(f"Deferred terminal focus failed: {focus_error}")
            return GLib.SOURCE_REMOVE

        # Try immediate focus
        try:
            if hasattr(terminal, 'backend') and terminal.backend:
                terminal.backend.grab_focus()
            elif hasattr(terminal, 'vte') and terminal.vte:
                terminal.vte.grab_focus()
            elif hasattr(terminal, 'grab_focus'):
                terminal.grab_focus()
        except Exception:
            pass

        # Schedule retries for delayed focus (useful when widget is still being created)
        GLib.idle_add(_focus_attempt, priority=GLib.PRIORITY_DEFAULT_IDLE)
        GLib.timeout_add(150, _focus_attempt)
        GLib.timeout_add(350, _focus_attempt)

    def _get_active_terminal_widget(self) -> Optional[TerminalWidget]:
        """Return the TerminalWidget for the currently selected tab, if any."""
        terminal_manager = getattr(self, 'terminal_manager', None)
        if terminal_manager is not None:
            return terminal_manager.get_focused_terminal()
        return None

    def toggle_terminal_search_overlay(self, select_all: bool = False) -> None:
        """Toggle the search overlay for the currently focused terminal tab."""
        terminal = self._get_active_terminal_widget()
        if terminal is None:
            return

        revealer = getattr(terminal, 'search_revealer', None)
        is_revealed = False
        if revealer is not None:
            try:
                is_revealed = bool(revealer.get_reveal_child())
            except Exception as exc:
                logger.debug('Failed to query terminal search revealer state: %s', exc)

        try:
            if is_revealed and hasattr(terminal, '_hide_search_overlay'):
                terminal._hide_search_overlay()
            elif hasattr(terminal, '_show_search_overlay'):
                terminal._show_search_overlay(select_all=select_all)
        except Exception as exc:
            logger.debug('Failed to toggle terminal search overlay: %s', exc)

    def on_tab_selected(self, tab_view: Adw.TabView, _pspec=None) -> None:
        """Update active terminal mapping when the user switches tabs."""
        self._update_content_theme_for_selected_tab()
        self._update_layout_toggle_state()
        try:
            page = tab_view.get_selected_page()
            if page is None:
                return
            child = page.get_child() if hasattr(page, 'get_child') else None
            if child is None:
                return

            if self._is_start_tab_page(page):
                GLib.idle_add(self._focus_connection_list_first_row)
                try:
                    if (self.config.get_setting('ui.sidebar_show_when_no_tabs', False)
                            and not self.has_user_tabs()):
                        self._apply_sidebar_visible(True)
                except Exception:
                    pass
                return
            
            # Focus the terminal when tab is selected
            if _is_terminal_widget(child):
                # Use a small delay to ensure the widget is fully visible
                def _focus_on_tab_switch():
                    try:
                        self._focus_terminal_widget(child)
                    except Exception as e:
                        logger.debug(f"Failed to focus terminal on tab switch: {e}")
                GLib.timeout_add(50, _focus_on_tab_switch)

            # Split-view tabs have no single connection — clear sidebar selection
            from .split_view import SplitViewTab
            if isinstance(child, SplitViewTab):
                try:
                    if hasattr(self.connection_list, 'unselect_all'):
                        self.connection_list.unselect_all()
                    else:
                        current = self.connection_list.get_selected_row()
                        if current is not None:
                            self.connection_list.unselect_row(current)
                except Exception:
                    pass
                return

            connection = self.terminal_to_connection.get(child)
            if connection:
                # Check if this is a local terminal
                host_value = _get_connection_host(connection) or _get_connection_alias(connection)
                if host_value == 'localhost':
                    # Local terminal - clear selection
                    try:
                        if hasattr(self.connection_list, 'unselect_all'):
                            self.connection_list.unselect_all()
                        else:
                            current = self.connection_list.get_selected_row()
                            if current is not None:
                                self.connection_list.unselect_row(current)
                    except Exception:
                        pass
                else:
                    # Regular connection terminal - select the corresponding row
                    self.active_terminals[connection] = child
                    conn_rows = self._rows_for_connection(connection)
                    if conn_rows:
                        selected_rows = []
                        try:
                            selected_rows = list(self.connection_list.get_selected_rows())
                        except Exception:
                            current = self.connection_list.get_selected_row()
                            if current:
                                selected_rows = [current]
                        # Leave the selection alone if any row for this
                        # connection is already selected; otherwise select the first.
                        if not any(r in selected_rows for r in conn_rows):
                            self._select_only_row(conn_rows[0])
            else:
                # Other non-connection terminal - clear selection
                try:
                    if hasattr(self.connection_list, 'unselect_all'):
                        self.connection_list.unselect_all()
                    else:
                        current = self.connection_list.get_selected_row()
                        if current is not None:
                            self.connection_list.unselect_row(current)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to sync tab selection: {e}")

    def on_connection_selected(self, list_box, row):
        """Handle connection list selection change"""
        try:
            connection_rows = self._get_selected_connection_rows()
            group_rows = self._get_selected_group_rows()
        except Exception:
            connection_rows = []
            group_rows = []

        has_connections = bool(connection_rows)
        has_groups = bool(group_rows)

        if has_connections and not has_groups:
            self.connection_toolbar.set_visible(True)
            self.group_toolbar.set_visible(False)

            multiple_connections = len(connection_rows) > 1
            selected_conn = getattr(connection_rows[0], 'connection', None)
            caps = capabilities_for(selected_conn) if (not multiple_connections and selected_conn) else frozenset()
            self.edit_button.set_sensitive(not multiple_connections)
            if hasattr(self, 'copy_key_button'):
                self.copy_key_button.set_sensitive(
                    not multiple_connections and Capability.KEY_DEPLOYMENT in caps
                )
            if hasattr(self, 'scp_button'):
                self.scp_button.set_sensitive(
                    not multiple_connections and Capability.FILE_TRANSFER in caps
                )
            self.manage_files_button.set_sensitive(
                not multiple_connections
                and Capability.FILE_TRANSFER in caps
                and not should_hide_file_manager_options()
            )
            self.manage_files_button.set_visible(not should_hide_file_manager_options())
            if hasattr(self, 'system_terminal_button') and self.system_terminal_button:
                # System terminal rides build_native_command(), an SSH-only path.
                self.system_terminal_button.set_sensitive(
                    not multiple_connections
                    and getattr(selected_conn, 'protocol', 'ssh') == 'ssh'
                )
            self.delete_button.set_sensitive(True)
            self.rename_group_button.set_sensitive(False)
            self.delete_group_button.set_sensitive(False)
        elif has_groups and not has_connections:
            self.connection_toolbar.set_visible(False)
            self.group_toolbar.set_visible(True)

            # Rename works for tag groups too (renames the tag); delete does not.
            allow_single_group = len(group_rows) == 1
            allow_group_delete = (
                allow_single_group
                and not getattr(group_rows[0], 'is_tag_group', False)
            )
            self.delete_button.set_sensitive(False)
            if hasattr(self, 'copy_key_button'):
                self.copy_key_button.set_sensitive(False)
            if hasattr(self, 'scp_button'):
                self.scp_button.set_sensitive(False)
            self.manage_files_button.set_sensitive(False)
            self.manage_files_button.set_visible(not should_hide_file_manager_options())
            if hasattr(self, 'system_terminal_button') and self.system_terminal_button:
                self.system_terminal_button.set_sensitive(False)
            self.rename_group_button.set_sensitive(allow_single_group)
            self.delete_group_button.set_sensitive(allow_group_delete)
        else:
            self.connection_toolbar.set_visible(False)
            self.group_toolbar.set_visible(False)
            self.delete_button.set_sensitive(False)
            if hasattr(self, 'copy_key_button'):
                self.copy_key_button.set_sensitive(False)
            if hasattr(self, 'scp_button'):
                self.scp_button.set_sensitive(False)
            self.manage_files_button.set_sensitive(False)
            self.manage_files_button.set_visible(not should_hide_file_manager_options())
            if hasattr(self, 'system_terminal_button') and self.system_terminal_button:
                self.system_terminal_button.set_sensitive(False)
            self.rename_group_button.set_sensitive(False)
            self.delete_group_button.set_sensitive(False)

    def on_add_connection_clicked(self, button):
        """Handle add connection button click"""
        self.show_connection_dialog()

    def on_edit_connection_clicked(self, button):
        """Handle edit connection button click"""
        selected_row = self.connection_list.get_selected_row()
        if selected_row:
            if hasattr(selected_row, 'connection'):
                self.show_connection_dialog(selected_row.connection)
            else:
                logger.debug("Cannot edit group row")

    def on_sidebar_toggle(self, button):
        """Handle sidebar toggle button click"""
        try:
            # Button active state now represents the action to perform
            # True = hide sidebar, False = show sidebar
            should_hide = button.get_active()
            is_visible = not should_hide
            self._toggle_sidebar_visibility(is_visible)
            # A manual toggle cancels any pending "hide on terminal open" delay.
            self._cancel_pending_sidebar_hide()

            # Update button icon and tooltip based on current sidebar state
            from sshpilot import icon_utils
            if is_visible:
                icon_utils.set_button_icon(button, 'sidebar-show-symbolic')
                button.set_tooltip_text(
                    f'Hide Sidebar (F9, {get_primary_modifier_label()}+B)'
                )
            else:
                icon_utils.set_button_icon(button, 'sidebar-show-symbolic')
                button.set_tooltip_text(
                    f'Show Sidebar (F9, {get_primary_modifier_label()}+B)'
                )
            
            # No need to save state - sidebar always starts visible
                
        except Exception as e:
            logger.error(f"Failed to toggle sidebar: {e}")

    # --- Sidebar behavior (Settings ▸ Sidebar ▸ Sidebar behavior) ------------
    def _apply_sidebar_visible(self, visible: bool) -> None:
        """Programmatically show/hide the sidebar and keep the toggle button in
        sync (used by the behavior hooks)."""
        try:
            self._toggle_sidebar_visibility(visible)
            if hasattr(self, 'sidebar_toggle_button'):
                # Button 'active' means "hidden" (see on_sidebar_toggle).
                self.sidebar_toggle_button.set_active(not visible)
        except Exception:
            logger.debug("apply_sidebar_visible failed", exc_info=True)

    def _cancel_pending_sidebar_hide(self) -> None:
        tid = getattr(self, '_sidebar_hide_timer_id', None)
        if tid:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self._sidebar_hide_timer_id = None

    def _hide_sidebar_after_terminal(self) -> bool:
        """Deferred hide so a freshly-opened terminal settles before the sidebar
        slides away (smoother than hiding instantly)."""
        self._sidebar_hide_timer_id = None
        try:
            self._apply_sidebar_visible(False)
        except Exception:
            logger.debug("hide_sidebar_after_terminal failed", exc_info=True)
        return GLib.SOURCE_REMOVE

    def _toggle_sidebar_visibility(self, is_visible):
        """Helper method to toggle sidebar visibility"""
        try:
            logger.debug(f"Toggle sidebar visibility requested: {is_visible}, split variant: {getattr(self, '_split_variant', 'unknown')}")
            if HAS_OVERLAY_SPLIT and getattr(self, '_split_variant', '') == 'overlay':
                # For OverlaySplitView
                self.split_view.set_show_sidebar(is_visible)
                logger.debug(f"Set OverlaySplitView sidebar visibility to: {is_visible}")
            elif HAS_NAV_SPLIT and getattr(self, '_split_variant', '') == 'navigation':
                # NavigationSplitView doesn't have set_show_sidebar method
                # Use collapsed property to hide/show sidebar
                # When collapsed=True and show-content=False, sidebar is visible
                # When collapsed=True and show-content=True, content is visible
                # When collapsed=False, both are visible side by side
                self._sidebar_visible = is_visible
                if is_visible:
                    # Show sidebar: un-collapse or show sidebar
                    try:
                        self.split_view.set_collapsed(False)
                    except Exception:
                        # If un-collapsing fails, try showing sidebar via show-content
                        try:
                            self.split_view.set_show_content(False)
                        except Exception:
                            pass
                else:
                    # Hide sidebar: collapse and show content
                    try:
                        self.split_view.set_collapsed(True)
                        self.split_view.set_show_content(True)
                    except Exception:
                        pass
                logger.debug(f"NavigationSplitView sidebar visibility set to: {is_visible}")
            else:
                # For Gtk.Paned fallback
                sidebar_widget = self.split_view.get_start_child()
                if sidebar_widget:
                    sidebar_widget.set_visible(is_visible)
                    logger.debug(f"Set Gtk.Paned sidebar visibility to: {is_visible}")
        except Exception as e:
            logger.error(f"Failed to toggle sidebar visibility: {e}")


    def on_scp_button_clicked(self, button):
        return self.scp_controller.on_scp_button_clicked(button)

    def on_manage_files_button_clicked(self, button):
        """Handle manage files button click from toolbar"""
        try:
            selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                return
            connection = getattr(selected_row, 'connection', None)
            if not connection:
                return

            self._open_manage_files_for_connection(connection)
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

    def _show_ssh_copy_id_terminal_using_main_widget(self, connection, ssh_key, force=False):
        return self.sshcopyid_runner.run(connection, ssh_key, force)

    def on_delete_connection_clicked(self, button):
        """Handle delete connection button click"""
        target_rows = self._get_target_connection_rows()
        if not target_rows:
            logger.debug("Delete requested without any connection selection")
            return

        connections = self._connections_from_rows(target_rows)
        neighbor_row = self._determine_neighbor_connection_row(target_rows)

        self._prompt_delete_connections(connections, neighbor_row)

    def on_rename_group_clicked(self, button):
        """Handle rename group button click"""
        selected_row = self.connection_list.get_selected_row()
        if selected_row and getattr(selected_row, 'is_tag_group', False):
            self.on_rename_tag_action(selected_row)
        elif selected_row and hasattr(selected_row, 'group_id'):
            # Pin the context row to the selection so a stale context-menu
            # row (possibly a tag row) can't divert the action.
            self._context_menu_group_row = selected_row
            self.on_edit_group_action(None, None)

    def on_delete_group_clicked(self, button):
        """Handle delete group button click"""
        selected_row = self.connection_list.get_selected_row()
        if (selected_row and hasattr(selected_row, 'group_id')
                and not getattr(selected_row, 'is_tag_group', False)):
            # Pin the context row to the selection so a stale context-menu
            # row (possibly a tag row) can't divert the action.
            self._context_menu_group_row = selected_row
            self.on_delete_group_action(None, None)

    def on_delete_connection_response(self, dialog, response, payload):
        """Handle delete connection dialog response"""
        try:
            connections: List[Connection]

            if isinstance(payload, dict):
                connections = payload.get('connections', []) or []
            elif isinstance(payload, (list, tuple)):
                connections = list(payload)
            else:
                connections = [payload] if payload else []

            if response not in {'delete', 'close_remove'}:
                return

            pending = iter(connection for connection in connections if connection)
            self._deleting_connections_batch = True

            def _delete_next_connection():
                try:
                    connection = next(pending)
                except StopIteration:
                    self._deleting_connections_batch = False
                    self.group_manager._save_groups()
                    try:
                        self.connection_manager.load_ssh_config()
                    except Exception:
                        logger.exception(
                            "Failed to reload connections after batch deletion"
                        )
                    return False

                try:
                    if response == 'close_remove':
                        self._disconnect_connection_terminals(connection)
                    self.connection_manager.remove_connection(
                        connection,
                        reload_config=False,
                    )
                except Exception:
                    logger.exception(
                        "Failed to delete connection %s",
                        getattr(connection, 'nickname', ''),
                    )

                # Give GTK a chance to process redraws and window-manager
                # responsiveness checks between potentially blocking keyring
                # and filesystem operations.
                GLib.idle_add(_delete_next_connection)
                return False

            GLib.idle_add(_delete_next_connection)
        except Exception as e:
            self._deleting_connections_batch = False
            logger.error(f"Failed to delete connections: {e}")

    def on_connection_added(self, manager, connection):
        """Handle new connection added"""
        self.group_manager.connections.setdefault(connection.nickname, None)
        if connection.nickname not in self.group_manager.root_connections:
            self.group_manager.root_connections.append(connection.nickname)
            self.group_manager._save_groups()
        self.rebuild_connection_list()

    def on_connection_removed(self, manager, connection):
        """Handle connection removed from the connection manager"""
        logger.info(f"Connection removed: {connection.nickname}")

        # Save current scroll position before any UI changes
        scroll_position = None
        if hasattr(self, 'connection_scrolled') and self.connection_scrolled:
            vadj = self.connection_scrolled.get_vadjustment()
            if vadj:
                scroll_position = vadj.get_value()

        # Remove from UI if it exists (a connection may have several rows)
        if connection in self.connection_rows:
            for row in self._rows_for_connection(connection):
                self.connection_list.remove(row)
            del self.connection_rows[connection]
        
        # Remove from group manager, including any group it was copied into
        self.group_manager.connections.pop(connection.nickname, None)
        if connection.nickname in self.group_manager.root_connections:
            self.group_manager.root_connections.remove(connection.nickname)
        for group in self.group_manager.groups.values():
            if connection.nickname in group.get('connections', []):
                group['connections'] = [
                    n for n in group['connections'] if n != connection.nickname
                ]
        if not getattr(self, '_deleting_connections_batch', False):
            self.group_manager._save_groups()
        self._refresh_group_rows_after_connection_removed(connection)

        # Close all terminals for this connection and clean up maps
        terminals = list(self.connection_to_terminals.get(connection, []))
        # Suppress confirmation while we programmatically close pages
        self._suppress_close_confirmation = True
        try:
            for term in terminals:
                try:
                    page = self._page_for_child(term)
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

        # Restore scroll position without auto-selecting any row
        def _restore_scroll_only():
            if scroll_position is not None and hasattr(self, 'connection_scrolled') and self.connection_scrolled:
                vadj = self.connection_scrolled.get_vadjustment()
                if vadj:
                    vadj.set_value(scroll_position)
            return False

        # Use idle_add to restore scroll position after UI updates complete
        GLib.idle_add(_restore_scroll_only)

    def _refresh_group_rows_after_connection_removed(self, connection):
        """Refresh visible group counts after an incremental row removal."""
        connections_dict = {
            conn.nickname: conn
            for conn in self.connection_manager.get_connections()
        }
        row = self.connection_list.get_first_child()
        while row:
            if isinstance(row, GroupRow):
                if getattr(row, 'is_tag_group', False):
                    row.group_info['connections'] = [
                        nickname
                        for nickname in row.group_info.get('connections', [])
                        if nickname != connection.nickname
                    ]
                else:
                    current_group = self.group_manager.groups.get(row.group_id)
                    if current_group is not None:
                        row.group_info['connections'] = list(
                            current_group.get('connections', [])
                        )

                row.connections_dict = connections_dict
                if hasattr(row, '_member_rows'):
                    row._member_rows = [
                        member
                        for member in row._member_rows
                        if getattr(member, 'connection', None) is not connection
                    ]
                row._update_display()
            row = row.get_next_sibling()

    def _recompute_connection_state(self, connection):
        """Aggregate per-terminal state into the connection's authoritative state.

        This is the single place the OR-across-terminals rule lives (moved out of
        the sidebar renderer): a connection is CONNECTED while any of its
        terminals is connected, and only drops to DISCONNECTED when the last one
        goes down. A richer "down" state already set on the connection (FAILED)
        is preserved rather than being flattened to DISCONNECTED.
        """
        # Local terminals use a lightweight LocalConnection without the status
        # API and have no sidebar row — nothing to aggregate.
        if not hasattr(connection, 'get_status'):
            return
        try:
            terminals = []
            if hasattr(self, 'connection_to_terminals'):
                terminals = list(self.connection_to_terminals.get(connection, []) or [])
            # Fall back to the most-recent terminal map for edge cases where the
            # comprehensive map hasn't been populated yet (mirrors prior renderer).
            if not terminals and hasattr(self, 'active_terminals'):
                recent = self.active_terminals.get(connection)
                if recent is not None:
                    terminals = [recent]

            def _term_state(t):
                s = getattr(t, 'connection_state', None)
                if isinstance(s, ConnectionState):
                    return s
                return (
                    ConnectionState.CONNECTED
                    if getattr(t, 'is_connected', False)
                    else ConnectionState.DISCONNECTED
                )

            states = [_term_state(t) for t in terminals]

            # Priority across a connection's terminals: any live tab wins, then a
            # tab still connecting, then a failed attempt (keep its reason), else
            # disconnected.
            if any(s == ConnectionState.CONNECTED for s in states):
                self.connection_manager.update_connection_state(connection, ConnectionState.CONNECTED)
            elif any(s == ConnectionState.CONNECTING for s in states):
                self.connection_manager.update_connection_state(connection, ConnectionState.CONNECTING)
            elif any(s == ConnectionState.FAILED for s in states):
                reason = ''
                for t in terminals:
                    if _term_state(t) == ConnectionState.FAILED:
                        reason = getattr(t, 'connection_state_reason', '') or ''
                        break
                self.connection_manager.update_connection_state(
                    connection, ConnectionState.FAILED, reason
                )
            elif terminals:
                self.connection_manager.update_connection_state(connection, ConnectionState.DISCONNECTED)
            else:
                # No terminals at all (every tab for this connection is closed):
                # there is nothing to report, so go neutral and hide the
                # indicator rather than showing a red "Disconnected"/"failed"
                # icon for what is an intentional close.
                self.connection_manager.update_connection_state(connection, ConnectionState.UNKNOWN)
        except Exception as e:
            logger.error(f"Failed to recompute connection state: {e}")

    def on_connection_status_changed(self, manager, connection, is_connected):
        """Handle connection status change (render-only — state is authoritative)."""
        logger.debug(f"Connection status changed: {connection.nickname} - {'Connected' if is_connected else 'Disconnected'}")
        rows = self._rows_for_connection(connection)
        if rows:
            for row in rows:
                # Render each row from the connection's authoritative state.
                row.update_status()
                row.queue_draw()

        # If this was a controlled reconnect and we are now connected, reset the flag
        if is_connected and getattr(self, '_is_controlled_reconnect', False):
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
        elif key == 'ui.group_row_display':
            normalized = 'nested'
            try:
                normalized = str(value).lower()
            except Exception:
                pass
            if normalized not in {'fullwidth', 'nested'}:
                normalized = 'nested'

            # Covers both connection rows and group headers (nested groups
            # honor the same fullwidth/nested layout).
            for row in self.connection_list:
                if hasattr(row, 'refresh_group_display_mode'):
                    row.refresh_group_display_mode(normalized)

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

        # Capture the currently-open tabs so they can be restored next launch
        # when the user has chosen the "previous session" startup behavior.
        try:
            session_manager = getattr(self, 'session_manager', None)
            if session_manager is not None:
                session_manager.save_previous(self.capture_session())
        except Exception as e:
            logger.debug(f"Failed to capture previous session on close: {e}")
            
        # Check for active connections across all tabs
        actually_connected = {}
        local_terminals = []
        ssh_terminals = []
        
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                if getattr(term, 'is_connected', False):
                    actually_connected.setdefault(conn, []).append(term)
                    # Categorize terminals
                    if hasattr(term, '_is_local_terminal') and term._is_local_terminal():
                        local_terminals.append(term)
                    else:
                        ssh_terminals.append(term)
        
        # If there are SSH terminals, always show warning
        if ssh_terminals:
            self.show_quit_confirmation_dialog()
            return True  # Prevent close, let dialog handle it
        
        # If there are only local terminals, check their job status
        if local_terminals:
            # Check if any local terminal has an active job
            has_active_jobs = False
            for term in local_terminals:
                if hasattr(term, 'has_active_job') and term.has_active_job():
                    has_active_jobs = True
                    break
            
            # If any local terminal has an active job, show warning
            if has_active_jobs:
                self.show_quit_confirmation_dialog()
                return True  # Prevent close, let dialog handle it
        
        # No active connections or all local terminals are idle, safe to close
        return False  # Allow close

    def prompt_ssh_passphrase(self, key_path: str, prompt: str = "") -> "str | None":
        """Show the SSH key passphrase prompt as a modal child of the main window.

        Invoked (on the GTK main thread) by the askpass IPC server so the prompt
        renders above the main window instead of as a stray top-level helper
        window that can hide behind it on Wayland. Returns the passphrase, or
        None if the user cancelled. Blocks until the dialog is dismissed.
        """
        present_for_modal_dialog(self)
        return _show_password_passphrase_dialog(
            self,
            prompt_type="passphrase",
            key_path=key_path or None,
        )

    def prompt_ssh_password(
        self,
        *,
        display_name: str = "",
        host: Optional[str] = None,
        username: Optional[str] = None,
        heading: Optional[str] = None,
        body: Optional[str] = None,
    ) -> Optional[str]:
        """Show an SSH password prompt as a modal child of the main window.

        Thin wrapper around :func:`show_ssh_password_dialog` for callers that
        already have the :class:`MainWindow` instance (e.g. future in-app actions).
        """
        return show_ssh_password_dialog(
            parent_window=self,
            display_name=display_name,
            host=host,
            username=username,
            connection_manager=getattr(self, "connection_manager", None),
            heading=heading,
            body=body,
        )

    def show_quit_confirmation_dialog(self):
        """Show confirmation dialog when quitting with active connections"""
        # Best-effort raise of the main window. On X11 / for a minimized window
        # this brings it forward; on Wayland a background app can't force a raise
        # without an activation token, so this only flags attention there. The
        # confirmation itself is a real top-level Gtk.AlertDialog (below) so it is
        # surfaced by the compositor regardless.
        try:
            self.unminimize()
        except Exception as e:
            logger.debug(f"Failed to unminimize window: {e}")
        try:
            self.present()
        except Exception as e:
            logger.debug(f"Failed to bring window to foreground: {e}")

        # Categorize connected terminals
        connected_items = []
        local_terminals = []
        ssh_terminals = []
        
        for conn, terms in self.connection_to_terminals.items():
            for term in terms:
                if getattr(term, 'is_connected', False):
                    connected_items.append((conn, term))
                    # Categorize terminals
                    if hasattr(term, '_is_local_terminal') and term._is_local_terminal():
                        local_terminals.append((conn, term))
                    else:
                        ssh_terminals.append((conn, term))
        
        active_count = len(connected_items)
        
        # Determine dialog content based on terminal types
        if ssh_terminals:
            # SSH terminals present - use original messaging
            if active_count == 1:
                message = f"You have 1 open terminal tab."
                detail = "Closing the application will disconnect this connection."
            else:
                message = f"You have {active_count} open terminal tabs."
                detail = f"Closing the application will disconnect all connections."
        else:
            # Only local terminals with active jobs
            if active_count == 1:
                message = f"You have 1 local terminal with an active job."
                detail = "Closing the application will terminate the running process."
            else:
                message = f"You have {active_count} local terminals with active jobs."
                detail = f"Closing the application will terminate all running processes."

        # Use Gtk.AlertDialog: it builds its own top-level window, so the
        # compositor maps it even when the main window is in the background
        # (an in-window Adw.AlertDialog is drawn inside the background surface
        # and stays unreachable on Wayland).
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message("Quit SSH Pilot?")
        dialog.set_detail(f"{message}\n\n{detail}")
        dialog.set_buttons(['Cancel', 'Quit Anyway'])
        dialog.set_cancel_button(0)   # Escape / dismiss -> Cancel
        dialog.set_default_button(1)  # Enter -> Quit Anyway

        app = self.get_application()
        if app is not None:
            app.hold()

        dialog.choose(self, None, self._on_quit_alert_chosen)

    def _on_quit_alert_chosen(self, dialog, result):
        """Handle the quit confirmation Gtk.AlertDialog result."""
        app = self.get_application()
        try:
            try:
                index = dialog.choose_finish(result)
            except GLib.Error:
                # Dismissed via Escape / window close -> treat as Cancel.
                index = -1
            if index == 1:  # "Quit Anyway"
                shutdown.cleanup_and_quit(self)
            else:
                # Cancel / dismissed: the user is staying in the app, so bring
                # the main window to the front. The button click provided a
                # valid activation token, so present() is honored on Wayland.
                try:
                    self.unminimize()
                except Exception as e:
                    logger.debug(f"Failed to unminimize window: {e}")
                try:
                    self.present()
                except Exception as e:
                    logger.debug(f"Failed to bring window to foreground: {e}")
        finally:
            if app is not None:
                app.release()


    def on_manage_local_authorized_keys_action(self, action, param=None):
        """Open the structured editor on the local user's ~/.ssh/authorized_keys."""
        try:
            from .authorized_keys_window import AuthorizedKeysWindow
        except Exception as exc:
            logger.error("authorized_keys editor unavailable: %s", exc)
            return
        try:
            window = AuthorizedKeysWindow(
                parent=self,
                local_path="~/.ssh/authorized_keys",
                connection_manager=self.connection_manager,
                key_manager=getattr(self, 'key_manager', None),
            )
            window.present()
        except Exception as exc:
            logger.error("Failed to open local authorized_keys editor: %s", exc)

    def on_manage_authorized_keys_action(self, action, param=None):
        """Open the structured authorized_keys editor for the right-clicked connection."""
        if not (hasattr(self, '_context_menu_connection') and self._context_menu_connection):
            return
        connection = self._context_menu_connection
        if Capability.KEY_DEPLOYMENT not in capabilities_for(connection):
            logger.debug("authorized_keys editor unavailable: protocol %r has no key deployment",
                         getattr(connection, 'protocol', 'ssh'))
            return
        try:
            from .authorized_keys_window import AuthorizedKeysWindow
            from .file_manager import create_file_manager_backend
        except Exception as exc:
            logger.error("authorized_keys editor unavailable: %s", exc)
            return

        host_value = _get_connection_host(connection) or _get_connection_alias(connection)
        username = getattr(connection, 'username', '') or ''
        port_value = getattr(connection, 'port', 22) or 22

        ssh_config = None
        if hasattr(self, 'config') and self.config is not None:
            try:
                ssh_config = self.config.get_ssh_config()
            except Exception as exc:
                logger.debug("Failed to read SSH configuration for authorized_keys editor: %s", exc)
                ssh_config = None

        initial_password = getattr(connection, 'password', None) or None
        if not initial_password and self.connection_manager is not None:
            try:
                initial_password = self.connection_manager.get_password(host_value, username)
            except Exception as exc:
                logger.debug("Password lookup failed for authorized_keys editor: %s", exc)
                initial_password = None

        try:
            manager = create_file_manager_backend(
                str(host_value or ''),
                str(username or ''),
                int(port_value),
                password=initial_password,
                connection=connection,
                connection_manager=self.connection_manager,
                ssh_config=ssh_config,
            )
        except Exception as exc:
            logger.error("Failed to create SFTP manager for authorized_keys: %s", exc)
            return

        try:
            window = AuthorizedKeysWindow(
                parent=self,
                connection=connection,
                sftp_manager=manager,
                connection_manager=self.connection_manager,
                key_manager=getattr(self, 'key_manager', None),
            )
            window.present()
        except Exception as exc:
            logger.error("Failed to open authorized_keys editor: %s", exc)
            try:
                manager.close()
            except Exception:
                pass

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
            target_rows = self._get_target_connection_rows(prefer_context=True)
            if not target_rows:
                return

            connections = self._connections_from_rows(target_rows)
            neighbor_row = self._determine_neighbor_connection_row(target_rows)

            self._prompt_delete_connections(connections, neighbor_row)
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

    def on_run_command_action(self, action=None, param=None):
        """Open the command picker for the right-clicked connection(s) or group."""
        panel = self._ensure_command_blocks_panel()
        if panel is None:
            return
        anchor = getattr(self, '_context_menu_row', None) or self
        # Copy the multi-select snapshot by value: it is cleared when the
        # context menu closes, but the picker popover outlives it.
        connections = list(getattr(self, '_context_menu_connections', None) or [])
        connection = getattr(self, '_context_menu_connection', None)
        group_row = getattr(self, '_context_menu_group_row', None)

        # Only target connections whose protocol can run remote commands.
        connections = [
            c for c in connections
            if Capability.REMOTE_COMMAND in capabilities_for(c)
        ]
        if connection is not None and Capability.REMOTE_COMMAND not in capabilities_for(connection):
            connection = None

        if len(connections) > 1:
            panel.show_command_picker_for_target(anchor, connections=connections)
        elif connection is not None:
            panel.show_command_picker_for_target(anchor, connection=connection)
        elif group_row is not None:
            gm = getattr(self, 'group_manager', None)
            if gm is None:
                return
            group = gm.groups.get(group_row.group_id)
            if group:
                panel.show_command_picker_for_target(anchor, group=group)

    def on_create_group_action(self, action, param=None):
        """Handle create group action"""
        try:
            # Form dialog (name + color): Adw.Dialog with actions in the header bar
            dialog = Adw.Dialog()
            dialog.set_title(_("Create New Group"))
            dialog.set_content_width(400)

            header = Adw.HeaderBar()
            header.set_show_start_title_buttons(False)
            header.set_show_end_title_buttons(False)

            cancel_button = Gtk.Button(label=_('Cancel'))
            header.pack_start(cancel_button)

            create_button = Gtk.Button(label=_('Create'))
            create_button.add_css_class('suggested-action')
            header.pack_end(create_button)

            content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)

            toolbar_view = Adw.ToolbarView()
            toolbar_view.add_top_bar(header)
            toolbar_view.set_content(content_area)
            dialog.set_child(toolbar_view)

            # Add label
            label = Gtk.Label(label=_("Enter a name for the new group:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)
            
            # Add text entry
            entry = Gtk.Entry()
            entry.set_placeholder_text(_("e.g., Production Servers"))
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_area.append(entry)

            # Color selector
            color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            color_row.set_hexpand(True)
            color_label = Gtk.Label(label=_("Group color"))
            color_label.set_xalign(0)
            color_label.set_hexpand(True)
            color_row.append(color_label)

            color_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            color_button = Gtk.ColorButton()
            color_button.set_use_alpha(True)
            color_button.set_title(_("Select group color"))
            rgba = Gdk.RGBA()
            rgba.red = rgba.green = rgba.blue = 0
            rgba.alpha = 0
            color_button.set_rgba(rgba)
            color_controls.append(color_button)

            color_selected = False

            def on_color_set(_button):
                nonlocal color_selected
                color_selected = True

            color_button.connect('color-set', on_color_set)

            clear_color_button = Gtk.Button(label=_("Clear"))
            clear_color_button.add_css_class('flat')

            def on_clear_color(_button):
                nonlocal color_selected
                color_selected = False
                cleared = Gdk.RGBA()
                cleared.red = cleared.green = cleared.blue = 0
                cleared.alpha = 0
                color_button.set_rgba(cleared)

            clear_color_button.connect('clicked', on_clear_color)
            color_controls.append(clear_color_button)

            color_row.append(color_controls)
            content_area.append(color_row)

            def on_create(_button):
                group_name = entry.get_text().strip()
                if not group_name:
                    error_dialog = Adw.MessageDialog(
                        transient_for=self,
                        modal=True,
                        heading=_("Error"),
                        body=_("Please enter a group name.")
                    )
                    error_dialog.add_response('ok', _('OK'))
                    error_dialog.present()
                    return
                selected_color = None
                rgba_value = color_button.get_rgba()
                if color_selected and rgba_value.alpha > 0:
                    selected_color = rgba_value.to_string()
                self.group_manager.create_group(group_name, color=selected_color)
                self.rebuild_connection_list()
                dialog.close()

            cancel_button.connect('clicked', lambda _b: dialog.close())
            create_button.connect('clicked', on_create)
            dialog.set_default_widget(create_button)
            dialog.present(self)
            
            def focus_entry():
                entry.grab_focus()
                return False
            
            GLib.idle_add(focus_entry)
            
        except Exception as e:
            logger.error(f"Failed to show create group dialog: {e}")
    
    def on_edit_group_action(self, action, param=None):
        """Handle edit group action"""
        try:
            logger.debug("Edit group action triggered")
            # Get the group row from context menu or selected row
            selected_row = getattr(self, '_context_menu_group_row', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
            logger.debug(f"Selected row: {selected_row}")
            if not selected_row:
                logger.debug("No selected row")
                return
            if not hasattr(selected_row, 'group_id'):
                logger.debug("Selected row is not a group row")
                return
            
            group_id = selected_row.group_id
            logger.debug(f"Group ID: {group_id}")
            group_info = self.group_manager.groups.get(group_id)
            if not group_info:
                logger.debug(f"Group info not found for ID: {group_id}")
                return
            
            # Form dialog (name + color): Adw.Dialog with actions in the header bar
            dialog = Adw.Dialog()
            dialog.set_title(_("Edit Group"))
            dialog.set_content_width(400)

            header = Adw.HeaderBar()
            header.set_show_start_title_buttons(False)
            header.set_show_end_title_buttons(False)

            cancel_button = Gtk.Button(label=_('Cancel'))
            header.pack_start(cancel_button)

            save_button = Gtk.Button(label=_('Save'))
            save_button.add_css_class('suggested-action')
            header.pack_end(save_button)

            content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)

            toolbar_view = Adw.ToolbarView()
            toolbar_view.add_top_bar(header)
            toolbar_view.set_content(content_area)
            dialog.set_child(toolbar_view)

            # Add label
            label = Gtk.Label(label=_("Enter a new name for the group:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)
            
            # Add text entry
            entry = Gtk.Entry()
            entry.set_text(group_info['name'])
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_area.append(entry)

            # Color selector
            color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            color_row.set_hexpand(True)
            color_label = Gtk.Label(label=_("Group color"))
            color_label.set_xalign(0)
            color_label.set_hexpand(True)
            color_row.append(color_label)

            color_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            color_button = Gtk.ColorButton()
            color_button.set_use_alpha(True)
            color_button.set_title(_("Select group color"))

            color_selected = False
            rgba = Gdk.RGBA()
            existing_color = group_info.get('color')
            if existing_color:
                try:
                    if rgba.parse(existing_color):
                        color_button.set_rgba(rgba)
                        color_selected = True
                    else:
                        rgba.red = rgba.green = rgba.blue = 0
                        rgba.alpha = 0
                        color_button.set_rgba(rgba)
                except Exception:
                    rgba.red = rgba.green = rgba.blue = 0
                    rgba.alpha = 0
                    color_button.set_rgba(rgba)
                    color_selected = False
            else:
                rgba.red = rgba.green = rgba.blue = 0
                rgba.alpha = 0
                color_button.set_rgba(rgba)

            def on_color_set(_button):
                nonlocal color_selected
                color_selected = True

            color_button.connect('color-set', on_color_set)
            color_controls.append(color_button)

            clear_color_button = Gtk.Button(label=_("Clear"))
            clear_color_button.add_css_class('flat')

            def on_clear_color(_button):
                nonlocal color_selected
                color_selected = False
                cleared = Gdk.RGBA()
                cleared.red = cleared.green = cleared.blue = 0
                cleared.alpha = 0
                color_button.set_rgba(cleared)

            clear_color_button.connect('clicked', on_clear_color)
            color_controls.append(clear_color_button)

            color_row.append(color_controls)
            content_area.append(color_row)

            def on_save(_button):
                new_name = entry.get_text().strip()
                if not new_name:
                    error_dialog = Adw.MessageDialog(
                        transient_for=self,
                        modal=True,
                        heading=_("Error"),
                        body=_("Please enter a group name.")
                    )
                    error_dialog.add_response('ok', _('OK'))
                    error_dialog.present()
                    return
                group_info['name'] = new_name
                rgba_value = color_button.get_rgba()
                selected_color = None
                if color_selected and rgba_value.alpha > 0:
                    selected_color = rgba_value.to_string()
                self.group_manager.set_group_color(group_id, selected_color)
                self.rebuild_connection_list()
                dialog.close()

            cancel_button.connect('clicked', lambda _b: dialog.close())
            save_button.connect('clicked', on_save)
            dialog.set_default_widget(save_button)
            dialog.present(self)
            
            def focus_entry():
                entry.grab_focus()
                entry.select_region(0, -1)
                return False
            
            GLib.idle_add(focus_entry)
            
        except Exception as e:
            logger.error(f"Failed to show edit group dialog: {e}")

    def on_rename_tag_action(self, tag_row):
        """Rename a virtual tag group: rewrite the tag on all tagged connections."""
        try:
            if not getattr(tag_row, 'is_tag_group', False):
                return
            if tag_row.group_info.get('untagged'):
                return  # the Untagged section is not a real tag
            old_name = str(tag_row.group_info.get('name', ''))
            old_key = str(tag_row.group_info.get('tag_key', '')) or old_name.casefold()

            # Form dialog: Adw.Dialog with actions in the header bar
            dialog = Adw.Dialog()
            dialog.set_title(_("Rename Tag"))
            dialog.set_content_width(400)

            header = Adw.HeaderBar()
            header.set_show_start_title_buttons(False)
            header.set_show_end_title_buttons(False)

            cancel_button = Gtk.Button(label=_('Cancel'))
            header.pack_start(cancel_button)

            save_button = Gtk.Button(label=_('Save'))
            save_button.add_css_class('suggested-action')
            header.pack_end(save_button)

            content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content_area.set_margin_start(20)
            content_area.set_margin_end(20)
            content_area.set_margin_top(20)
            content_area.set_margin_bottom(20)
            content_area.set_spacing(12)

            toolbar_view = Adw.ToolbarView()
            toolbar_view.add_top_bar(header)
            toolbar_view.set_content(content_area)
            dialog.set_child(toolbar_view)

            label = Gtk.Label(label=_("Enter a new name for the tag:"))
            label.set_wrap(True)
            label.set_xalign(0)
            content_area.append(label)

            entry = Gtk.Entry()
            entry.set_text(old_name)
            entry.set_activates_default(True)
            entry.set_hexpand(True)
            content_area.append(entry)

            def _show_error(body):
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Error"),
                    body=body,
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()

            def on_save(_button):
                new_name = entry.get_text().strip()
                if not new_name:
                    _show_error(_("Please enter a tag name."))
                    return
                if ',' in new_name:
                    # Tags are entered comma-separated in the connection
                    # dialog, so a comma in a tag name could not round-trip.
                    _show_error(_("Tag names cannot contain commas."))
                    return
                if new_name != old_name:
                    from .tag_groups import migrate_expanded_state
                    self.config.rename_tag(old_name, new_name)
                    try:
                        state = self.config.get_setting('ui.tag_groups_expanded', {}) or {}
                        self.config.set_setting(
                            'ui.tag_groups_expanded',
                            migrate_expanded_state(state, old_key, new_name.casefold()),
                        )
                    except Exception:
                        logger.debug("Failed to migrate tag expansion state", exc_info=True)
                    self.rebuild_connection_list()
                dialog.close()

            cancel_button.connect('clicked', lambda _b: dialog.close())
            save_button.connect('clicked', on_save)
            dialog.set_default_widget(save_button)
            dialog.present(self)

            def focus_entry():
                entry.grab_focus()
                entry.select_region(0, -1)
                return False

            GLib.idle_add(focus_entry)

        except Exception as e:
            logger.error(f"Failed to show rename tag dialog: {e}")

    def _open_tag_group_split(self, tag_row):
        """Open a tag group's connections in split view (context menu path)."""
        try:
            self._context_menu_group_row = tag_row
            self.on_open_group_in_split_view_action(None, None)
        except Exception as e:
            logger.error(f"Failed to open tag group in split view: {e}")

    def on_move_to_ungrouped_action(self, action, param=None):
        """Handle move to ungrouped / remove from group action.

        When the action is triggered from a connection shown under a specific
        group and that connection belongs to several groups, only the
        membership for that group is removed. Otherwise the connection is fully
        ungrouped (removed from every group).
        """
        try:
            connections = self._get_target_connections(prefer_context=True)
            if not connections:
                return

            context_row = getattr(self, '_context_menu_row', None)
            context_group_id = getattr(context_row, '_group_id', None)

            for connection in connections:
                nickname = getattr(connection, 'nickname', None)
                if not nickname:
                    continue
                member_groups = self.group_manager.get_connection_groups(nickname)
                if (
                    context_group_id
                    and len(connections) == 1
                    and context_group_id in member_groups
                    and len(member_groups) > 1
                ):
                    # Only detach from the group the row is displayed under
                    self.group_manager.remove_connection_from_group(nickname, context_group_id)
                else:
                    self.group_manager.move_connection(nickname, None)
            self.rebuild_connection_list()

        except Exception as e:
            logger.error(f"Failed to move connection to ungrouped: {e}")
    
    def on_move_to_group_action(self, action, param=None):
        """Handle move to group action"""
        self._open_group_assignment_dialog('move')

    def on_copy_to_group_action(self, action, param=None):
        """Handle copy to group action (keeps existing memberships)"""
        self._open_group_assignment_dialog('copy')

    def _open_group_assignment_dialog(self, mode: str = 'move'):
        """Show the dialog used by both 'Move to Group' and 'Copy to Group'.

        ``mode`` is either ``'move'`` (relocate the connection to the chosen
        group) or ``'copy'`` (add it to the chosen group while keeping it in any
        group it already belongs to).
        """
        is_copy = mode == 'copy'
        try:
            connections = self._get_target_connections(prefer_context=True)
            if not connections:
                return

            connection_nicknames = [
                conn.nickname for conn in connections if hasattr(conn, 'nickname')
            ]
            if not connection_nicknames:
                return

            def assign(nickname: str, target_group_id) -> None:
                if is_copy:
                    if target_group_id:
                        self.group_manager.copy_connection_to_group(nickname, target_group_id)
                else:
                    self.group_manager.move_connection(nickname, target_group_id)

            available_groups = self.get_available_groups()
            logger.debug(f"Available groups for {mode} dialog: {len(available_groups)} groups")

            from sshpilot import icon_utils

            title_text = _("Copy to Group") if is_copy else _("Move to Group")
            confirm_label = _("Copy") if is_copy else _("Move")

            # Adwaita dialog scaffold (GNOME HIG): Adw.Dialog + ToolbarView/HeaderBar
            # with a PreferencesPage body (provides insets and grouped row styling).
            dialog = Adw.Dialog()
            dialog.set_title(title_text)
            dialog.set_content_width(420)
            dialog.set_follows_content_size(True)

            toolbar_view = Adw.ToolbarView()
            header = Adw.HeaderBar()
            header.set_show_start_title_buttons(False)
            header.set_show_end_title_buttons(False)

            cancel_button = Gtk.Button(label=_("Cancel"))
            cancel_button.connect('clicked', lambda _b: dialog.close())
            header.pack_start(cancel_button)

            confirm_button = Gtk.Button(label=confirm_label)
            confirm_button.add_css_class('suggested-action')
            header.pack_end(confirm_button)

            toolbar_view.add_top_bar(header)

            page = Adw.PreferencesPage()
            body_scroller = Gtk.ScrolledWindow()
            body_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            body_scroller.set_propagate_natural_height(True)
            body_scroller.set_max_content_height(420)
            body_scroller.set_child(page)
            toolbar_view.set_content(body_scroller)
            dialog.set_child(toolbar_view)

            # --- Create new group -------------------------------------------
            create_group = Adw.PreferencesGroup()
            create_group.set_title(_("Create New Group"))
            if len(connection_nicknames) == 1:
                create_group.set_description(
                    _("Copy the connection to a new group") if is_copy
                    else _("Move the connection to a new group")
                )
            else:
                create_group.set_description(
                    _("Copy the selected connections to a new group") if is_copy
                    else _("Move the selected connections to a new group")
                )

            create_group_entry = Adw.EntryRow()
            create_group_entry.set_title(_("Group name"))
            create_group.add(create_group_entry)

            color_row = Adw.ActionRow()
            color_row.set_title(_("Color"))
            color_row.set_subtitle(_("Optional"))

            color_button = Gtk.ColorButton()
            color_button.set_valign(Gtk.Align.CENTER)
            color_button.set_use_alpha(True)
            color_button.set_title(_("Select group color"))
            initial_rgba = Gdk.RGBA()
            initial_rgba.red = initial_rgba.green = initial_rgba.blue = 0
            initial_rgba.alpha = 0
            color_button.set_rgba(initial_rgba)

            color_selected = False

            def mark_color_selected(_button):
                nonlocal color_selected
                color_selected = True

            color_button.connect('color-set', mark_color_selected)

            def reset_color_selection() -> None:
                nonlocal color_selected
                color_selected = False
                cleared = Gdk.RGBA()
                cleared.red = cleared.green = cleared.blue = 0
                cleared.alpha = 0
                color_button.set_rgba(cleared)

            clear_color_button = Gtk.Button(label=_("Clear"))
            clear_color_button.set_valign(Gtk.Align.CENTER)
            clear_color_button.add_css_class('flat')
            clear_color_button.connect('clicked', lambda _btn: reset_color_selection())

            color_row.add_suffix(color_button)
            color_row.add_suffix(clear_color_button)
            create_group.add(color_row)
            page.add(create_group)

            # --- Existing groups (single selection via a checkmark) ---------
            existing_group = Adw.PreferencesGroup()
            existing_group.set_title(_("Existing Groups"))

            selected_group_id = None
            selected_row_ref = None

            def has_valid_target() -> bool:
                if create_group_entry.get_text().strip():
                    return True
                return selected_group_id is not None

            def update_confirm_state(*_args):
                confirm_button.set_sensitive(has_valid_target())

            def select_group_row(row) -> None:
                nonlocal selected_group_id, selected_row_ref
                if selected_row_ref is row:
                    return
                if selected_row_ref is not None:
                    selected_row_ref._check.set_visible(False)
                selected_row_ref = row
                selected_group_id = row.group_id
                row._check.set_visible(True)
                update_confirm_state()

            if available_groups:
                for group in available_groups:
                    row = Adw.ActionRow()
                    row.set_title(group['name'])
                    row.set_activatable(True)
                    icon = icon_utils.new_image_from_icon_name('folder-symbolic')
                    row.add_prefix(icon)
                    check = Gtk.Image.new_from_icon_name('object-select-symbolic')
                    check.set_visible(False)
                    row.add_suffix(check)
                    row._check = check
                    row.group_id = group['id']
                    row.connect('activated', select_group_row)
                    existing_group.add(row)
            else:
                existing_group.set_description(_("No groups yet — create one above."))

            page.add(existing_group)

            # --- Validation + actions (business logic preserved) ------------
            def find_existing_group_id(name: str):
                lowered = name.lower()
                for group in available_groups:
                    if group['name'].lower() == lowered:
                        return group['id']
                return None

            def show_group_exists_error(message: str) -> None:
                error = Adw.AlertDialog(
                    heading=_("Group Already Exists"),
                    body=message,
                )
                error.add_response('ok', _("OK"))
                error.set_default_response('ok')
                error.set_close_response('ok')
                error.present(dialog)

            create_group_entry.connect('changed', update_confirm_state)
            update_confirm_state()

            def perform_move() -> bool:
                group_name = create_group_entry.get_text().strip()
                if group_name:
                    existing_group_id = find_existing_group_id(group_name)
                    if existing_group_id:
                        for nickname in connection_nicknames:
                            assign(nickname, existing_group_id)
                        self.rebuild_connection_list()
                        return True
                    try:
                        selected_color = None
                        rgba_value = color_button.get_rgba()
                        if color_selected and rgba_value.alpha > 0:
                            selected_color = rgba_value.to_string()
                        new_group_id = self.group_manager.create_group(group_name, color=selected_color)
                        for nickname in connection_nicknames:
                            assign(nickname, new_group_id)
                        self.rebuild_connection_list()
                        return True
                    except ValueError as e:
                        show_group_exists_error(str(e))
                        create_group_entry.set_text("")
                        reset_color_selection()
                        create_group_entry.grab_focus()
                        update_confirm_state()
                        return False

                if selected_group_id is not None:
                    for nickname in connection_nicknames:
                        assign(nickname, selected_group_id)
                    self.rebuild_connection_list()
                    return True
                return False

            def on_confirm(*_args):
                if has_valid_target() and perform_move():
                    dialog.close()

            confirm_button.connect('clicked', on_confirm)
            create_group_entry.connect('entry-activated', lambda _e: on_confirm())

            dialog.present(self)

        except Exception as e:
            logger.error(f"Failed to show move to group dialog: {e}")

    def move_connection_to_group(self, connection_nickname: str, target_group_id: Optional[str] = None):
        """Move a connection to a specific group"""
        try:
            self.group_manager.move_connection(connection_nickname, target_group_id)
            self.rebuild_connection_list()
        except Exception as e:
            logger.error(f"Failed to move connection {connection_nickname} to group: {e}")
    
    def get_available_groups(self) -> List[Dict]:
        """Get list of available groups for selection"""
        return self.group_manager.get_all_groups()

    def open_in_system_terminal(self, connection):
        """Open the connection in the system's default terminal using ssh_connection_builder"""
        if getattr(connection, 'protocol', 'ssh') != 'ssh':
            # build_native_command() is an SSH-only path.
            logger.debug("System terminal unavailable for protocol %r",
                         getattr(connection, 'protocol', 'ssh'))
            return
        try:
            from .ssh_connection_builder import build_native_command

            # Build a plain native command (ssh -F <config> host). The external
            # terminal provides its own TTY/agent, so we deliberately do NOT apply
            # sshPilot's in-app askpass or agent bypass here.
            ssh_cmd_parts = build_native_command(
                connection,
                self.config if hasattr(self, 'config') else None,
            )
            # Skip 'ssh' and join the rest, handling options properly
            ssh_command_parts = []
            i = 0
            while i < len(ssh_cmd_parts):
                if ssh_cmd_parts[i] == 'ssh':
                    i += 1
                    continue
                elif ssh_cmd_parts[i] == '-o' and i + 1 < len(ssh_cmd_parts):
                    # Quote option values that contain spaces
                    opt_val = ssh_cmd_parts[i + 1]
                    if ' ' in opt_val:
                        ssh_command_parts.append(f"-o '{opt_val}'")
                    else:
                        ssh_command_parts.append(f"-o {opt_val}")
                    i += 2
                elif ssh_cmd_parts[i].startswith('-'):
                    ssh_command_parts.append(ssh_cmd_parts[i])
                    i += 1
                else:
                    # Host or command - quote if needed
                    part = ssh_cmd_parts[i]
                    if ' ' in part:
                        ssh_command_parts.append(f"'{part}'")
                    else:
                        ssh_command_parts.append(part)
                    i += 1
            
            ssh_command = ' '.join(ssh_command_parts)
            # Prepend 'ssh' since we skipped it when building the command parts
            ssh_command = f'ssh {ssh_command}'

            use_external = self.config.get_setting('use-external-terminal', False)
            if use_external:
                terminal_command = self._get_user_preferred_terminal()
            else:
                terminal_command = self._get_default_terminal_command()

            if not terminal_command:
                common_terminals = [
                    'gnome-terminal', 'ptyxis', 'konsole', 'xterm', 'alacritty',
                    'kitty', 'terminator', 'tilix', 'xfce4-terminal'
                ]
                for term in common_terminals:
                    try:
                        result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                        if result.returncode == 0:
                            terminal_command = [term]
                            break
                    except Exception:
                        continue

            if not terminal_command:
                try:
                    result = subprocess.run(['which', 'xdg-terminal'], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        terminal_command = ['xdg-terminal']
                except Exception:
                    pass

            if not terminal_command:
                self._show_terminal_error_dialog()
                return

            self._open_system_terminal(terminal_command, ssh_command)

        except Exception as e:
            logger.error(f"Failed to open system terminal: {e}")
            self._show_terminal_error_dialog()

    def _open_system_terminal(self, terminal_command: List[str], ssh_command: str):
        """Launch a terminal command with an SSH command."""
        try:
            if is_macos():
                app = None
                if terminal_command and terminal_command[0] == 'open':
                    # handle commands like ['open', '-a', 'App']
                    if len(terminal_command) >= 3 and terminal_command[1] == '-a':
                        app = os.path.basename(terminal_command[2])
                if app:
                    app_lower = app.lower()
                    if app_lower in ['terminal', 'terminal.app']:
                        script = f'tell app "Terminal" to do script "{ssh_command}"\ntell app "Terminal" to activate'
                        cmd = ['osascript', '-e', script]
                    elif app_lower in ['iterm', 'iterm2', 'iterm.app']:
                        script = (
                            'tell application "iTerm"\n'
                            '    if (count of windows) = 0 then\n'
                            '        create window with default profile\n'
                            '    end if\n'
                            '    tell current window\n'
                            '        create tab with default profile\n'
                            f'        tell current session to write text "{ssh_command}"\n'
                            '    end tell\n'
                            '    activate\n'
                            'end tell'
                        )
                        cmd = ['osascript', '-e', script]
                    elif app_lower == 'warp':
                        cmd = ['open', f'warp://{ssh_command}']
                        # Warp handles focus automatically via URL scheme
                    elif app_lower in ['alacritty', 'kitty']:
                        cmd = ['open', '-a', app, '--args', '-e', 'bash', '-lc', f'{ssh_command}; exec bash']
                        # Launch terminal and then activate it
                        subprocess.Popen(cmd, start_new_session=True)
                        time.sleep(0.5)  # Give the app time to launch
                        activate_script = f'tell application "{app}" to activate'
                        subprocess.Popen(['osascript', '-e', activate_script])
                        return
                    elif app_lower == 'ghostty':
                        cmd = ['open', '-na', app, '--args', '-e', ssh_command]
                        # Launch terminal and then activate it
                        subprocess.Popen(cmd, start_new_session=True)
                        time.sleep(0.5)  # Give the app time to launch
                        activate_script = f'tell application "{app}" to activate'
                        subprocess.Popen(['osascript', '-e', activate_script])
                        return
                    else:
                        cmd = ['open', '-a', app, '--args', 'bash', '-lc', f'{ssh_command}; exec bash']
                        # Launch terminal and then activate it
                        subprocess.Popen(cmd, start_new_session=True)
                        time.sleep(0.5)  # Give the app time to launch
                        activate_script = f'tell application "{app}" to activate'
                        subprocess.Popen(['osascript', '-e', activate_script])
                        return
                else:
                    cmd = terminal_command + ['--args', 'bash', '-lc', f'{ssh_command}; exec bash']
            else:
                terminal_basename = os.path.basename(terminal_command[0])
                if terminal_basename == 'ptyxis':
                    # Use --standalone with -- to start fresh instance with only our command
                    # This prevents opening a default window when ptyxis isn't running
                    cmd = terminal_command + ['--standalone', '--', 'bash', '-c', f'{ssh_command}; exec bash']
                elif terminal_basename in ['gnome-terminal', 'tilix', 'xfce4-terminal', 'foot', 'blackbox']:
                    cmd = terminal_command + ['--', 'bash', '-c', f'{ssh_command}; exec bash']
                elif terminal_basename in ['konsole', 'terminator', 'guake']:
                    cmd = terminal_command + ['-e', f'bash -c "{ssh_command}; exec bash"']
                elif terminal_basename in ['alacritty', 'kitty']:
                    cmd = terminal_command + ['-e', 'bash', '-c', f'{ssh_command}; exec bash']
                elif terminal_basename == 'xterm':
                    cmd = terminal_command + ['-e', f'bash -c "{ssh_command}; exec bash"']
                elif terminal_basename == 'xdg-terminal':
                    cmd = terminal_command + [ssh_command]
                elif terminal_basename in ['ghostty']:
                    cmd = terminal_command + ['+new-window', '-e', 'bash', '-c', f'{ssh_command}; exec bash']
                else:
                    cmd = terminal_command + [ssh_command]

            logger.info(f"Launching system terminal: {' '.join(cmd)}")
            subprocess.Popen(cmd, start_new_session=True)
            
            # Try to bring the terminal to front on Linux
            if not is_macos():
                try:
                    # Try wmctrl first (more reliable)
                    result = subprocess.run(['which', 'wmctrl'], capture_output=True, timeout=1)
                    if result.returncode == 0:
                        time.sleep(0.5)  # Give the terminal time to launch
                        terminal_basename = os.path.basename(terminal_command[0])
                        subprocess.Popen(['wmctrl', '-a', terminal_basename], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        # Fallback to xdotool
                        result = subprocess.run(['which', 'xdotool'], capture_output=True, timeout=1)
                        if result.returncode == 0:
                            time.sleep(0.5)  # Give the terminal time to launch
                            terminal_basename = os.path.basename(terminal_command[0])
                            subprocess.Popen(['xdotool', 'search', '--name', terminal_basename, 'windowactivate'], 
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    # Ignore focus errors - terminal launching is more important
                    pass

        except Exception as e:
            logger.error(f"Failed to open system terminal: {e}")
            self._show_terminal_error_dialog()

    def _open_connection_in_external_terminal(self, connection):
        """Open the connection in the user's preferred external terminal"""
        try:
            # Native-only: connect by the ~/.ssh/config host identifier and let
            # the external terminal's ssh read per-host settings from the config.
            host_value = ''
            if hasattr(connection, 'resolve_host_identifier'):
                try:
                    host_value = connection.resolve_host_identifier()
                except Exception:
                    host_value = ''
            if not host_value:
                host_value = _get_connection_host(connection) or _get_connection_alias(connection)

            ssh_command = f"ssh {host_value}" if host_value else "ssh"

            terminal = self._get_user_preferred_terminal()
            if not terminal:
                terminal = self._get_default_terminal_command()

            if not terminal:
                self._show_terminal_error_dialog()
                return

            self._open_system_terminal(terminal, ssh_command)

        except Exception as e:
            logger.error(f"Failed to open connection in external terminal: {e}")
            self._show_terminal_error_dialog()

    def _get_default_terminal_command(self) -> Optional[List[str]]:
        """Get the default terminal command from desktop environment"""
        try:
            if is_macos():
                # Map bundle identifiers to display names in preference order.
                mac_terms = {
                    'com.apple.Terminal': 'Terminal',
                    'com.googlecode.iterm2': 'iTerm',

                    'dev.warp.Warp': 'Warp',
                    'io.alacritty': 'Alacritty',
                    'net.kovidgoyal.kitty': 'Kitty',
                    'com.mitmaro.ghostty': 'Ghostty',
                }

                for bundle_id, name in mac_terms.items():
                    # First try AppleScript lookup by app name
                    try:
                        result = subprocess.run(
                            ['osascript', '-e', f'id of app "{name}"'],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        )
                        if result.returncode == 0 and bundle_id in result.stdout:
                            return ['open', '-a', name]
                    except Exception:
                        pass

                    # Fallback to Spotlight metadata search by bundle identifier
                    try:
                        result = subprocess.run(
                            ['mdfind', f'kMDItemCFBundleIdentifier=={bundle_id}'],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            return ['open', '-a', name]
                    except Exception:
                        pass

                return None

            desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()

            if 'gnome' in desktop:
                # Try gnome-terminal first, then ptyxis as fallback
                try:
                    result = subprocess.run(['which', 'gnome-terminal'], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        return ['gnome-terminal']
                except Exception:
                    pass
                # Try ptyxis if gnome-terminal is not available
                try:
                    result = subprocess.run(['which', 'ptyxis'], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        return ['ptyxis']
                except Exception:
                    pass
                return None
            elif 'kde' in desktop or 'plasma' in desktop:
                return ['konsole']
            elif 'xfce' in desktop:
                return ['xfce4-terminal']
            elif 'cinnamon' in desktop:
                return ['gnome-terminal']
            elif 'mate' in desktop:
                return ['mate-terminal']
            elif 'lxqt' in desktop:
                return ['qterminal']
            elif 'lxde' in desktop:
                return ['lxterminal']

            common_terminals = [
                'gnome-terminal', 'ptyxis', 'konsole', 'xfce4-terminal', 'alacritty',
                'kitty', 'terminator', 'tilix', 'guake'
            ]

            for term in common_terminals:
                try:
                    result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        return [term]
                except Exception:
                    continue

            return None

        except Exception as e:
            logger.error(f"Failed to get default terminal: {e}")
            return None
    
    def _get_user_preferred_terminal(self) -> Optional[List[str]]:
        """Get the user's preferred terminal from settings"""
        try:
            preferred_terminal = self.config.get_setting('external-terminal', 'gnome-terminal')

            if preferred_terminal == 'custom':
                custom_path = self.config.get_setting('custom-terminal-path', '')
                if custom_path:
                    if is_macos():
                        return ['open', '-a', custom_path]
                    return [custom_path]
                else:
                    logger.warning("Custom terminal path is not set, falling back to built-in terminal")
                    return None

            if is_macos():
                # Preferences may store either an app name ("iTerm") or a full
                # command ("open -a iTerm").  If the value already starts with
                # "open" use it verbatim, otherwise build an "open -a" command
                # for the specified app.
                if preferred_terminal.startswith('open'):
                    return shlex.split(preferred_terminal)

                return ['open', '-a', preferred_terminal]

            try:
                result = subprocess.run(['which', preferred_terminal], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    return [preferred_terminal]
                else:
                    logger.warning(f"Preferred terminal '{preferred_terminal}' not found, falling back to built-in terminal")
                    return None
            except Exception as e:
                logger.error(f"Failed to check preferred terminal '{preferred_terminal}': {e}")
                return None

        except Exception as e:
            logger.error(f"Failed to get user preferred terminal: {e}")
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

    def _do_quit(self):
        """Actually quit the application - FINAL STEP"""
        try:
            logger.info("Quitting application")
            
            # Close all file manager windows first
            if hasattr(self, '_internal_file_manager_windows'):
                count = len(self._internal_file_manager_windows)
                logger.info(f"Closing {count} file manager windows")
                if count > 0:
                    for window in list(self._internal_file_manager_windows):
                        try:
                            logger.info(f"Closing file manager window: {window}")
                            # Try to clean up the manager first
                            if hasattr(window, '_manager') and window._manager is not None:
                                logger.info("Closing file manager backend in file manager window")
                                window._manager.close()
                                window._manager = None
                                logger.info("File manager backend closed")
                            # Close the window
                            if hasattr(window, 'close'):
                                window.close()
                            elif hasattr(window, 'destroy'):
                                window.destroy()
                        except Exception as exc:
                            logger.error(f"Error closing file manager window: {exc}", exc_info=True)
                self._internal_file_manager_windows.clear()
                logger.info("All file manager windows closed")
            
            # Also check all application windows for file manager windows (defensive)
            try:
                app = self.get_application()
                if app:
                    all_windows = list(app.get_windows())
                    for window in all_windows:
                        # Check if it's a file manager window that wasn't tracked
                        if hasattr(window, '_manager') and hasattr(window, '_on_close_request'):
                            if not hasattr(self, '_internal_file_manager_windows') or window not in self._internal_file_manager_windows:
                                logger.info(f"Found untracked file manager window, closing: {window}")
                                try:
                                    if hasattr(window, '_manager') and window._manager is not None:
                                        logger.info("Closing file manager backend in untracked window")
                                        window._manager.close()
                                        window._manager = None
                                except Exception as exc:
                                    logger.error(f"Error closing untracked file manager window: {exc}", exc_info=True)
            except Exception as exc:
                logger.debug(f"Error checking for untracked file manager windows: {exc}")
            
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
    
    def _update_tab_titles(self):
        """Update tab titles"""
        for page in self.tab_view.get_pages():
            if self._is_start_tab_page(page):
                continue
            child = page.get_child()
            if hasattr(child, 'connection'):
                page.set_title(child.connection.nickname)
    
    def _on_plugin_connection_saved(self, dialog, connection_data):
        """Persist a plugin-protocol connection (JSON store, no ssh_config)."""
        if dialog.is_editing and dialog.connection is not None:
            old_connection = dialog.connection
            original_nickname = old_connection.nickname
            if not self.connection_manager.update_connection(old_connection, connection_data):
                logger.error("Failed to update plugin connection")
                return
            new_nickname = connection_data.get('nickname') or original_nickname
            if original_nickname != new_nickname:
                try:
                    self.group_manager.rename_connection(original_nickname, new_nickname)
                except Exception:
                    pass
                try:
                    old_meta = self.config.get_connection_meta(original_nickname)
                    if old_meta:
                        new_meta = self.config.get_connection_meta(new_nickname)
                        merged = {**old_meta, **new_meta}
                        meta_all = self.config.get_setting('connections_meta', {}) or {}
                        meta_all.pop(original_nickname, None)
                        self.config.set_setting('connections_meta', meta_all)
                        self.config.set_connection_meta(new_nickname, merged)
                except Exception:
                    logger.debug("Failed to migrate connection meta on rename", exc_info=True)
            try:
                old_connection.tags = self.config.get_connection_tags(old_connection.nickname)
            except Exception:
                pass
            rows = self._rows_for_connection(old_connection)
            if rows:
                for row in rows:
                    row.update_display()
            else:
                self.rebuild_connection_list()
            logger.info(f"Updated plugin connection: {old_connection.nickname}")
        else:
            connection = Connection(connection_data)
            if self.connection_manager.update_connection(connection, connection_data):
                self.rebuild_connection_list()
                logger.info(f"Created new plugin connection: {connection_data['nickname']}")
            else:
                logger.error("Failed to save plugin connection")

    def on_connection_saved(self, dialog, connection_data):
        """Handle connection saved from dialog"""
        save_completion = connection_data.pop('__save_completion', None)

        def _complete_save(ok):
            if callable(save_completion):
                save_completion(bool(ok))

        try:
            if connection_data.get('protocol', 'ssh') != 'ssh':
                self._on_plugin_connection_saved(dialog, connection_data)
                return
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
                    'hostname': _norm_str(getattr(old_connection, 'hostname', getattr(old_connection, 'host', ''))),
                    'username': _norm_str(getattr(old_connection, 'username', '')),
                    'port': int(getattr(old_connection, 'port', 22) or 22),
                    'auth_method': int(getattr(old_connection, 'auth_method', 0) or 0),
                    'keyfile': _norm_str(getattr(old_connection, 'keyfile', '')),
                    'certificate': _norm_str(getattr(old_connection, 'certificate', '')),
                    'key_select_mode': int(getattr(old_connection, 'key_select_mode', 0) or 0),
                    'password': _norm_str(getattr(old_connection, 'password', '')),
                    'key_passphrase': _norm_str(getattr(old_connection, 'key_passphrase', '')),
                    'x11_forwarding': bool(getattr(old_connection, 'x11_forwarding', False)),
                    'forwarding_rules': _norm_rules(getattr(old_connection, 'forwarding_rules', [])),
                    'pre_command': _norm_str(getattr(old_connection, 'pre_command', '') or (getattr(old_connection, 'data', {}).get('pre_command') if hasattr(old_connection, 'data') else '')),
                    'local_command': _norm_str(getattr(old_connection, 'local_command', '') or (getattr(old_connection, 'data', {}).get('local_command') if hasattr(old_connection, 'data') else '')),
                    'remote_command': _norm_str(getattr(old_connection, 'remote_command', '') or (getattr(old_connection, 'data', {}).get('remote_command') if hasattr(old_connection, 'data') else '')),
                    'extra_ssh_config': _norm_str(getattr(old_connection, 'extra_ssh_config', '') or (getattr(old_connection, 'data', {}).get('extra_ssh_config') if hasattr(old_connection, 'data') else '')),
                }
                # Editing always forces an update so forwarding rules stay synced;
                # change detection was intentionally removed (it could skip needed
                # updates), so no diff between old and new values is computed here.
                # Always force update when editing connections - skip change detection entirely for forwarding rules
                logger.info("Editing connection '%s' - forcing update to ensure forwarding rules are synced", existing['nickname'])

                logger.debug(f"Updating connection '{old_connection.nickname}'")

                # Ensure auth_method always present and normalized
                try:
                    connection_data['auth_method'] = int(connection_data.get('auth_method', getattr(old_connection, 'auth_method', 0)) or 0)
                except Exception:
                    connection_data['auth_method'] = 0

                original_nickname = old_connection.nickname

                # Update connection in manager first
                if not self.connection_manager.update_connection(old_connection, connection_data):
                    logger.error("Failed to update connection in SSH config")
                    _complete_save(False)
                    return

                # Preserve group assignment if nickname changed
                new_nickname = connection_data['nickname']
                if original_nickname != new_nickname:
                    try:
                        self.group_manager.rename_connection(original_nickname, new_nickname)
                    except Exception:
                        pass
                    # Migrate per-connection metadata (pinned, WoL, tags) to the
                    # new nickname. The dialog already wrote its own fields under
                    # the new key, so those win over the old entry's values.
                    try:
                        old_meta = self.config.get_connection_meta(original_nickname)
                        if old_meta:
                            new_meta = self.config.get_connection_meta(new_nickname)
                            merged = {**old_meta, **new_meta}
                            meta_all = self.config.get_setting('connections_meta', {}) or {}
                            meta_all.pop(original_nickname, None)
                            self.config.set_setting('connections_meta', meta_all)
                            self.config.set_connection_meta(new_nickname, merged)
                    except Exception:
                        logger.debug("Failed to migrate connection meta on rename", exc_info=True)

                # Update connection attributes in memory (ensure forwarding rules kept)
                old_connection.nickname = connection_data['nickname']
                old_connection.hostname = connection_data['hostname']
                old_connection.host = old_connection.hostname
                old_connection.username = connection_data['username']
                old_connection.port = connection_data['port']
                old_connection.keyfile = connection_data['keyfile']
                old_connection.certificate = connection_data.get('certificate', '')
                old_connection.password = connection_data['password']
                old_connection.key_passphrase = connection_data.get('key_passphrase', getattr(old_connection, 'key_passphrase', '')) or ''
                old_connection.auth_method = connection_data['auth_method']
                # Persist key selection mode in-memory so the dialog reflects it without restart
                try:
                    old_connection.key_select_mode = int(connection_data.get('key_select_mode', getattr(old_connection, 'key_select_mode', 0)) or 0)
                except Exception:
                    pass
                old_connection.x11_forwarding = connection_data['x11_forwarding']
                old_connection.forwarding_rules = list(connection_data.get('forwarding_rules', []))
                # Ensure proxy settings are refreshed in-memory so new connections
                # immediately pick up the updated directives without needing a
                # full application restart. The connection manager updates the
                # serialized data, but the active Connection instance used by
                # terminals/file manager must also reflect the new values.
                try:
                    proxy_jump_value = connection_data.get('proxy_jump', [])
                    if isinstance(proxy_jump_value, str):
                        proxy_jump_value = [
                            h.strip() for h in re.split(r'[\s,]+', proxy_jump_value) if h.strip()
                        ]
                    else:
                        proxy_jump_value = [
                            str(h).strip() for h in (proxy_jump_value or []) if str(h).strip()
                        ]
                    old_connection.proxy_jump = proxy_jump_value
                except Exception:
                    proxy_jump_value = []
                    old_connection.proxy_jump = []

                proxy_command_value = connection_data.get('proxy_command', '') or ''
                forward_agent_value = bool(connection_data.get('forward_agent', False))

                old_connection.proxy_command = proxy_command_value
                old_connection.forward_agent = forward_agent_value

                # Keep the backing data dict synchronized so any downstream
                # consumers that still read from connection.data see the new
                # directives without waiting for another reload cycle.
                try:
                    if hasattr(old_connection, 'data') and isinstance(old_connection.data, dict):
                        old_connection.data['proxy_jump'] = list(proxy_jump_value)
                        old_connection.data['proxy_command'] = proxy_command_value
                        old_connection.data['forward_agent'] = forward_agent_value
                except Exception:
                    pass

                # Invalidate any prepared SSH command so future connection
                # attempts rebuild the argument list using the refreshed proxy
                # settings. Otherwise terminals reuse the cached command and
                # continue using the previous ProxyJump chain until restart.
                try:
                    if hasattr(old_connection, 'ssh_cmd'):
                        old_connection.ssh_cmd = []
                except Exception:
                    try:
                        delattr(old_connection, 'ssh_cmd')
                    except Exception:
                        pass

                # Update commands
                try:
                    old_connection.pre_command = connection_data.get('pre_command', '')
                    old_connection.local_command = connection_data.get('local_command', '')
                    old_connection.remote_command = connection_data.get('remote_command', '')
                    old_connection.extra_ssh_config = connection_data.get('extra_ssh_config', '')
                except Exception:
                    pass
                
                # The connection has already been updated in-place, so we don't need to reload from disk
                # The forwarding rules are already updated in the connection_data
                


                # Update UI. The tag-filtered list is derived during rebuilds,
                # so a tag change needs a full rebuild while a tag filter is
                # active — update_display() alone leaves it stale.
                tags_changed = False
                try:
                    fresh_tags = self.config.get_connection_tags(old_connection.nickname)
                    if list(getattr(old_connection, 'tags', None) or []) != fresh_tags:
                        tags_changed = True
                    old_connection.tags = fresh_tags
                except Exception:
                    pass
                rows = self._rows_for_connection(old_connection)
                if tags_changed and getattr(self, '_tag_filter', None):
                    self.rebuild_connection_list()
                elif rows:
                    # Update the display for every row representing this connection
                    for row in rows:
                        row.update_display()
                else:
                    # If the connection is not in the rows, rebuild the list
                    self.rebuild_connection_list()
                
                logger.info(f"Updated connection: {old_connection.nickname}")
                
                # If the connection is active, ask if user wants to reconnect
                if is_connected and terminal is not None:
                    # Store the terminal in the connection for later use
                    old_connection._terminal_instance = terminal
                    self._prompt_reconnect(old_connection)
                _complete_save(True)
                
            else:
                # Create new connection
                connection = Connection(connection_data)
                if self.connection_manager.isolated_mode:
                    connection.isolated_config = True
                    connection.config_root = self.connection_manager.ssh_config_path
                    connection.data['isolated_mode'] = True
                    if self.connection_manager.ssh_config_path:
                        connection.data['config_root'] = self.connection_manager.ssh_config_path
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
                # Ensure certificate is applied immediately
                try:
                    connection.certificate = connection_data.get('certificate', '')
                except Exception:
                    connection.certificate = ''
                # Ensure extra SSH config settings are applied immediately
                try:
                    connection.extra_ssh_config = connection_data.get('extra_ssh_config', '')
                except Exception:
                    connection.extra_ssh_config = ''
                # Add the new connection to the manager's connections list
                self.connection_manager.connections.append(connection)
                

                
                # Save the connection to SSH config and emit the connection-added signal
                if self.connection_manager.update_connection(connection, connection_data):
                    # Reload from SSH config and rebuild list immediately
                    try:
                        self.connection_manager.load_ssh_config()
                        self.rebuild_connection_list()
                    except Exception:
                        pass
                    # Reload config after saving
                    try:

                        self.connection_manager.load_ssh_config()
                        self.rebuild_connection_list()

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
                    _complete_save(True)
                else:
                    logger.error("Failed to save connection to SSH config")
                    _complete_save(False)
                
        except Exception as e:
            logger.error(f"Failed to save connection: {e}")
            _complete_save(False)
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
            self.rebuild_connection_list()
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

        # Ensure the tab for this connection is focused so the user can
        # observe the reconnection process even if another tab was
        # previously active.
        try:
            self._focus_most_recent_tab(connection)
        except Exception:
            pass

        # Set controlled reconnect flag
        self._is_controlled_reconnect = True


        
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

            # Rebuild the SSH command using the latest configuration so that
            # options resolved via ssh -G are honored for the reconnect.
            # Plugin protocols rebuild statelessly in build_spawn() instead.
            if getattr(connection, 'protocol', 'ssh') == 'ssh':
                try:
                    loop = asyncio.get_event_loop()
                    # Native-only (connect() delegates to native_connect()).
                    if hasattr(connection, 'native_connect'):
                        connect_coro = connection.native_connect()
                    else:
                        connect_coro = connection.connect()
                    if loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(connect_coro, loop)
                        future.result()
                    else:
                        loop.run_until_complete(connect_coro)
                except Exception as prep_err:
                    logger.error(
                        "Failed to prepare SSH command before reconnect: %s",
                        prep_err,
                    )
                    GLib.idle_add(self._show_reconnect_error, connection, str(prep_err))
                    return False

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
        # Remove from active terminals if reconnection fails
        if connection in self.active_terminals:
            del self.active_terminals[connection]
            
        # Update UI to show disconnected state
        for row in self._rows_for_connection(connection):
            row.update_status()
        
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
