"""Middle-click behaviour in the sidebar.

Middle-clicking a connection row opens it in a new tab; middle-clicking a
group row opens every connection in that group in its own tab (which confirms
itself when the group is large — see ``test_split_view_open_confirmation``).

Pure unit test: ``Gtk.GestureClick`` is replaced with a fake that hands us the
handler, so no display is needed.
"""

import types

import pytest

from sshpilot import sidebar
from sshpilot.sidebar import Gdk


class FakeGesture:
    """Records the button it filters on and the handlers connected to it."""

    instances = []

    def __init__(self):
        self.button = None
        self.state = None
        self.current_button = None
        self.handlers = {}
        FakeGesture.instances.append(self)

    def set_button(self, button):
        self.button = button

    def set_propagation_phase(self, phase):
        pass

    def set_state(self, state):
        self.state = state

    def get_current_button(self):
        return self.current_button

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def press(self, x=0.0, y=0.0, n_press=1):
        self.handlers['pressed'](self, n_press, x, y)


class FakeConnectionList:
    def __init__(self):
        self.controllers = []
        self.selected_row = None

    def add_controller(self, controller):
        self.controllers.append(controller)

    def get_selected_row(self):
        return self.selected_row


def make_window(monkeypatch, row):
    """Attach the sidebar gestures to a fake window and return it."""
    FakeGesture.instances = []
    monkeypatch.setattr(sidebar.Gtk, 'GestureClick', FakeGesture)

    window = types.SimpleNamespace(
        connection_list=FakeConnectionList(),
        add_controller=lambda controller: None,
        _pick_connection_list_row=lambda x, y: row,
        opened_tabs=[],
        opened_group_tabs=[],
    )
    window.on_open_new_connection_action = lambda *_a: window.opened_tabs.append(
        window._context_menu_connection
    )
    window.on_open_group_in_tabs_action = lambda *_a: window.opened_group_tabs.append(
        window._context_menu_group_row
    )
    sidebar._attach_connection_list_context_menu(window)
    return window


def middle_gesture():
    for gesture in FakeGesture.instances:
        if gesture.button == Gdk.BUTTON_MIDDLE:
            return gesture
    raise AssertionError('no middle-click gesture was attached')


def test_middle_click_on_a_connection_row_opens_a_tab(monkeypatch):
    connection = object()
    row = types.SimpleNamespace(connection=connection)
    window = make_window(monkeypatch, row)

    middle_gesture().press()

    assert window.opened_tabs == [connection]
    assert window.opened_group_tabs == []


def test_middle_click_on_a_group_row_opens_the_group_in_tabs(monkeypatch):
    row = types.SimpleNamespace(group_id='g1', group_info={'name': 'Prod'})
    window = make_window(monkeypatch, row)

    gesture = middle_gesture()
    gesture.press()

    assert window.opened_group_tabs == [row]
    assert window.opened_tabs == []
    # The click is consumed so the ListBox does not also act on it.
    assert gesture.state is sidebar.Gtk.EventSequenceState.CLAIMED


def test_middle_click_restores_the_previous_context_targets(monkeypatch):
    row = types.SimpleNamespace(group_id='g1', group_info={'name': 'Prod'})
    window = make_window(monkeypatch, row)
    sentinel = object()
    window._context_menu_group_row = sentinel
    window._context_menu_connection = sentinel
    window._context_menu_connections = [sentinel]

    middle_gesture().press()

    assert window.opened_group_tabs == [row]
    assert window._context_menu_group_row is sentinel
    assert window._context_menu_connection is sentinel
    assert window._context_menu_connections == [sentinel]


def test_middle_click_on_an_unknown_row_does_nothing(monkeypatch):
    window = make_window(monkeypatch, types.SimpleNamespace())

    middle_gesture().press()

    assert window.opened_tabs == []
    assert window.opened_group_tabs == []


def test_double_middle_click_is_ignored(monkeypatch):
    connection = object()
    window = make_window(monkeypatch, types.SimpleNamespace(connection=connection))

    middle_gesture().press(n_press=2)

    assert window.opened_tabs == []
