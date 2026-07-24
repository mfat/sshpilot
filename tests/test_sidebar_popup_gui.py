"""GUI integration test for the detachable search popup.

Drift guard: this drives the *real* window, so `popup.show()`/`hide()` run the
real owner callbacks (`_on_search_popup_shown/hidden`, which call the live
minimal-mode helpers). If any of that contract is renamed or changed, this
fails — which the mocked unit tests in test_sidebar_popup.py cannot catch.

Runs only under the on-demand GUI harness (skipped headless / in CI).
"""
import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def test_popup_reparents_sidebar_on_real_window(gui):
    win = gui.window
    popup = win._search_popup
    box = win._sidebar_box
    tv = win._sidebar_toolbar_view

    # Docked: the sidebar box is the ToolbarView's content.
    assert tv.get_content() is box
    assert popup.visible is False

    popup.show()  # runs _on_search_popup_shown -> the real minimal-mode helpers
    assert popup.visible is True
    assert tv.get_content() is None
    assert box.get_parent() is popup._panel

    popup.hide()  # runs _on_search_popup_hidden
    assert popup.visible is False
    assert tv.get_content() is box


def test_popup_transparency_toggle_on_real_window(gui):
    popup = gui.window._search_popup
    popup.set_transparent(True)
    assert popup._panel.has_css_class('sidebar-popup-transparent')
    popup.set_transparent(False)
    assert not popup._panel.has_css_class('sidebar-popup-transparent')
