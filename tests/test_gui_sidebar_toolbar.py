import pytest

from gi.repository import Gtk
import sshpilot.window as window_module

from tests._gui_harness import requires_gui

requires_gui()

pytestmark = pytest.mark.gui


def _horizontal_measure(widget):
    minimum, natural, _minimum_baseline, _natural_baseline = widget.measure(
        Gtk.Orientation.HORIZONTAL, -1
    )
    return minimum, natural


def test_selection_toolbar_keeps_same_width_for_connections_and_groups(gui):
    win = gui.window
    stack = win._sidebar_selection_toolbar

    assert stack.get_hhomogeneous()
    assert stack.get_vhomogeneous()

    stack.set_visible_child_name('connection')
    gui.pump(50)
    connection_size = _horizontal_measure(stack)

    stack.set_visible_child_name('group')
    gui.pump(50)
    group_size = _horizontal_measure(stack)

    stack.set_visible_child_name('empty')
    gui.pump(50)
    empty_size = _horizontal_measure(stack)

    assert connection_size == group_size == empty_size


def test_new_group_button_follows_new_connection_in_sidebar_header(gui):
    header = gui.window._sidebar_header_handle.get_child()
    new_connection = header.get_first_child()
    new_group = new_connection.get_next_sibling()

    assert new_group.get_action_name() == 'win.create-group'
    assert new_group.get_child().get_icon_name() == 'folder-new-symbolic'


def test_explicit_sort_is_not_reapplied_during_sidebar_rebuild(gui, monkeypatch):
    """A rebuild must preserve daemon/DnD ordering after an explicit sort."""
    window = gui.window
    calls = []

    def fake_apply_sort(group_manager, connections, preset_id):
        calls.append((group_manager, tuple(connections), preset_id))
        return True

    monkeypatch.setattr(window_module, "apply_sort_to_manager", fake_apply_sort)

    window.apply_connection_sort_preset("name-desc")

    assert len(calls) == 1
    assert calls[0][0] is window.group_manager
    assert calls[0][2] == "name-desc"
