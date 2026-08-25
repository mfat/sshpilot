"""GTK-free default application settings.

Plain dict defaults independent of Gio.Settings / GObject / widgets.
Translations and UI state do not belong here.
"""
from __future__ import annotations

from typing import Any, Dict

CONFIG_VERSION = 3


def get_default_config() -> Dict[str, Any]:
    """Return the canonical default configuration tree."""
    return {
        'config_version': CONFIG_VERSION,
        'shortcuts': {},
        # Built-in plugins that are off by default on a fresh install (the
        # user can enable them in Preferences ▸ Plugins). Only seeded when the
        # config file is first created; existing configs are left untouched.
        'plugins': {
            'disabled': ['docker-manager'],
        },
        'terminal': {
            'theme': 'default',
            'font': 'Monospace 12',
            'scrollback_lines': 10000,
            'cursor_blink': True,
            'audible_bell': False,
            'term': None,
            'pass_through_mode': False,
            'copy_on_select': False,
            'paste_on_right_click': False,
            'encoding': 'UTF-8',
            'macos_option_key_passthrough': False,
        },
        'secrets': {
            # Secret storage backend: 'auto' (platform default), 'libsecret',
            # 'keyring', 'pass', 'bitwarden' (the bw CLI; covers self-hosted
            # Vaultwarden too), 'agent' ('agent' = don't store secrets), or a
            # registered custom backend. (Legacy 'vaultwarden' migrates to
            # 'bitwarden'.)
            'backend': 'auto',
            # Session-backed backend (Bitwarden, incl. self-hosted Vaultwarden):
            # minutes of idle before the cached unlock token is dropped and
            # re-unlock is required. 0 = keep until the app exits.
            'session_timeout': 0,
            # Bitwarden CLI account/profile: a path to a `bw` data directory
            # (BITWARDENCLI_APPDATA_DIR). Empty = the default account. Use a
            # separate data dir per account (e.g. a self-hosted Vaultwarden).
            'bitwarden': {
                'profile': '',
            },
            # KeePass (.kdbx) backend: path to the database file and an optional key
            # file. The master password is typed per launch (kept in memory).
            'keepassxc': {
                'database': '',
                'keyfile': '',
            },
        },
        'identity': {
            # Default SSH agent offered to connections. 'auto' = the OS/desktop
            # ssh-agent (inherited via SSH_AUTH_SOCK). A fixed-socket agent
            # (e.g. '1password', or 'custom') is written as a global `Host *`
            # IdentityAgent directive to ~/.ssh/config. The per-connection key is
            # set via IdentityFile, not here.
            'provider': 'auto',
            # Socket path for the 'custom' agent (written as IdentityAgent).
            'agent_socket': '',
        },
        'ui': {
            # Interface language as a locale code ('de'). Empty = follow the
            # system locale. Applied at startup by i18n.apply_language();
            # gettext caches its catalogue, so a change needs a restart.
            'language': '',
            'show_hostname': True,
            'auto_focus_terminal': True,
            'confirm_close_tabs': True,
            'remember_window_size': True,
            'window_width': 1200,
            'window_height': 800,
            'sidebar_width': 250,
            'group_color_display': 'bar',
            'group_color_child_rows': False,
            'group_row_display': 'nested',
            'use_group_color_in_tab': False,
            'use_group_color_in_terminal': False,
            'sidebar_show_user_hostname': True,
            'sidebar_show_group_count': True,
            'sidebar_show_connection_status': True,
            'sidebar_show_port_forwarding': True,
            'sidebar_show_connection_icon': False,
            'sidebar_show_group_icon': False,
            'sidebar_flat_rows': True,
            # Sidebar behavior (Settings ▸ Sidebar ▸ Sidebar behavior)
            'sidebar_hide_on_startup': False,
            'sidebar_hide_on_terminal_open': False,  # legacy; see sidebar_on_terminal_open
            'sidebar_show_when_no_tabs': False,
            'sidebar_mode': 'full',  # 'full' | 'minimal' (icon strip)
            # What happens to the sidebar when a session opens:
            # 'none' | 'minimize' (icon strip) | 'hide'.
            'sidebar_on_terminal_open': 'none',
            'sidebar_minimal_row_style': 'initials',  # 'initials' | 'icon'
            # Header-bar button visibility (Settings ▸ Interface ▸ Header Bar)
            'headerbar_show_sidebar_toggle': False,
            'headerbar_show_split_view': False,
            'headerbar_show_commands': True,
            'headerbar_show_terminal_theme': True,
            'headerbar_show_theme_toggle': False,
            'headerbar_show_local_terminal': True,
        },
        'welcome': {
            'background_color': None,  # None for default, or CSS string for custom
            'tile_color': None,  # None for default, or hex color for custom
        },
        'connections_meta': {},  # per-connection metadata
        'ssh': {
            'compression': False,
            'auto_add_host_keys': True,
            'batch_mode': False,
            'verbosity': 0,
            'debug_enabled': False,
            'use_isolated_config': False,
            'ssh_overrides': [],
            'strict_host_key_checking': 'accept-new',
            # Global SSH override fields (daemon-owned). 0 disables the
            # corresponding ``-o`` option (matching compose_ssh_overrides).
            'connection_timeout': 0,
            'connection_attempts': 0,
            'keepalive_interval': 0,
            'keepalive_count_max': 0,
            # When the user hasn't configured keepalive (here or in
            # ~/.ssh/config), apply a sane default ServerAlive* so a dead
            # link is detected (~interval*count seconds) instead of the
            # indicator staying green forever. User/per-host values win.
            'apply_default_keepalive': True,
            'default_keepalive_interval': 15,
            'default_keepalive_count': 3,
            # Preload a host's keyring-backed key(s) into ssh-agent on
            # connect so a passphrased key locked in gnome-keyring gets
            # unlocked and can sign (the agent is never disabled).
            'agent_preload_keys': True,
            'agent_preload_lifetime': 0,  # ssh-add -t <secs>; 0 = no expiry
        },
        'file_manager': {
            'open_externally': False,
            'sftp_keepalive_interval': 30,
            'sftp_keepalive_count_max': 5,
            'sftp_connect_timeout': 20,
            # Icon size step used by the built-in SFTP file manager. Integer
            # in [0, 4]; index into the per-view size tables in
            # file_manager_window.py. Default 1 = list 24px / grid 72px.
            'icon_size_level': 1,
        },
        'security': {
            'store_passwords': True,
            'ssh_agent_forwarding': True,
        },
        'logging': {
            # 'info' (default) or 'debug'. CLI --verbose / --quiet always
            # win over this. Migrated from the legacy ssh.debug_enabled
            # key on first load (see _ensure_config_defaults).
            'level': 'info',
        },
        'command_blocks': {
            'folders': [],
            'commands': [],
            'defaults_loaded': False,
            'view_layout': 'list',
            'auto_hide_sidebar': False,
            'insert_only': False,
        },
    }
