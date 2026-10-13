"""Main-window layout presets offered on Settings › Appearance.

A preset is only a shortcut: it writes the existing startup settings
(Interface › App Startup and Sidebar › Hide Sidebar on Startup), so every
layout it offers is also reachable through those toggles. When the settings
match no preset (say, a restored session) no preset is current.
"""

from typing import Optional

STARTUP_BEHAVIOR_KEY = 'app-startup-behavior'
HIDE_SIDEBAR_KEY = 'ui.sidebar_hide_on_startup'

# Preset id -> the settings it writes.
LAYOUT_PRESETS = {
    'sidebar': {STARTUP_BEHAVIOR_KEY: 'welcome', HIDE_SIDEBAR_KEY: False},
    'startpage': {STARTUP_BEHAVIOR_KEY: 'welcome', HIDE_SIDEBAR_KEY: True},
    'terminal': {STARTUP_BEHAVIOR_KEY: 'terminal', HIDE_SIDEBAR_KEY: True},
}

PRESET_KEYS = frozenset({STARTUP_BEHAVIOR_KEY, HIDE_SIDEBAR_KEY})


def current_preset(config) -> Optional[str]:
    """The preset the current settings match, or None."""
    current = {
        STARTUP_BEHAVIOR_KEY: config.get_setting(STARTUP_BEHAVIOR_KEY, 'welcome'),
        HIDE_SIDEBAR_KEY: bool(config.get_setting(HIDE_SIDEBAR_KEY, False)),
    }
    for preset_id, values in LAYOUT_PRESETS.items():
        if values == current:
            return preset_id
    return None


def apply_preset(config, preset_id: str) -> None:
    """Write *preset_id*'s settings, skipping the ones already set."""
    for key, value in LAYOUT_PRESETS[preset_id].items():
        if config.get_setting(key, None) != value:
            config.set_setting(key, value)
