"""Accent-override CSS keeps the full navigation-sidebar selection ladder."""

from sshpilot.accent_override_css import (
    NAVIGATION_SIDEBAR_SELECTION_CSS,
    build_accent_override_css,
)


def test_navigation_sidebar_selection_includes_hover_and_active():
    css = NAVIGATION_SIDEBAR_SELECTION_CSS
    assert 'alpha(@window_fg_color, 0.18)' in css
    assert 'alpha(@window_fg_color, 0.22)' in css
    assert 'alpha(@window_fg_color, 0.26)' in css
    assert 'row:selected:hover' in css
    assert 'row:selected:active' in css
    assert 'sidebar-section-header:selected' in css


def test_build_accent_override_css_embeds_shared_selection_ladder():
    css = build_accent_override_css('#ff00aa')
    assert '@define-color accent_bg_color #ff00aa;' in css
    assert 'theme_selected_bg_color #ff00aa' in css
    assert 'alpha(@window_fg_color, 0.18)' in css
    assert 'alpha(@window_fg_color, 0.22)' in css
    assert 'alpha(@window_fg_color, 0.26)' in css
    assert 'row:selected:hover' in css
