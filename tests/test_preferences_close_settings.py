"""The Closing Tabs and Quitting switches map onto the stored policies."""

import types

from sshpilot.preferences import PreferencesWindow


class FakeConfig:
    def __init__(self):
        self.settings = {}

    def set_setting(self, key, value):
        self.settings[key] = value


def test_ask_before_quitting_switch_stores_the_app_close_policy():
    prefs = types.SimpleNamespace(config=FakeConfig())
    switch = types.SimpleNamespace(get_active=lambda: True)

    PreferencesWindow.on_confirm_quit_changed(prefs, switch, None)
    assert prefs.config.settings["terminal.daemon_app_close_policy"] == "ask"

    switch.get_active = lambda: False
    PreferencesWindow.on_confirm_quit_changed(prefs, switch, None)
    assert prefs.config.settings["terminal.daemon_app_close_policy"] == "terminate"


def test_closing_settings_are_indexed_on_the_general_page():
    from sshpilot.settings_index import PAGE_TITLES, SETTINGS

    assert "general" in PAGE_TITLES and "interface" not in PAGE_TITLES
    closing = [e for e in SETTINGS if e.group == "Closing Tabs and Quitting"]
    assert {e.title for e in closing} == {"Ask before closing a tab", "Ask before quitting"}
    assert {e.page_id for e in closing} == {"general"}
