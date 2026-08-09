"""GUI regression coverage for the group-form dialogs hiding after a save.

The edit (rename), create, and rename-tag dialogs run their mutation through the
async group mutation controller and keep themselves open until it completes.
Each success callback must dismiss the dialog; otherwise the dialog stays open
forever after a successful save (issue: "Group rename dialog won't hide on save").
"""

import pytest

from gi.repository import Adw, Gtk

from tests._gui_harness import requires_gui

requires_gui()

pytestmark = pytest.mark.gui


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


def _hosted_dialogs():
    """Currently-visible (mapped) ``Adw.Dialog`` instances across all toplevels."""
    tops = Gtk.Window.get_toplevels()
    for i in range(tops.get_n_items()):
        for widget in _walk(tops.get_item(i)):
            if isinstance(widget, Adw.Dialog) and widget.get_mapped():
                yield widget


def _entry(dialog):
    return next(
        (widget for widget in _walk(dialog) if isinstance(widget, Gtk.Entry)),
        None,
    )


def _confirm_button(dialog):
    return next(
        (widget for widget in _walk(dialog)
         if isinstance(widget, Gtk.Button)
         and 'suggested-action' in widget.get_css_classes()),
        None,
    )


def _wait_closed(dialog, gui, timeout_ms=2000):
    """Return once the ``closed`` signal has fired, else fail."""
    closed = []
    dialog.connect('closed', lambda *_: closed.append(True))
    import time
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if closed:
            return True
        gui.pump(10)
    return bool(closed)


def _wait_no_mapped_dialogs(gui, timeout_ms=2000):
    """Wait until no visible dialog remains (close animation finished)."""
    import time
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if list(_hosted_dialogs()) == []:
            return True
        gui.pump(10)
    return False


class _Controller:
    """Minimal controller double: runs steps then completes async on GTK."""

    def __init__(self, gui):
        from unittest.mock import MagicMock

        self.client = MagicMock()
        self._gui = gui

    def run(self, operation, *, on_success, on_error, refresh_after=True):
        self._gui.GLib.idle_add(lambda: on_success(operation()) or False)

    def run_sequence(self, steps, *, on_success, on_error):
        def _run():
            try:
                previous = None
                for step in steps:
                    previous = step(previous)
            except Exception as exc:  # pragma: no cover - failure path
                on_error(exc)
                return False
            on_success(previous)
            return False
        self._gui.GLib.idle_add(_run)


def _install_group(gui, name='Before'):
    window = gui.window
    window.group_manager.groups = {
        'g1': {
            'id': 'g1',
            'name': name,
            'parent_id': None,
            'children': [],
            'connections': [],
            'expanded': True,
            'order': 0,
            'color': None,
        },
    }
    window.rebuild_connection_list()
    gui.pump(100)
    row = next(
        (r for r in window.connection_list if getattr(r, 'group_id', None) == 'g1'),
        None,
    )
    assert row is not None, 'group row not built'
    window.connection_list.select_row(row)
    gui.pump(50)
    return row


def _confirm(dialog, gui, new_name):
    entry = _entry(dialog)
    assert entry is not None, 'group form entry missing'
    entry.set_text(new_name)
    confirm = _confirm_button(dialog)
    assert confirm is not None, 'confirm button missing'
    confirm.emit('clicked')
    assert _wait_closed(dialog, gui), 'dialog must close after a successful save'
    assert _wait_no_mapped_dialogs(gui), 'no dialog may remain after save'


def test_edit_group_dialog_hides_after_save(gui):
    window = gui.window
    _install_group(gui, name='Before')
    window.group_manager.controller = _Controller(gui)

    window.on_rename_group_clicked(None)
    gui.pump(100)

    dialog = next(_hosted_dialogs(), None)
    assert dialog is not None, 'edit group dialog did not appear'
    _confirm(dialog, gui, 'After')


def test_create_group_dialog_hides_after_save(gui):
    window = gui.window
    window.group_manager.controller = _Controller(gui)

    window.on_create_group_action(None, None)
    gui.pump(100)

    dialog = next(_hosted_dialogs(), None)
    assert dialog is not None, 'create group dialog did not appear'
    _confirm(dialog, gui, 'Prod')


def test_rename_tag_dialog_hides_after_save(gui):
    from types import SimpleNamespace

    window = gui.window
    controller = _Controller(gui)
    window.group_manager.controller = controller
    window.group_manager.client = controller.client

    tag_row = SimpleNamespace(
        is_tag_group=True,
        group_info={'name': 'staging', 'tag_key': 'staging'},
    )
    window.on_rename_tag_action(tag_row)
    gui.pump(100)

    dialog = next(_hosted_dialogs(), None)
    assert dialog is not None, 'rename tag dialog did not appear'
    _confirm(dialog, gui, 'prod')
