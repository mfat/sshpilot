"""Unit tests for OLED pure black theme support."""

from sshpilot.oled_theme_css import OLED_THEME_CSS, apply_oled_theme_override
from pathlib import Path


def test_oled_theme_css_defines_pitch_black_tokens():
    assert "@define-color window_bg_color #000000;" in OLED_THEME_CSS
    assert "@define-color view_bg_color #000000;" in OLED_THEME_CSS
    assert "@define-color headerbar_bg_color #000000;" in OLED_THEME_CSS
    assert "@define-color card_bg_color #080808;" in OLED_THEME_CSS
    assert "@define-color sidebar_bg_color #000000;" in OLED_THEME_CSS


def test_oled_theme_css_does_not_override_terminal():
    # Terminal widgets (VteTerminal / PyXterm) have their own color settings;
    # verify that OLED CSS does not touch VTE / terminal classes directly.
    assert "vte" not in OLED_THEME_CSS.lower()
    assert "terminal" not in OLED_THEME_CSS.lower()


def test_apply_oled_theme_override_safe_without_display():
    # Should not raise exception even when headless or display is None
    apply_oled_theme_override(True)
    apply_oled_theme_override(False)


def test_theme_selector_includes_oled_black():
    source = Path("src/sshpilot/theme_selector.py").read_text(encoding="utf-8")
    assert "'black', 'black', _('OLED Black Style')" in source
    assert "checkbutton.black { background: #000000;" in source


def test_actions_allows_black_app_theme():
    source = Path("src/sshpilot/actions.py").read_text(encoding="utf-8")
    assert "'default', 'light', 'dark', 'black'" in source


def test_window_supports_black_app_theme():
    source = Path("src/sshpilot/window.py").read_text(encoding="utf-8")
    assert "'black': _('OLED Black')" in source
    assert "menu.append(_('OLED Black'), 'win.set-app-theme::black')" in source
