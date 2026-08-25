"""Terminal theme selector catalog and window wiring guards."""

from pathlib import Path

from sshpilot.config import Config
from sshpilot.terminal_theme_selector import (
    TERMINAL_SCHEME_KEYS,
    selectable_terminal_theme_keys,
)


def test_selector_keys_exist_in_authoritative_terminal_themes():
    themes = Config.load_builtin_themes(None)
    assert selectable_terminal_theme_keys(themes) == TERMINAL_SCHEME_KEYS


def test_selector_omits_catalog_entries_that_are_not_user_selectable():
    themes = Config.load_builtin_themes(None)
    keys = selectable_terminal_theme_keys(themes)
    assert "dark" not in keys
    assert "light" not in keys


def test_selector_uses_ptyxis_style_palette_cards():
    source = Path("src/sshpilot/terminal_theme_selector.py").read_text(
        encoding="utf-8"
    )
    assert "self.flow_box = Gtk.FlowBox()" in source
    assert "self.flow_box.set_max_children_per_line(3)" in source
    assert '_("The quick brown fox jumps over the lazy dog")' in source
    assert 'tuple(palette[1:7])' in source
    assert 'button.add_css_class("terminal-palette-selected")' in source


def test_tab_bar_terminal_theme_picker_uses_global_terminal_setting():
    source = Path("src/sshpilot/window.py").read_text(encoding="utf-8")
    assert "self._terminal_theme_menu_button = Gtk.MenuButton()" in source
    assert "self.config.set_setting('terminal.theme', theme_key)" in source
    assert "self._headerbar_end_box.append(self._terminal_theme_menu_button)" in source


def test_terminal_theme_picker_has_headerbar_visibility_preference():
    actions_source = Path("src/sshpilot/actions.py").read_text(encoding="utf-8")
    preferences_source = Path("src/sshpilot/preferences.py").read_text(encoding="utf-8")
    window_source = Path("src/sshpilot/window.py").read_text(encoding="utf-8")

    assert "'ui.headerbar_show_terminal_theme', True" in actions_source
    assert "'ui.headerbar_show_terminal_theme'" in preferences_source
    assert "'_terminal_theme_menu_button', 'ui.headerbar_show_terminal_theme', True" in window_source
