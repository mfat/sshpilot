"""Guards for the WindowSessionMixin extraction.

The session capture/restore methods were moved verbatim out of window.py into
sshpilot/window_session.py as a mixin. These checks ensure the move stays
behavior-preserving: every method resolves to the mixin module (so no stray
copy in the window.py body silently shadows it). The behavioral round-trip is
covered separately by tests/test_sessions.py.
"""

import sys
import types


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


_SESSION_METHODS = (
    "capture_session",
    "restore_session",
    "_restore_split_tab",
    "_close_all_tabs",
)


def test_session_methods_resolve_to_mixin_module():
    wm = _window_module()
    for name in _SESSION_METHODS:
        method = getattr(wm.MainWindow, name)
        assert method.__module__ == "sshpilot.window_session", (
            f"{name} resolved to {method.__module__}, expected the mixin — a stray "
            "copy in window.py is shadowing it"
        )


def test_session_mixin_in_mro():
    wm = _window_module()
    mro_names = [c.__name__ for c in wm.MainWindow.__mro__]
    assert "WindowSessionMixin" in mro_names
