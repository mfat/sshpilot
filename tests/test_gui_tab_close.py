"""GUI regression tests for bulk tab-close confirmation (issue #1014).

"Close Other Tabs" / "Close Tabs to the Right" must show a SINGLE confirmation
dialog when "Confirm before disconnecting" is on (previously one modal per page
stacked and became undismissable), and must close directly when the preference
is off. Single "Close" keeps its own per-tab confirmation.

These boot the real app and drive the actual win.tabmenu-* actions the context
menu items target. Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _enable_confirm(gui, on=True):
    gui.window.config.set_setting('confirm-disconnect', on)


def _opt_out_checkbox(dialog):
    checkbox = dialog.get_extra_child()
    assert checkbox is not None
    assert checkbox.get_label() == "Don't ask me again"
    return checkbox


def test_close_others_one_dialog_then_confirm(gui):
    gui.open_local_tabs(3)
    _enable_confirm(gui, True)
    target = gui.user_pages()[1]

    gui.activate_action('tabmenu-close-others', target_page=target)

    dlgs = gui.message_dialogs()
    assert len(dlgs) == 1, f'expected exactly one dialog, got {len(dlgs)}'
    assert 'session' in dlgs[0].get_body().lower()

    gui.respond('close')
    remaining = gui.user_pages()
    assert len(remaining) == 1
    assert remaining[0] is target


def test_close_others_cancel_keeps_all(gui):
    gui.open_local_tabs(3)
    _enable_confirm(gui, True)
    target = gui.user_pages()[1]

    gui.activate_action('tabmenu-close-others', target_page=target)
    assert len(gui.message_dialogs()) == 1

    gui.respond('cancel')
    assert len(gui.user_pages()) == 3  # nothing closed


def test_close_others_no_dialog_when_pref_off(gui):
    gui.open_local_tabs(3)
    _enable_confirm(gui, False)
    target = gui.user_pages()[1]

    gui.activate_action('tabmenu-close-others', target_page=target)

    assert gui.message_dialogs() == []  # closed directly, no confirmation
    remaining = gui.user_pages()
    assert len(remaining) == 1
    assert remaining[0] is target


def test_close_right_counts_only_right(gui):
    gui.open_local_tabs(4)
    _enable_confirm(gui, True)
    pages = gui.user_pages()
    target = pages[1]  # two tabs sit to its right

    gui.activate_action('tabmenu-close-right', target_page=target)

    dlgs = gui.message_dialogs()
    assert len(dlgs) == 1
    assert '2 tab' in dlgs[0].get_body()  # only the two right-side tabs

    gui.respond('close')
    remaining = gui.user_pages()
    assert len(remaining) == 2  # target + the one to its left
    assert target in remaining


def test_single_close_still_confirms(gui):
    gui.open_local_tabs(2)
    _enable_confirm(gui, True)
    target = gui.user_pages()[0]

    gui.activate_action('tabmenu-close', target_page=target)

    dlgs = gui.message_dialogs()
    assert len(dlgs) == 1
    assert 'Close connection' in dlgs[0].get_heading()

    gui.respond('cancel')
    assert len(gui.user_pages()) == 2  # cancelled, still open


def test_single_close_opt_out_disables_future_confirmations(gui):
    gui.open_local_tabs(3)
    _enable_confirm(gui, True)

    gui.activate_action('tabmenu-close', target_page=gui.user_pages()[0])
    dialog = gui.message_dialogs()[0]
    _opt_out_checkbox(dialog).set_active(True)
    gui.respond('close')

    assert gui.window.config.get_setting('confirm-disconnect', True) is False
    assert len(gui.user_pages()) == 2

    gui.activate_action('tabmenu-close', target_page=gui.user_pages()[0])
    assert gui.message_dialogs() == []
    assert len(gui.user_pages()) == 1


def test_close_others_opt_out_disables_future_confirmations(gui):
    gui.open_local_tabs(3)
    _enable_confirm(gui, True)
    target = gui.user_pages()[1]

    gui.activate_action('tabmenu-close-others', target_page=target)
    dialog = gui.message_dialogs()[0]
    _opt_out_checkbox(dialog).set_active(True)
    gui.respond('close')

    assert gui.window.config.get_setting('confirm-disconnect', True) is False
    assert gui.user_pages() == [target]


def test_cancel_with_opt_out_checked_keeps_confirmation_enabled(gui):
    gui.open_local_tabs(2)
    _enable_confirm(gui, True)

    gui.activate_action('tabmenu-close', target_page=gui.user_pages()[0])
    dialog = gui.message_dialogs()[0]
    _opt_out_checkbox(dialog).set_active(True)
    gui.respond('cancel')

    assert gui.window.config.get_setting('confirm-disconnect', True) is True
    assert len(gui.user_pages()) == 2
