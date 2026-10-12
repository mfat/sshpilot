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
    # MainWindow.__mro__ (making `mro.index(...)` raise). Names are stable.
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


def test_send_ignores_overlapping_active_broadcast():
    from unittest.mock import Mock
    from sshpilot.window_broadcast import WindowBroadcastMixin

    window = Mock()
    window._broadcast_submission_pending = True
    WindowBroadcastMixin.on_broadcast_send_clicked(window, Mock())
    window.terminal_manager.broadcast_command.assert_not_called()


def test_successful_session_broadcast_reenables_send_action():
    from unittest.mock import Mock
    from sshpilot.window_broadcast import WindowBroadcastMixin

    window = Mock()
    window.broadcast_send_button = Mock()
    window._broadcast_submission_pending = True
    WindowBroadcastMixin._on_broadcast_sent(window, None)
    assert window._broadcast_submission_pending is False
    window.broadcast_send_button.set_sensitive.assert_called_once_with(True)


def test_second_send_is_ignored_while_first_submission_callback_is_pending():
    from unittest.mock import Mock
    from sshpilot.window_broadcast import WindowBroadcastMixin

    window = Mock()
    window._broadcast_submission_pending = False
    window.broadcast_entry.get_text.return_value = "uptime"
    window.terminal_manager.broadcast_command.return_value = Mock()
    WindowBroadcastMixin.on_broadcast_send_clicked(window, Mock())
    WindowBroadcastMixin.on_broadcast_send_clicked(window, Mock())
    window.terminal_manager.broadcast_command.assert_called_once()
    assert window._broadcast_submission_pending is True
    window.broadcast_send_button.set_sensitive.assert_called_once_with(False)


def test_banner_never_hides_itself():
    # The banner stays until the user dismisses it (Cancel/Escape): sending
    # must not hide it, and no auto-hide timer machinery may come back.
    from unittest.mock import Mock
    from sshpilot.window_broadcast import WindowBroadcastMixin

    for name in ("_schedule_broadcast_hide_timeout", "on_broadcast_entry_focus_leave"):
        assert not hasattr(WindowBroadcastMixin, name)

    window = Mock()
    window._broadcast_submission_pending = False
    window.broadcast_entry.get_text.return_value = "uptime"
    WindowBroadcastMixin.on_broadcast_send_clicked(window, Mock())
    window.hide_broadcast_banner.assert_not_called()
    window.broadcast_banner.set_reveal_child.assert_not_called()
    window.broadcast_banner.set_visible.assert_not_called()


def test_clear_icon_empties_entry_and_keeps_focus():
    from unittest.mock import Mock
    from gi.repository import Gtk
    from sshpilot.window_broadcast import WindowBroadcastMixin

    window = Mock()
    WindowBroadcastMixin.on_broadcast_entry_icon_release(
        window, window.broadcast_entry, Gtk.EntryIconPosition.SECONDARY
    )
    window.broadcast_entry.set_text.assert_called_once_with("")
    window.broadcast_entry.grab_focus.assert_called_once()
    window.hide_broadcast_banner.assert_not_called()
