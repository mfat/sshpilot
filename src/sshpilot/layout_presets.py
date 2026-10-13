"""Main-window layout presets offered on Settings › Interface.

A preset is only a shortcut: it writes the existing startup settings
(Interface › App Startup and Sidebar › Hide Sidebar on Startup), so every
layout it offers is also reachable through those toggles. When the settings
open a restored session, no view card is selected.
"""

from typing import Optional

STARTUP_BEHAVIOR_KEY = 'app-startup-behavior'
HIDE_SIDEBAR_KEY = 'ui.sidebar_hide_on_startup'

# View id -> the App Startup value it writes. The sidebar is independent of
# the view: either view opens with or without it.
VIEWS = {
    'startpage': 'welcome',
    'terminal': 'terminal',
}


def current_view(config) -> Optional[str]:
    """The view App Startup opens, or None (a restored session)."""
    behavior = config.get_setting(STARTUP_BEHAVIOR_KEY, 'welcome')
    for view_id, value in VIEWS.items():
        if value == behavior:
            return view_id
    return None


def apply_view(config, view_id: str) -> None:
    if config.get_setting(STARTUP_BEHAVIOR_KEY, None) != VIEWS[view_id]:
        config.set_setting(STARTUP_BEHAVIOR_KEY, VIEWS[view_id])


def sidebar_shown(config) -> bool:
    return not bool(config.get_setting(HIDE_SIDEBAR_KEY, False))


def set_sidebar_shown(config, shown: bool) -> None:
    if sidebar_shown(config) != shown:
        config.set_setting(HIDE_SIDEBAR_KEY, not shown)
