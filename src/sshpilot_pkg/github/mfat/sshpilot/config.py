"""
Configuration Manager for sshPilot
Handles application settings, themes, and preferences
"""

import json
import logging
import os
from typing import Dict, Any, Optional

from gi.repository import Gio, GLib, GObject

logger = logging.getLogger(__name__)

class Config(GObject.Object):
    """Configuration manager for sshPilot"""
    
    __gsignals__ = {
        'setting-changed': (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }
    
    def __init__(self):
        super().__init__()
        
        # Try to use GSettings if schema is available
        try:
            self.settings = Gio.Settings.new('io.github.mfat.sshpilot')
            self.use_gsettings = True
            logger.info("Using GSettings for configuration")
        except Exception as e:
            logger.warning(f"GSettings not available, using JSON config: {e}")
            self.settings = None
            self.use_gsettings = False
            self.config_file = os.path.expanduser('~/.config/sshpilot/config.json')
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
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            else:
                # Create default config
                default_config = self.get_default_config()
                self.save_json_config(default_config)
                return default_config
        except Exception as e:
            logger.error(f"Failed to load JSON config: {e}")
            return self.get_default_config()

    def save_json_config(self, config_data: Dict[str, Any] = None):
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

    def get_default_config(self) -> Dict[str, Any]:
        """Get default configuration values"""
        return {
            'terminal': {
                'theme': 'default',
                'font': 'Monospace 12',
                'scrollback_lines': 10000,
                'cursor_blink': True,
                'audible_bell': False,
            },
            'ui': {
                'show_hostname': True,
                'auto_focus_terminal': True,
                'confirm_close_tabs': True,
                'remember_window_size': True,
                'window_width': 1200,
                'window_height': 800,
                'sidebar_width': 250,
            },
            'ssh': {
                'connection_timeout': 30,
                'keepalive_interval': 60,
                'compression': True,
                'auto_add_host_keys': True,
            },
            'security': {
                'store_passwords': True,
                'ssh_agent_forwarding': True,
            }
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
            }
        }

    def get_setting(self, key: str, default=None):
        """Get a setting value"""
        try:
            if self.use_gsettings:
                # Convert key format for GSettings
                gsettings_key = key.replace('.', '-')
                if self.settings.list_keys().__contains__(gsettings_key):
                    return self.settings.get_value(gsettings_key).unpack()
                else:
                    return default
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
                    self.settings.set_value(gsettings_key, GLib.Variant.new_string(str(value)))
            else:
                # Navigate nested dictionary and set value
                keys = key.split('.')
                current = self.config_data
                for k in keys[:-1]:
                    if k not in current:
                        current[k] = {}
                    current = current[k]
                current[keys[-1]] = value
                
                # Save to file
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
        if name in self.terminal_themes and name not in ['default', 'dark', 'light', 'solarized_dark', 'solarized_light']:
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

    def save_window_geometry(self, width: int, height: int, sidebar_width: int = None):
        """Save window geometry"""
        if self.get_setting('ui.remember_window_size', True):
            self.set_setting('ui.window_width', width)
            self.set_setting('ui.window_height', height)
            if sidebar_width is not None:
                self.set_setting('ui.sidebar_width', sidebar_width)

    def get_ssh_config(self) -> Dict[str, Any]:
        """Get SSH configuration"""
        return {
            'connection_timeout': self.get_setting('ssh.connection_timeout', 30),
            'keepalive_interval': self.get_setting('ssh.keepalive_interval', 60),
            'compression': self.get_setting('ssh.compression', True),
            'auto_add_host_keys': self.get_setting('ssh.auto_add_host_keys', True),
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
            config_data['custom_themes'] = {
                name: theme for name, theme in self.terminal_themes.items()
                if name not in ['default', 'dark', 'light', 'solarized_dark', 'solarized_light']
            }
            
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
            with open(file_path, 'r') as f:
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