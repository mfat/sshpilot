"""Regression tests for the startup max-sidebar-width resolution.

Previously the saved ``ui.max-sidebar-width`` setting was read while building
the split view but then ignored — the code hard-coded ``set_max_sidebar_width(400)``
and the saved value only took effect after the user changed it mid-session
(via the settings-changed handler). The build path now routes the saved value
through ``_effective_max_sidebar_width`` and applies it on startup.

These tests cover the pure parsing/fallback logic. They do NOT prove the GTK
widget is actually told to use the width (that requires a real GTK display and
is covered by manual QA) — only that the value handed to the widget is correct.
"""

import sys
import types


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


def test_saved_int_is_used():
    fn = _window_module()._effective_max_sidebar_width
    assert fn(320) == 320


def test_saved_numeric_string_is_coerced():
    fn = _window_module()._effective_max_sidebar_width
    assert fn("350") == 350


def test_none_falls_back_to_default():
    fn = _window_module()._effective_max_sidebar_width
    assert fn(None) == 400


def test_invalid_value_falls_back_to_default():
    fn = _window_module()._effective_max_sidebar_width
    assert fn("not-a-number") == 400
    assert fn(object()) == 400


def test_custom_default_is_honored_when_unset():
    fn = _window_module()._effective_max_sidebar_width
    assert fn(None, default=280) == 280
