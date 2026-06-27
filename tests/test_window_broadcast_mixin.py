"""Guards for the WindowBroadcastMixin extraction.

The broadcast banner methods were moved verbatim out of window.py into
sshpilot/window_broadcast.py as a mixin. These checks ensure the move stays
behavior-preserving: every method resolves to the mixin module (so no stray
copy in the window.py body silently shadows it), and the mixin sits ahead of
WindowActions in the MRO so its on_broadcast_command_action wins over the
(now-removed) dead duplicate that used to live in WindowActions.
"""

import sys
import types


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


_BROADCAST_METHODS = (
    "on_broadcast_send_clicked",
    "on_broadcast_cancel_clicked",
    "on_broadcast_entry_activate",
    "on_broadcast_entry_key_pressed",
    "on_broadcast_banner_key_pressed",
    "hide_broadcast_banner",
    "_focus_active_terminal_tab",
    "show_broadcast_banner",
    "on_broadcast_entry_changed",
    "on_broadcast_entry_focus_enter",
    "on_broadcast_entry_focus_leave",
    "_cancel_broadcast_hide_timeout",
    "_schedule_broadcast_hide_timeout",
    "on_broadcast_command_action",
)


def test_broadcast_methods_resolve_to_mixin_module():
    wm = _window_module()
    for name in _BROADCAST_METHODS:
        method = getattr(wm.MainWindow, name)
        assert method.__module__ == "sshpilot.window_broadcast", (
            f"{name} resolved to {method.__module__}, expected the mixin — a stray "
            "copy in window.py is shadowing it"
        )


def test_mixin_precedes_window_actions_in_mro():
    wm = _window_module()
    # Compare by class name, not imported identity: other tests reimport
    # sshpilot.actions / sshpilot.window as fresh modules, so a `from ... import`
    # here can yield a different class object than the one baked into
    # MainWindow.__mro__. Names are stable across reimports.
    mro_names = [c.__name__ for c in wm.MainWindow.__mro__]
    assert "WindowBroadcastMixin" in mro_names
    assert mro_names.index("WindowBroadcastMixin") < mro_names.index("WindowActions")


def test_dead_broadcast_copy_removed_from_window_actions():
    # The old dialog-based duplicate in WindowActions was dead (shadowed) and
    # has been deleted; it must not creep back and shadow the mixin. Locate the
    # actual base in MainWindow's MRO (by name) so this is robust to reimports.
    wm = _window_module()
    actions_cls = next(c for c in wm.MainWindow.__mro__ if c.__name__ == "WindowActions")
    assert "on_broadcast_command_action" not in vars(actions_cls)
