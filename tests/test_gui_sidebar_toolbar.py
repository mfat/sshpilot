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


def _menu_actions(model):
    actions = []
    for index in range(model.get_n_items()):
        value = model.get_item_attribute_value(index, 'action', None)
        if value is not None:
            actions.append(value.get_string())
    return actions


def test_sidebar_actions_live_in_the_title_row(gui):
    win = gui.window
    header = win.sidebar_header_bar
    add_button = win._sidebar_add_button
    view_button = win._sidebar_view_button
    # One row: the actions are packed into the sidebar header bar itself.
    for button in (add_button, win.search_button, view_button):
        assert button.is_ancestor(header)
    # The + menu offers both New Connection and New Group.
    assert _menu_actions(add_button.get_menu_model()) == [
        'app.new-connection', 'win.create-group']
    assert add_button.get_icon_name() == 'list-add-symbolic'


def test_view_options_menu_carries_sort_tags_and_hostnames(gui):
    win = gui.window
    view_button = win._sidebar_view_button
    view_button.popup()
    gui.pump(50)
    view_button.popdown()
    menu = view_button.get_menu_model()
    sort_section = menu.get_item_link(0, 'section')
    assert set(_menu_actions(sort_section)) == {'win.sort-connections'}
    options = menu.get_item_link(1, 'section')
    assert options.get_item_link(0, 'submenu') is not None
    assert _menu_actions(options) == ['win.show-hostnames']
    assert win.lookup_action('show-hostnames') is not None


def test_show_hostnames_state_follows_toggle(gui):
    win = gui.window
    action = win.lookup_action('show-hostnames')
    before = action.get_state().get_boolean()
    win.lookup_action('toggle-hide-hosts').activate(None)
    gui.pump(20)
    assert action.get_state().get_boolean() is (not before)
    # Disabled unless rows show hostnames; enabled, it toggles the same state.
    action.set_enabled(True)
    action.activate(None)
    gui.pump(20)
    assert action.get_state().get_boolean() is before


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
