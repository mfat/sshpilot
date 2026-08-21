"""Custom content title-bar decoration layout regression tests."""

import sys
import types


def _helper():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot.window import _decoration_layout_without_app_icon
    return _decoration_layout_without_app_icon


def test_removes_start_side_app_icon_without_moving_other_controls():
    assert _helper()('icon:minimize,maximize,close') == ':minimize,maximize,close'


def test_removes_icon_and_fallback_menu_from_either_side():
    assert _helper()('close,menu:maximize,icon,minimize') == 'close:maximize,minimize'


def test_preserves_left_handed_window_button_layout():
    assert _helper()('close,maximize,minimize:icon') == 'close,maximize,minimize:'


def test_missing_layout_uses_gtk_default_without_the_icon():
    assert _helper()(None) == ':minimize,maximize,close'
