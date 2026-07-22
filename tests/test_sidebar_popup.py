"""Unit tests for SearchPopup reparenting logic (search_popup.py).

Exercised via __new__ with mock structural pieces + callbacks — no GTK
realization. These verify the popup's own logic; the owner *contract* (that the
window supplies working callbacks/widgets) is covered by the GUI integration
test in test_sidebar_popup_gui.py, which the mocks here cannot catch.
"""
from unittest.mock import MagicMock

from sshpilot.search_popup import SearchPopup


def _popup():
    p = SearchPopup.__new__(SearchPopup)
    p._visible = False
    p._transparent = False
    p._scrim = MagicMock()
    p._panel = MagicMock()
    p._home = MagicMock()
    p._content = MagicMock()
    p._width_func = MagicMock(return_value=280)
    p._on_shown = MagicMock()
    p._on_hidden = MagicMock()
    p._on_dismiss = None
    return p


def test_show_reparents_into_panel():
    p = _popup()
    p.show()

    assert p.visible is True
    p._home.set_content.assert_called_once_with(None)
    p._panel.append.assert_called_once_with(p._content)
    p._on_shown.assert_called_once()
    p._panel.set_size_request.assert_called_once_with(280, -1)
    p._panel.set_visible.assert_called_with(True)
    p._scrim.set_visible.assert_called_with(True)


def test_hide_reattaches_home():
    p = _popup()
    p._visible = True
    p._content.get_parent.return_value = p._panel

    p.hide()

    assert p.visible is False
    p._panel.remove.assert_called_once_with(p._content)
    p._home.set_content.assert_called_once_with(p._content)
    p._on_hidden.assert_called_once()
    p._panel.set_visible.assert_called_with(False)


def test_show_is_idempotent():
    p = _popup()
    p._visible = True
    p.show()
    p._home.set_content.assert_not_called()


def test_hide_is_noop_when_not_shown():
    p = _popup()
    p.hide()
    p._home.set_content.assert_not_called()


def test_transparency_toggle():
    p = _popup()
    p.set_transparent(True)
    assert p._transparent is True
    p._panel.add_css_class.assert_called_once_with('sidebar-popup-transparent')

    p.set_transparent(False)
    assert p._transparent is False
    p._panel.remove_css_class.assert_called_once_with('sidebar-popup-transparent')


def test_dismiss_uses_on_dismiss_callback():
    p = _popup()
    p._on_dismiss = MagicMock()
    p.dismiss()
    p._on_dismiss.assert_called_once()


def test_dismiss_falls_back_to_hide():
    p = _popup()
    p._on_dismiss = None
    p._visible = True
    p._content.get_parent.return_value = p._panel

    p.dismiss()

    assert p.visible is False  # hide() ran
