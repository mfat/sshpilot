"""Guards for the WindowHelpMixin extraction.

The about-dialog, help-URL, and keyboard-shortcuts-window methods were moved
verbatim out of window.py into sshpilot/window_help.py as a mixin. This ensures
every method resolves to the mixin module (so no stray copy in the window.py
body silently shadows it).
"""

import sys
import types


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


_HELP_METHODS = (
    "show_about_dialog",
    "open_help_url",
    "show_shortcuts_window",
    "_build_shortcuts_window",
    "_add_safe_current_shortcuts",
    "_get_safe_current_shortcuts",
)


def test_help_methods_resolve_to_mixin_module():
    wm = _window_module()
    for name in _HELP_METHODS:
        method = getattr(wm.MainWindow, name)
        assert method.__module__ == "sshpilot.window_help", (
            f"{name} resolved to {method.__module__}, expected the mixin — a stray "
            "copy in window.py is shadowing it"
        )


def test_help_mixin_in_mro():
    wm = _window_module()
    mro_names = [c.__name__ for c in wm.MainWindow.__mro__]
    assert "WindowHelpMixin" in mro_names
