"""
Configuration Manager for sshPilot
Handles application settings, themes, and preferences
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple


from gi.repository import Gio, GLib, GObject
from .platform_utils import get_config_dir

logger = logging.getLogger(__name__)

# Increment this whenever the configuration format changes
CONFIG_VERSION = 3

class Config(GObject.Object):
    """Configuration manager for sshPilot"""
    
    __gsignals__ = {
        'setting-changed': (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }
    
    def __init__(self):
        super().__init__()
        
        # Try to use GSettings ONLY if schema is installed; otherwise use JSON
        self.settings = None
        self.use_gsettings = False
        try:
            schema_id = 'io.github.mfat.sshpilot'
            source = Gio.SettingsSchemaSource.get_default()
            schema = source.lookup(schema_id, True) if source else None
            if schema is not None:
                self.settings = Gio.Settings.new_full(schema, None, None)
                self.use_gsettings = True
                logger.debug("Using GSettings for configuration")
            else:
                logger.debug("GSettings schema not found; using JSON config")
        except Exception as e:
            logger.warning(f"GSettings unavailable; using JSON config: {e}")

        # JSON config is used either as primary or as fallback store
        self.config_file = os.path.join(get_config_dir(), 'config.json')
        self.config_data = self.load_json_config()
        
        # Load built-in themes
        self.terminal_themes = self.load_builtin_themes()
        
        # Connect to settings changes
        if self.use_gsettings:
            self.settings.connect('changed', self.on_setting_changed)

    def load_json_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file) as f:
                    config = json.load(f)

                # Purge outdated configurations
                stored_version = config.get('config_version', 1)
                if stored_version < CONFIG_VERSION:
                    backup_file = f"{self.config_file}.bak"
                    try:
                        os.replace(self.config_file, backup_file)
                        logger.warning(
                            "Outdated config version %s detected; backing up to %s and regenerating defaults",
                            stored_version,
                            backup_file,
                        )
                    except OSError:
                        os.remove(self.config_file)
                        logger.warning(
                            "Outdated config version %s detected; old config removed and new defaults generated",
                            stored_version,
                        )

                    config = self.get_default_config()
                    self.save_json_config(config)
                else:
                    config, updated = self._ensure_config_defaults(config)
                    if updated:
                        self.save_json_config(config)

                return config
            else:
                # Create default config
                default_config = self.get_default_config()
                self.save_json_config(default_config)
                return default_config
        except Exception as e:
            logger.error(f"Failed to load JSON config: {e}")
            return self.get_default_config()

    def save_json_config(self, config_data: Optional[Dict[str, Any]] = None):
        """Save configuration to JSON file"""
        try:
            if config_data is None:
                config_data = self.config_data

            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(config_data, f, indent=2)

            logger.debug("Configuration saved to JSON file")
        except Exception as e:
            logger.error(f"Failed to save JSON config: {e}")

    # Shortcut helpers (get_shortcut_overrides / get_shortcut_override /
    # set_shortcut_override) live further down — the canonical versions read
    # config_data directly and persist + emit change signals.

    def get_default_config(self) -> Dict[str, Any]:
        """Get default configuration values"""
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
                'show_hostname': True,
                'auto_focus_terminal': True,
                'confirm_close_tabs': True,
                'remember_window_size': True,
                'window_width': 1200,
                'window_height': 800,
                'sidebar_width': 250,
                'group_color_display': 'fill',
                'group_row_display': 'nested',
                'use_group_color_in_tab': False,
                'use_group_color_in_terminal': False,
                'connection_sort_last': 'name-asc',
                'sidebar_show_user_hostname': True,
                'sidebar_show_group_count': True,
                'sidebar_show_connection_status': True,
                'sidebar_show_port_forwarding': True,
                'sidebar_show_connection_icon': True,
                'sidebar_flat_rows': False,
                # Sidebar behavior (Settings ▸ Sidebar ▸ Sidebar behavior)
                'sidebar_hide_on_startup': False,
                'sidebar_hide_on_terminal_open': False,  # incl. local terminals
                'sidebar_show_when_no_tabs': False,
                # Header-bar button visibility (Settings ▸ Interface ▸ Header Bar)
                'headerbar_show_split_view': False,
                'headerbar_show_commands': True,
                'headerbar_show_theme_toggle': True,
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
                'force_internal': False,
                'open_externally': False,
                'sftp_keepalive_interval': 30,
                'sftp_keepalive_count_max': 5,
                'sftp_connect_timeout': 20,
                # Icon size step used by the built-in SFTP file manager. Integer
                # in [0, 4]; index into the per-view size tables in
                # file_manager_window.py. Default 1 = list 24px / grid 72px.
                'icon_size_level': 1,
                # Set the first time the user is offered a choice between
                # built-in and system file managers (or skipped because only
                # built-in is available on the current platform). Prevents
                # re-prompting on subsequent "Manage Files" clicks.
                'first_run_prompt_shown': False,
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
                'auto_hide_sidebar': False,
                'insert_only': False,
            },
        }

    def load_builtin_themes(self) -> Dict[str, Dict[str, str]]:
        """Load built-in terminal themes"""
        return {
            'default': {
                'name': 'Default',
                'foreground': '#FFFFFF',
                'background': '#000000',
                'cursor_color': '#FFFFFF',
                'highlight_background': '#4A90E2',
                'highlight_foreground': '#FFFFFF',
                'palette': [
                    '#000000', '#CC0000', '#4E9A06', '#C4A000',
                    '#3465A4', '#75507B', '#06989A', '#D3D7CF',
                    '#555753', '#EF2929', '#8AE234', '#FCE94F',
                    '#729FCF', '#AD7FA8', '#34E2E2', '#EEEEEC'
                ]
            },
            'dark': {
                'name': 'Dark',
                'foreground': '#F8F8F2',
                'background': '#282A36',
                'cursor_color': '#F8F8F2',
                'highlight_background': '#44475A',
                'highlight_foreground': '#F8F8F2',
                'palette': [
                    '#000000', '#FF5555', '#50FA7B', '#F1FA8C',
                    '#BD93F9', '#FF79C6', '#8BE9FD', '#BFBFBF',
                    '#4D4D4D', '#FF6E67', '#5AF78E', '#F4F99D',
                    '#CAA9FA', '#FF92D0', '#9AEDFE', '#E6E6E6'
                ]
            },
            'light': {
                'name': 'Light',
                'foreground': '#2E3436',
                'background': '#FFFFFF',
                'cursor_color': '#2E3436',
                'highlight_background': '#C4E3F3',
                'highlight_foreground': '#2E3436',
                'palette': [
                    '#2E3436', '#CC0000', '#4E9A06', '#C4A000',
                    '#3465A4', '#75507B', '#06989A', '#D3D7CF',
                    '#555753', '#EF2929', '#8AE234', '#FCE94F',
                    '#729FCF', '#AD7FA8', '#34E2E2', '#EEEEEC'
                ]
            },
            'black_on_white': {
                'name': 'Black on White',
                'foreground': '#000000',
                'background': '#FFFFFF',
                'cursor_color': '#000000',
                'highlight_background': '#C4E3F3',
                'highlight_foreground': '#000000',
                'palette': [
                    '#000000', '#CC0000', '#4E9A06', '#C4A000',
                    '#3465A4', '#75507B', '#06989A', '#D3D7CF',
                    '#555753', '#EF2929', '#8AE234', '#FCE94F',
                    '#729FCF', '#AD7FA8', '#34E2E2', '#EEEEEC'
                ]
            },
            'solarized_dark': {
                'name': 'Solarized Dark',
                'foreground': '#839496',
                'background': '#002B36',
                'cursor_color': '#839496',
                'highlight_background': '#073642',
                'highlight_foreground': '#839496',
                'palette': [
                    '#073642', '#DC322F', '#859900', '#B58900',
                    '#268BD2', '#D33682', '#2AA198', '#EEE8D5',
                    '#002B36', '#CB4B16', '#586E75', '#657B83',
                    '#839496', '#6C71C4', '#93A1A1', '#FDF6E3'
                ]
            },
            'solarized_light': {
                'name': 'Solarized Light',
                'foreground': '#657B83',
                'background': '#FDF6E3',
                'cursor_color': '#657B83',
                'highlight_background': '#EEE8D5',
                'highlight_foreground': '#657B83',
                'palette': [
                    '#073642', '#DC322F', '#859900', '#B58900',
                    '#268BD2', '#D33682', '#2AA198', '#EEE8D5',
                    '#002B36', '#CB4B16', '#586E75', '#657B83',
                    '#839496', '#6C71C4', '#93A1A1', '#FDF6E3'
                ]
            },
            'monokai': {
                'name': 'Monokai',
                'foreground': '#F8F8F2',
                'background': '#272822',
                'cursor_color': '#F8F8F0',
                'highlight_background': '#49483E',
                'highlight_foreground': '#F8F8F2',
                'palette': [
                    '#272822', '#F92672', '#A6E22E', '#F4BF75',
                    '#66D9EF', '#AE81FF', '#A1EFE4', '#F8F8F2',
                    '#75715E', '#F92672', '#A6E22E', '#F4BF75',
                    '#66D9EF', '#AE81FF', '#A1EFE4', '#F9F8F5'
                ]
            },
            'dracula': {
                'name': 'Dracula',
                'foreground': '#F8F8F2',
                'background': '#282A36',
                'cursor_color': '#F8F8F0',
                'highlight_background': '#44475A',
                'highlight_foreground': '#F8F8F2',
                'palette': [
                    '#000000', '#FF5555', '#50FA7B', '#F1FA8C',
                    '#BD93F9', '#FF79C6', '#8BE9FD', '#BFBFBF',
                    '#4D4D4D', '#FF6E67', '#5AF78E', '#F4F99D',
                    '#CAA9FA', '#FF92D0', '#9AEDFE', '#E6E6E6'
                ]
            },
            'nord': {
                'name': 'Nord',
                'foreground': '#D8DEE9',
                'background': '#2E3440',
                'cursor_color': '#D8DEE9',
                'highlight_background': '#4C566A',
                'highlight_foreground': '#ECEFF4',
                'palette': [
                    '#3B4252', '#BF616A', '#A3BE8C', '#EBCB8B',
                    '#81A1C1', '#B48EAD', '#88C0D0', '#E5E9F0',
                    '#4C566A', '#BF616A', '#A3BE8C', '#EBCB8B',
                    '#81A1C1', '#B48EAD', '#8FBCBB', '#ECEFF4'
                ]
            },
            # Additional popular themes
            'gruvbox_dark': {
                'name': 'Gruvbox Dark',
                'foreground': '#EBDBB2',
                'background': '#282828',
                'cursor_color': '#EBDBB2',
                'highlight_background': '#3C3836',
                'highlight_foreground': '#EBDBB2',
                'palette': [
                    '#282828', '#CC241D', '#98971A', '#D79921',
                    '#458588', '#B16286', '#689D6A', '#A89984',
                    '#928374', '#FB4934', '#B8BB26', '#FABD2F',
                    '#83A598', '#D3869B', '#8EC07C', '#EBDBB2'
                ]
            },
            'one_dark': {
                'name': 'One Dark',
                'foreground': '#ABB2BF',
                'background': '#282C34',
                'cursor_color': '#528BFF',
                'highlight_background': '#3E4451',
                'highlight_foreground': '#ABB2BF',
                'palette': [
                    '#282C34', '#E06C75', '#98C379', '#E5C07B',
                    '#61AFEF', '#C678DD', '#56B6C2', '#ABB2BF',
                    '#5C6370', '#E06C75', '#98C379', '#E5C07B',
                    '#61AFEF', '#C678DD', '#56B6C2', '#FFFFFF'
                ]
            },
            'tomorrow_night': {
                'name': 'Tomorrow Night',
                'foreground': '#C5C8C6',
                'background': '#1D1F21',
                'cursor_color': '#AEAFAD',
                'highlight_background': '#373B41',
                'highlight_foreground': '#C5C8C6',
                'palette': [
                    '#1D1F21', '#CC6666', '#B5BD68', '#F0C674',
                    '#81A2BE', '#B294BB', '#8ABEB7', '#C5C8C6',
                    '#969896', '#CC6666', '#B5BD68', '#F0C674',
                    '#81A2BE', '#B294BB', '#8ABEB7', '#FFFFFF'
                ]
            },
            'material_dark': {
                'name': 'Material Dark',
                'foreground': '#EEFFFF',
                'background': '#263238',
                'cursor_color': '#FFCC00',
                'highlight_background': '#314549',
                'highlight_foreground': '#EEFFFF',
                'palette': [
                    '#000000', '#FF5370', '#C3E88D', '#FFCB6B',
                    '#82AAFF', '#C792EA', '#89DDFF', '#EEFFFF',
                    '#546E7A', '#FF5370', '#C3E88D', '#FFCB6B',
                    '#82AAFF', '#C792EA', '#89DDFF', '#FFFFFF'
                ]
            },
            'rose_pine': {
                'name': 'Rosé Pine',
                'foreground': '#e0def4',
                'background': '#191724',
                'cursor_color': '#524f67',
                'highlight_background': '#26233a',
                'highlight_foreground': '#e0def4',
                'palette': [
                    '#191724', '#eb6f92', '#31748f', '#f6c177',
                    '#9ccfd8', '#c4a7e7', '#ebbcba', '#e0def4',
                    '#6e6a86', '#eb6f92', '#31748f', '#f6c177',
                    '#9ccfd8', '#c4a7e7', '#ebbcba', '#e0def4'
                ]
            },
            'rose_pine_moon': {
                'name': 'Rosé Pine Moon',
                'foreground': '#e0def4',
                'background': '#232136',
                'cursor_color': '#56526e',
                'highlight_background': '#393552',
                'highlight_foreground': '#e0def4',
                'palette': [
                    '#232136', '#eb6f92', '#3e8fb0', '#f6c177',
                    '#9ccfd8', '#c4a7e7', '#ea9a97', '#e0def4',
                    '#6e6a86', '#eb6f92', '#3e8fb0', '#f6c177',
                    '#9ccfd8', '#c4a7e7', '#ea9a97', '#e0def4'
                ]
            },
            'rose_pine_dawn': {
                'name': 'Rosé Pine Dawn',
                'foreground': '#464261',
                'background': '#faf4ed',
                'cursor_color': '#cecacd',
                'highlight_background': '#f2e9e1',
                'highlight_foreground': '#464261',
                'palette': [
                    '#faf4ed', '#b4637a', '#286983', '#ea9d34',
                    '#56949f', '#907aa9', '#d7827e', '#464261',
                    '#9893a5', '#b4637a', '#286983', '#ea9d34',
                    '#56949f', '#907aa9', '#d7827e', '#464261'
                ]
            },
            'catppuccin_latte': {
                'name': 'Catppuccin Latte',
                'foreground': '#4c4f69',
                'background': '#eff1f5',
                'cursor_color': '#dc8a78',
                'highlight_background': '#ccd0da',
                'highlight_foreground': '#4c4f69',
                'palette': [
                    '#5c5f77', '#d20f39', '#40a02b', '#df8e1d',
                    '#1e66f5', '#8839ef', '#179299', '#acb0be',
                    '#6c6f85', '#d20f39', '#40a02b', '#df8e1d',
                    '#1e66f5', '#8839ef', '#179299', '#bcc0cc'
                ]
            },
            'catppuccin_frappe': {
                'name': 'Catppuccin Frappé',
                'foreground': '#c6d0f5',
                'background': '#303446',
                'cursor_color': '#f2d5cf',
                'highlight_background': '#414559',
                'highlight_foreground': '#c6d0f5',
                'palette': [
                    '#51576d', '#e78284', '#a6d189', '#e5c890',
                    '#8caaee', '#ca9ee6', '#81c8be', '#b5bfe2',
                    '#626880', '#e78284', '#a6d189', '#e5c890',
                    '#8caaee', '#ca9ee6', '#81c8be', '#a5adce'
                ]
            },
            'catppuccin_macchiato': {
                'name': 'Catppuccin Macchiato',
                'foreground': '#cad3f5',
                'background': '#24273a',
                'cursor_color': '#f4dbd6',
                'highlight_background': '#363a4f',
                'highlight_foreground': '#cad3f5',
                'palette': [
                    '#494d64', '#ed8796', '#a6da95', '#eed49f',
                    '#8aadf4', '#c6a0f6', '#8bd5ca', '#b8c0e0',
                    '#5b6078', '#ed8796', '#a6da95', '#eed49f',
                    '#8aadf4', '#c6a0f6', '#8bd5ca', '#a5adcb'
                ]
            },
            'catppuccin_mocha': {
                'name': 'Catppuccin Mocha',
                'foreground': '#cdd6f4',
                'background': '#1e1e2e',
                'cursor_color': '#f5e0dc',
                'highlight_background': '#313244',
                'highlight_foreground': '#cdd6f4',
                'palette': [
                    '#45475a', '#f38ba8', '#a6e3a1', '#f9e2af',
                    '#89b4fa', '#cba6f7', '#94e2d5', '#bac2de',
                    '#585b70', '#f38ba8', '#a6e3a1', '#f9e2af',
                    '#89b4fa', '#cba6f7', '#94e2d5', '#a6adc8'
                ]
            }
        }

    def get_setting(self, key: str, default=None):
        """Get a setting value"""
        try:
            if self.use_gsettings:
                # Convert key format for GSettings
                gsettings_key = key.replace('.', '-')
                # If key exists in schema, use it
                if self.settings.list_keys().__contains__(gsettings_key):
                    return self.settings.get_value(gsettings_key).unpack()
                # Fallback to JSON store for keys outside schema
                # Navigate nested dictionary
                keys = key.split('.')
                value = self.config_data
                for k in keys:
                    if isinstance(value, dict) and k in value:
                        value = value[k]
                    else:
                        return default
                return value
            else:
                # Navigate nested dictionary
                keys = key.split('.')
                value = self.config_data
                for k in keys:
                    if isinstance(value, dict) and k in value:
                        value = value[k]
                    else:
                        return default
                return value
        except Exception as e:
            logger.error(f"Failed to get setting {key}: {e}")
            return default

    def set_setting(self, key: str, value: Any):
        """Set a setting value"""
        try:
            if self.use_gsettings:
                # Convert key format for GSettings
                gsettings_key = key.replace('.', '-')
                if self.settings.list_keys().__contains__(gsettings_key):
                    # Use proper GSettings setter based on Python type
                    try:
                        if isinstance(value, bool):
                            self.settings.set_boolean(gsettings_key, bool(value))
                        elif isinstance(value, int) and not isinstance(value, bool):
                            # bool is subclass of int; ensure pure int here
                            self.settings.set_int(gsettings_key, int(value))
                        elif isinstance(value, float):
                            try:
                                self.settings.set_double(gsettings_key, float(value))
                            except Exception:
                                # Fallback to string if schema type is not double
                                self.settings.set_string(gsettings_key, str(value))
                        elif isinstance(value, str):
                            self.settings.set_string(gsettings_key, value)
                        else:
                            # Fallback: try to coerce to the existing key's variant type
                            try:
                                current_variant = self.settings.get_value(gsettings_key)
                                variant_type = current_variant.get_type_string()
                                self.settings.set_value(gsettings_key, GLib.Variant(variant_type, value))
                            except Exception:
                                # Last resort: store as string
                                self.settings.set_string(gsettings_key, str(value))
                    except Exception:
                        # If anything goes wrong, fall back to storing in JSON config
                        keys = key.split('.')
                        current = self.config_data
                        for k in keys[:-1]:
                            if k not in current or not isinstance(current[k], dict):
                                current[k] = {}
                            current = current[k]
                        current[keys[-1]] = value
                        self.save_json_config()
                else:
                    # Fallback to JSON store when key not present in schema
                    keys = key.split('.')
                    current = self.config_data
                    for k in keys[:-1]:
                        if k not in current or not isinstance(current[k], dict):
                            current[k] = {}
                        current = current[k]
                    current[keys[-1]] = value
                    self.save_json_config()
            else:
                # Navigate nested dictionary and set value (pure JSON mode)
                keys = key.split('.')
                current = self.config_data
                for k in keys[:-1]:
                    if k not in current:
                        current[k] = {}
                    current = current[k]
                current[keys[-1]] = value
                self.save_json_config()
            
            # Emit signal
            self.emit('setting-changed', key, value)
            
            logger.debug(f"Setting {key} = {value}")
            
        except Exception as e:
            logger.error(f"Failed to set setting {key}: {e}")

    def on_setting_changed(self, settings, key):
        """Handle GSettings change"""
        value = settings.get_value(key).unpack()
        # Convert key format back
        config_key = key.replace('-', '.')
        self.emit('setting-changed', config_key, value)

    def get_terminal_profile(self, theme_name: Optional[str] = None) -> Dict[str, str]:
        """Get terminal theme profile"""
        if theme_name is None:
            theme_name = self.get_setting('terminal.theme', 'default')
        
        if theme_name in self.terminal_themes:
            theme = self.terminal_themes[theme_name].copy()
        else:
            logger.warning(f"Theme {theme_name} not found, using default")
            theme = self.terminal_themes['default'].copy()
        
        # Add font setting
        theme['font'] = self.get_setting('terminal.font', 'Monospace 12')
        
        return theme

    def get_available_themes(self) -> Dict[str, str]:
        """Get list of available themes"""
        return {name: theme['name'] for name, theme in self.terminal_themes.items()}

    # --- Per-connection metadata helpers ---
    def get_connection_meta(self, key: str) -> Dict[str, Any]:
        """Return stored metadata for a connection keyed by nickname (or unique key)."""
        try:
            meta_all = self.get_setting('connections_meta', {})
            if isinstance(meta_all, dict):
                value = meta_all.get(key, {})
                return value if isinstance(value, dict) else {}
        except Exception:
            pass
        return {}

    def set_connection_meta(self, key: str, meta: Dict[str, Any]):
        """Store metadata for a connection."""
        try:
            meta_all = self.get_setting('connections_meta', {})
            if not isinstance(meta_all, dict):
                meta_all = {}
            meta_all[key] = meta or {}
            self.set_setting('connections_meta', meta_all)
        except Exception:
            logger.error(f"Failed to persist connection meta for {key}")

    def pin_connection(self, nickname: str) -> None:
        """Mark a connection as pinned to the start page."""
        meta = self.get_connection_meta(nickname)
        meta['pinned'] = True
        self.set_connection_meta(nickname, meta)

    def unpin_connection(self, nickname: str) -> None:
        """Remove a connection's pinned flag."""
        meta = self.get_connection_meta(nickname)
        meta.pop('pinned', None)
        self.set_connection_meta(nickname, meta)

    def is_pinned(self, nickname: str) -> bool:
        """Return True if the connection is pinned to the start page."""
        return bool(self.get_connection_meta(nickname).get('pinned', False))

    def get_pinned_nicknames(self) -> list:
        """Return a list of nicknames that are currently pinned."""
        try:
            meta_all = self.get_setting('connections_meta', {})
            if not isinstance(meta_all, dict):
                return []
            return [k for k, v in meta_all.items() if isinstance(v, dict) and v.get('pinned')]
        except Exception:
            return []

    def get_connection_tags(self, nickname: str) -> list:
        """Return the list of string tags for a connection (empty if none)."""
        tags = self.get_connection_meta(nickname).get('tags', [])
        if isinstance(tags, list):
            return [str(t).strip() for t in tags if str(t).strip()]
        return []

    def set_connection_tags(self, nickname: str, tags: list) -> None:
        """Persist tags for a connection; removes the key when empty."""
        meta = self.get_connection_meta(nickname)
        cleaned = [str(t).strip() for t in (tags or []) if str(t).strip()]
        if cleaned:
            meta['tags'] = cleaned
        else:
            meta.pop('tags', None)
        self.set_connection_meta(nickname, meta)

    def get_all_tags(self) -> list:
        """Return all distinct tags across connections with usage counts.

        Case-insensitive merge (first-seen casing wins), sorted alphabetically.
        Returns a list of (display_tag, connection_count) tuples.
        """
        from .tag_groups import compute_tag_groups

        tag_map = {}
        meta_all = self.get_setting('connections_meta', {})
        if isinstance(meta_all, dict):
            for nickname, meta in meta_all.items():
                if isinstance(meta, dict) and isinstance(meta.get('tags'), list):
                    tag_map[nickname] = meta['tags']
        return [(tag, len(nicks)) for tag, nicks in compute_tag_groups(tag_map)]

    def rename_tag(self, old_tag: str, new_tag: str) -> int:
        """Rename *old_tag* to *new_tag* on every connection.

        Matches case-insensitively and de-duplicates per connection (renaming
        onto an existing tag merges). Returns the number of connections
        changed.
        """
        from .tag_groups import rename_tag_in_list

        old_key = str(old_tag).casefold()
        new_name = str(new_tag).strip()
        if not new_name:
            return 0
        changed_count = 0
        meta_all = self.get_setting('connections_meta', {})
        if not isinstance(meta_all, dict):
            return 0
        for nickname, meta in list(meta_all.items()):
            if not isinstance(meta, dict):
                continue
            tags = meta.get('tags')
            if not isinstance(tags, list) or not tags:
                continue
            new_tags, changed = rename_tag_in_list(tags, old_key, new_name)
            if changed:
                self.set_connection_tags(nickname, new_tags)
                changed_count += 1
        return changed_count

    def add_custom_theme(self, name: str, theme_data: Dict[str, str]):
        """Add a custom theme"""
        self.terminal_themes[name] = theme_data
        
        # Save custom themes to config
        custom_themes = self.get_setting('terminal.custom_themes', {})
        custom_themes[name] = theme_data
        self.set_setting('terminal.custom_themes', custom_themes)
        
        logger.info(f"Added custom theme: {name}")

    def remove_custom_theme(self, name: str):
        """Remove a custom theme"""
        if name in self.terminal_themes and name not in ['default', 'dark', 'light', 'black_on_white', 'solarized_dark', 'solarized_light', 'monokai', 'dracula', 'nord', 'gruvbox_dark', 'one_dark', 'tomorrow_night', 'material_dark', 'rose_pine', 'rose_pine_moon', 'rose_pine_dawn', 'catppuccin_latte', 'catppuccin_frappe', 'catppuccin_macchiato', 'catppuccin_mocha']:
            del self.terminal_themes[name]
            
            # Remove from config
            custom_themes = self.get_setting('terminal.custom_themes', {})
            if name in custom_themes:
                del custom_themes[name]
                self.set_setting('terminal.custom_themes', custom_themes)
            
            logger.info(f"Removed custom theme: {name}")

    def get_window_geometry(self) -> Dict[str, int]:
        """Get saved window geometry"""
        return {
            'width': self.get_setting('ui.window_width', 1200),
            'height': self.get_setting('ui.window_height', 800),
            'sidebar_width': self.get_setting('ui.sidebar_width', 250),
        }

    def save_window_geometry(self, width: int, height: int, sidebar_width: Optional[int] = None):
        """Save window geometry"""
        if self.get_setting('ui.remember_window_size', True):
            self.set_setting('ui.window_width', width)
            self.set_setting('ui.window_height', height)
            if sidebar_width is not None:
                self.set_setting('ui.sidebar_width', sidebar_width)

    def get_ssh_config(self) -> Dict[str, Any]:
        """Get SSH configuration values with sensible defaults.

        All advanced options persisted under the ``ssh.`` namespace are
        returned so that downstream builders (terminal, file manager, command
        helpers, etc.) can honour the user's preferences.
        """

        defaults: Dict[str, Any] = {
            'auto_add_host_keys': True,
            'batch_mode': False,
            'compression': False,
            'debug_enabled': False,
            'strict_host_key_checking': 'accept-new',
            'use_isolated_config': False,
            'verbosity': 0,
            'ssh_overrides': [],
            'apply_default_keepalive': True,
            'default_keepalive_interval': 15,
            'default_keepalive_count': 3,
        }

        optional_int_keys = {
            'connection_attempts',
            'connection_timeout',
            'keepalive_count_max',
            'keepalive_interval',
        }

        # Internal keepalive defaults applied when the user hasn't set their own
        # keepalive. Always present (non-optional) so the builder can rely on them.
        positive_int_keys = {
            'default_keepalive_interval',
            'default_keepalive_count',
        }

        bool_keys = {
            'auto_add_host_keys',
            'batch_mode',
            'compression',
            'debug_enabled',
            'use_isolated_config',
            'apply_default_keepalive',
        }

        config: Dict[str, Any] = {}

        for key, default_value in defaults.items():
            value = self.get_setting(f'ssh.{key}', default_value)

            if key in bool_keys:
                if isinstance(value, bool):
                    pass
                elif isinstance(value, str):
                    lowered = value.strip().lower()
                    value = lowered in {'1', 'true', 'yes', 'on'}
                else:
                    value = bool(value)
            elif key in optional_int_keys:
                # handled separately after defaults loop
                pass
            elif key in positive_int_keys:
                try:
                    coerced_int = int(value)
                except (TypeError, ValueError):
                    coerced_int = int(default_value)
                value = coerced_int if coerced_int > 0 else int(default_value)
            elif key == 'strict_host_key_checking':
                if value is None:
                    value = default_value
                else:
                    strict_value = str(value).strip()
                    if not strict_value:
                        value = ''
                    else:
                        normalized = strict_value.lower()
                        if normalized in {'accept-new', 'yes', 'no', 'ask'}:
                            value = 'accept-new' if normalized == 'accept-new' else normalized
                        else:
                            value = default_value
            elif key == 'ssh_overrides':
                if isinstance(value, (list, tuple)):
                    coerced: List[str] = []
                    for entry in value:
                        if entry is None:
                            continue
                        coerced.append(str(entry))
                    value = coerced
                else:
                    value = []

            config[key] = value

        for key in optional_int_keys:
            raw_value = self.get_setting(f'ssh.{key}', None)
            if raw_value in (None, ''):
                config[key] = None
                continue
            try:
                coerced = int(raw_value)
            except (TypeError, ValueError):
                config[key] = None
                continue
            if coerced <= 0:
                config[key] = None
            else:
                config[key] = coerced

        # verbosity remains treated as integer, defaulting to 0 when unset
        verbosity_value = self.get_setting('ssh.verbosity', defaults['verbosity'])
        try:
            config['verbosity'] = int(verbosity_value)
        except (TypeError, ValueError):
            config['verbosity'] = defaults['verbosity']

        return config

    def get_file_manager_config(self) -> Dict[str, Any]:
        """Return configuration relevant to the built-in SFTP file manager."""

        defaults = self.get_default_config().get('file_manager', {})

        def _get_bool(key: str) -> bool:
            value = self.get_setting(f'file_manager.{key}', defaults.get(key, False))
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {'1', 'true', 'yes', 'on'}:
                    return True
                if lowered in {'0', 'false', 'no', 'off'}:
                    return False
            return bool(value)

        def _get_non_negative_int(key: str) -> int:
            default_value = int(defaults.get(key, 0))
            raw_value = self.get_setting(f'file_manager.{key}', default_value)
            if raw_value in (None, ''):
                return default_value
            try:
                coerced = int(raw_value)
            except (TypeError, ValueError):
                return default_value
            if coerced < 0:
                return default_value
            return coerced

        def _get_icon_size_level() -> int:
            default_value = int(defaults.get('icon_size_level', 1))
            raw_value = self.get_setting('file_manager.icon_size_level', default_value)
            try:
                coerced = int(raw_value)
            except (TypeError, ValueError):
                return default_value
            return max(0, min(4, coerced))

        return {
            'force_internal': _get_bool('force_internal'),
            'open_externally': _get_bool('open_externally'),
            'sftp_keepalive_interval': _get_non_negative_int('sftp_keepalive_interval'),
            'sftp_keepalive_count_max': _get_non_negative_int('sftp_keepalive_count_max'),
            'sftp_connect_timeout': _get_non_negative_int('sftp_connect_timeout'),
            'icon_size_level': _get_icon_size_level(),
            'first_run_prompt_shown': _get_bool('first_run_prompt_shown'),
        }

    def get_security_config(self) -> Dict[str, Any]:
        """Get security configuration"""
        return {
            'store_passwords': self.get_setting('security.store_passwords', True),
            'ssh_agent_forwarding': self.get_setting('security.ssh_agent_forwarding', True),
        }

    # Resource monitoring removed

    def reset_to_defaults(self):
        """Reset all settings to defaults"""
        try:
            if self.use_gsettings:
                # Reset all GSettings keys
                for key in self.settings.list_keys():
                    self.settings.reset(key)
            else:
                # Reset JSON config
                self.config_data = self.get_default_config()
                self.save_json_config()
            
            logger.info("Configuration reset to defaults")
            
        except Exception as e:
            logger.error(f"Failed to reset configuration: {e}")

    def export_config(self, file_path: str) -> bool:
        """Export configuration to file"""
        try:
            config_data = {}
            
            if self.use_gsettings:
                # Export GSettings
                for key in self.settings.list_keys():
                    config_key = key.replace('-', '.')
                    config_data[config_key] = self.settings.get_value(key).unpack()
            else:
                config_data = self.config_data.copy()
            
            # Add custom themes
            builtin = ['default', 'dark', 'light', 'black_on_white', 'solarized_dark', 'solarized_light', 'monokai', 'dracula', 'nord', 'gruvbox_dark', 'one_dark', 'tomorrow_night', 'material_dark', 'rose_pine', 'rose_pine_moon', 'rose_pine_dawn', 'catppuccin_latte', 'catppuccin_frappe', 'catppuccin_macchiato', 'catppuccin_mocha']
            config_data['custom_themes'] = {name: theme for name, theme in self.terminal_themes.items() if name not in builtin}
            
            with open(file_path, 'w') as f:
                json.dump(config_data, f, indent=2)
            
            logger.info(f"Configuration exported to {file_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to export configuration: {e}")
            return False

    def import_config(self, file_path: str) -> bool:
        """Import configuration from file"""
        try:
            with open(file_path) as f:
                imported_config = json.load(f)
            
            # Import custom themes
            if 'custom_themes' in imported_config:
                for name, theme in imported_config['custom_themes'].items():
                    self.add_custom_theme(name, theme)
                del imported_config['custom_themes']
            
            # Import settings
            for key, value in imported_config.items():
                self.set_setting(key, value)
            
            logger.info(f"Configuration imported from {file_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to import configuration: {e}")
            return False

    # --- Shortcut override helpers ---
    def _ensure_config_defaults(self, config: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Ensure newly added keys exist in the provided config dict."""
        updated = False

        shortcuts = config.get('shortcuts')
        if not isinstance(shortcuts, dict):
            config['shortcuts'] = {}
            updated = True

        terminal_cfg = config.get('terminal')
        if not isinstance(terminal_cfg, dict):
            config['terminal'] = self.get_default_config().get('terminal', {}).copy()
            terminal_cfg = config['terminal']
            updated = True
        if 'pass_through_mode' not in terminal_cfg:
            terminal_cfg['pass_through_mode'] = False
            updated = True
        elif not isinstance(terminal_cfg['pass_through_mode'], bool):
            terminal_cfg['pass_through_mode'] = bool(terminal_cfg['pass_through_mode'])
            updated = True
        if 'term' not in terminal_cfg:
            terminal_cfg['term'] = None
            updated = True
        else:
            term_value = terminal_cfg['term']
            normalized_term = None
            if isinstance(term_value, str):
                normalized_term = term_value.strip() or None
            elif term_value is None:
                normalized_term = None
            if normalized_term != term_value:
                terminal_cfg['term'] = normalized_term
                updated = True

        encoding_value = terminal_cfg.get('encoding')
        if isinstance(encoding_value, str):
            normalized_encoding = encoding_value.strip()
            if not normalized_encoding:
                normalized_encoding = 'UTF-8'
            if normalized_encoding != encoding_value:
                terminal_cfg['encoding'] = normalized_encoding
                updated = True
        else:
            terminal_cfg['encoding'] = 'UTF-8'
            updated = True

        file_manager_defaults = self.get_default_config().get('file_manager', {})
        file_manager_cfg = config.get('file_manager')
        if not isinstance(file_manager_cfg, dict):
            config['file_manager'] = file_manager_defaults.copy()
            updated = True
        else:
            if 'force_internal' not in file_manager_cfg:
                file_manager_cfg['force_internal'] = bool(
                    file_manager_defaults.get('force_internal', False)
                )
                updated = True
            elif not isinstance(file_manager_cfg['force_internal'], bool):
                file_manager_cfg['force_internal'] = bool(file_manager_cfg['force_internal'])
                updated = True

            if 'open_externally' not in file_manager_cfg:
                file_manager_cfg['open_externally'] = bool(
                    file_manager_defaults.get('open_externally', False)
                )
                updated = True
            elif not isinstance(file_manager_cfg['open_externally'], bool):
                file_manager_cfg['open_externally'] = bool(file_manager_cfg['open_externally'])
                updated = True

            if 'first_run_prompt_shown' not in file_manager_cfg:
                file_manager_cfg['first_run_prompt_shown'] = bool(
                    file_manager_defaults.get('first_run_prompt_shown', False)
                )
                updated = True
            elif not isinstance(file_manager_cfg['first_run_prompt_shown'], bool):
                file_manager_cfg['first_run_prompt_shown'] = bool(
                    file_manager_cfg['first_run_prompt_shown']
                )
                updated = True

            def _ensure_non_negative_int(key: str) -> None:
                nonlocal updated
                default_value = file_manager_defaults.get(key, 0)
                value = file_manager_cfg.get(key, default_value)
                try:
                    coerced = int(value)
                except (TypeError, ValueError):
                    coerced = default_value
                if coerced < 0:
                    coerced = default_value
                if file_manager_cfg.get(key) != coerced:
                    file_manager_cfg[key] = coerced
                    updated = True

            for int_key in (
                'sftp_keepalive_interval',
                'sftp_keepalive_count_max',
                'sftp_connect_timeout',
            ):
                if int_key not in file_manager_cfg:
                    file_manager_cfg[int_key] = int(file_manager_defaults.get(int_key, 0))
                    updated = True
                else:
                    _ensure_non_negative_int(int_key)

            icon_size_default = int(file_manager_defaults.get('icon_size_level', 1))
            icon_size_value = file_manager_cfg.get('icon_size_level', icon_size_default)
            try:
                coerced_icon_size = int(icon_size_value)
            except (TypeError, ValueError):
                coerced_icon_size = icon_size_default
            clamped_icon_size = max(0, min(4, coerced_icon_size))
            if file_manager_cfg.get('icon_size_level') != clamped_icon_size:
                file_manager_cfg['icon_size_level'] = clamped_icon_size
                updated = True

        # --- Logging level: migrate from legacy ssh.debug_enabled --------
        logging_cfg = config.get('logging')
        if not isinstance(logging_cfg, dict):
            logging_cfg = {}
            config['logging'] = logging_cfg
            updated = True
        if logging_cfg.get('level') not in ('info', 'debug'):
            # One-shot migration: if the old hidden ssh.debug_enabled key was
            # True, preserve that as the new 'debug' level. Otherwise default
            # to 'info'.
            legacy_ssh = config.get('ssh') if isinstance(config.get('ssh'), dict) else {}
            legacy_debug = bool(legacy_ssh.get('debug_enabled', False)) if legacy_ssh else False
            logging_cfg['level'] = 'debug' if legacy_debug else 'info'
            updated = True

        ui_cfg = config.get('ui')
        if not isinstance(ui_cfg, dict):
            default_ui = self.get_default_config().get('ui', {}).copy()
            config['ui'] = default_ui
            ui_cfg = default_ui
            updated = True
        display_value = ui_cfg.get('group_color_display') if isinstance(ui_cfg, dict) else None
        if display_value is None:
            ui_cfg['group_color_display'] = 'fill'
            updated = True
        else:
            if not isinstance(display_value, str):
                display_value = str(display_value)
            normalized = display_value.lower()
            if normalized not in {'fill', 'badge'}:
                normalized = 'fill'
            if ui_cfg.get('group_color_display') != normalized:
                ui_cfg['group_color_display'] = normalized
                updated = True

        if 'use_group_color_in_tab' not in ui_cfg:
            ui_cfg['use_group_color_in_tab'] = False
            updated = True
        elif not isinstance(ui_cfg['use_group_color_in_tab'], bool):
            ui_cfg['use_group_color_in_tab'] = bool(ui_cfg['use_group_color_in_tab'])
            updated = True

        if 'use_group_color_in_terminal' not in ui_cfg:
            ui_cfg['use_group_color_in_terminal'] = False
            updated = True
        elif not isinstance(ui_cfg['use_group_color_in_terminal'], bool):
            ui_cfg['use_group_color_in_terminal'] = bool(ui_cfg['use_group_color_in_terminal'])
            updated = True

        sort_last = ui_cfg.get('connection_sort_last')
        if not isinstance(sort_last, str):
            ui_cfg['connection_sort_last'] = 'name-asc'
            updated = True

        ssh_cfg = config.get('ssh')
        if not isinstance(ssh_cfg, dict):
            default_ssh = self.get_default_config().get('ssh', {}).copy()
            config['ssh'] = default_ssh
            updated = True
            ssh_cfg = config['ssh']
        elif 'apply_advanced' in ssh_cfg:
            del ssh_cfg['apply_advanced']
            updated = True
        if 'use_isolated_config' not in ssh_cfg:
            ssh_cfg['use_isolated_config'] = False
            updated = True
        elif not isinstance(ssh_cfg['use_isolated_config'], bool):
            ssh_cfg['use_isolated_config'] = bool(ssh_cfg['use_isolated_config'])
            updated = True

        if not isinstance(config.get('command_blocks'), dict):
            config['command_blocks'] = self.get_default_config()['command_blocks'].copy()
            updated = True
        else:
            cb = config['command_blocks']
            if not isinstance(cb.get('folders'), list):
                cb['folders'] = []
                updated = True
            if not isinstance(cb.get('commands'), list):
                cb['commands'] = []
                updated = True
            if 'insert_only' not in cb:
                cb['insert_only'] = False
                updated = True
            if 'auto_hide_sidebar' not in cb:
                cb['auto_hide_sidebar'] = False
                updated = True

        return config, updated

    def get_shortcut_overrides(self) -> Dict[str, List[str]]:
        """Return a mapping of action names to user-defined shortcut overrides."""
        overrides = self.config_data.get('shortcuts')
        changed = False
        if not isinstance(overrides, dict):
            overrides = {}
            self.config_data['shortcuts'] = overrides
            changed = True

        cleaned: Dict[str, List[str]] = {}
        for action_name, accels in overrides.items():
            if isinstance(accels, list):
                valid_accels = [str(accel) for accel in accels if isinstance(accel, str)]
                if valid_accels != accels:
                    changed = True
                cleaned[action_name] = valid_accels
            elif accels is None:
                changed = True
            else:
                changed = True

        if changed:
            self.config_data['shortcuts'] = cleaned
            self.save_json_config()
            self.emit('setting-changed', 'shortcuts', self._clone_shortcut_overrides(cleaned))

        return self._clone_shortcut_overrides(cleaned)

    def get_shortcut_override(self, action_name: str) -> Optional[List[str]]:
        """Return the stored accelerators for the given action, if any."""
        overrides = self.get_shortcut_overrides()
        shortcuts = overrides.get(action_name)
        if shortcuts is None:
            return None
        return list(shortcuts)

    def set_shortcut_override(self, action_name: str, shortcuts: Optional[List[str]]):
        """Persist user-defined accelerators for a specific action.

        Passing ``None`` removes the stored override, while an empty list is treated
        as an explicit request to disable shortcuts for the action.
        """
        overrides = self.config_data.get('shortcuts')
        if not isinstance(overrides, dict):
            overrides = {}
            self.config_data['shortcuts'] = overrides

        if shortcuts is None:
            if action_name in overrides:
                del overrides[action_name]
                self.save_json_config()
                self.emit('setting-changed', f'shortcuts.{action_name}', None)
                self.emit('setting-changed', 'shortcuts', self._clone_shortcut_overrides(overrides))
            return

        normalized: List[str] = [str(accel) for accel in shortcuts if isinstance(accel, str)]
        overrides[action_name] = normalized
        self.save_json_config()
        self.emit('setting-changed', f'shortcuts.{action_name}', list(normalized))
        self.emit('setting-changed', 'shortcuts', self._clone_shortcut_overrides(overrides))

    def clear_shortcut_overrides(self):
        """Remove all stored shortcut overrides."""
        self.config_data['shortcuts'] = {}
        self.save_json_config()
        self.emit('setting-changed', 'shortcuts', {})

    def _clone_shortcut_overrides(self, overrides: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """Return a shallow copy of the override mapping with cloned accelerator lists."""
        return {action: list(accels) for action, accels in overrides.items()}
