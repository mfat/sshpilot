"""Regression tests for the large split-view confirmation (GH #1232).

Opening a group in split view is one click, so a group with dozens of hosts
used to spawn that many sessions with no warning. Batches up to
``SPLIT_VIEW_CONFIRM_THRESHOLD`` still open straight away; bigger ones must ask
first, and must open only when the user confirms.

Pure unit tests: ``Adw.AlertDialog`` is replaced with a recording fake, so no
desktop is required.
"""

import types

import pytest

from sshpilot import actions
from sshpilot.actions import SPLIT_VIEW_CONFIRM_THRESHOLD, WindowActions


class FakeAlertDialog:
    instances = []

    def __init__(self, **kwargs):
        self.heading = kwargs.get("heading")
        self.body = kwargs.get("body")
        self.responses = []
        self.default_response = None
        self.close_response = None
        self.presented_parent = None
        self.close_calls = 0
        self._handlers = []
        FakeAlertDialog.instances.append(self)

    def add_response(self, response_id, label):
        self.responses.append(response_id)

    def set_response_appearance(self, response_id, appearance):
        pass

    def set_default_response(self, response_id):
        self.default_response = response_id

    def set_close_response(self, response_id):
        self.close_response = response_id

    def present(self, parent=None):
        self.presented_parent = parent

    def close(self):
        self.close_calls += 1

    def connect(self, signal, handler, *args):
        self._handlers.append((signal, handler, args))

    def emit_response(self, response_id):
        for signal, handler, args in self._handlers:
            if signal == "response":
                handler(self, response_id, *args)


class FakeConnection:
    def __init__(self, nickname):
        self.nickname = nickname


class FakeGroupRow:
    def __init__(self, name, nicknames):
        self.group_info = {"name": name, "connections": list(nicknames)}


class FakeConnectionManager:
    def __init__(self, connections):
        self._by_nickname = {c.nickname: c for c in connections}

    def find_connection_by_nickname(self, nickname):
        return self._by_nickname.get(nickname)


@pytest.fixture(autouse=True)
def fake_adw(monkeypatch):
    FakeAlertDialog.instances = []
    monkeypatch.setattr(actions.Adw, "AlertDialog", FakeAlertDialog)
    monkeypatch.setattr(
        actions.Adw,
        "ResponseAppearance",
        types.SimpleNamespace(SUGGESTED=object(), DESTRUCTIVE=object()),
    )
    monkeypatch.setattr(actions, "mark_default_response_visible", lambda widget: True)
    return FakeAlertDialog


def make_window():
    """A bare WindowActions with the split-tab creation stubbed out."""
    window = WindowActions()
    window.created = []
    window.tabbed = []
    window._create_split_view_tab = lambda connections=None, title=None: (
        window.created.append((list(connections or []), title))
    )
    window._open_new_connection_tabs = lambda connections: (
        window.tabbed.extend(connections)
    )
    return window


def connections(n):
    return [FakeConnection(f"host{i}") for i in range(n)]


def test_batch_at_threshold_opens_without_asking():
    window = make_window()
    conns = connections(SPLIT_VIEW_CONFIRM_THRESHOLD)

    window._open_connections_in_split_view(conns)

    assert FakeAlertDialog.instances == []
    assert window.created == [(conns, None)]


def test_empty_batch_opens_nothing():
    window = make_window()

    window._open_connections_in_split_view([])

    assert FakeAlertDialog.instances == []
    assert window.created == []


def test_large_batch_asks_before_opening():
    window = make_window()
    conns = connections(SPLIT_VIEW_CONFIRM_THRESHOLD + 1)

    window._open_connections_in_split_view(conns, "Split View — Prod")

    assert window.created == [], "nothing may open before the user confirms"
    assert len(FakeAlertDialog.instances) == 1
    dialog = FakeAlertDialog.instances[0]
    assert str(len(conns)) in dialog.body
    assert dialog.responses == ["cancel", "tabs", "open"]
    assert dialog.close_response == "cancel"
    assert dialog.presented_parent is window

    dialog.emit_response("open")

    assert window.created == [(conns, "Split View — Prod")]
    assert dialog.close_calls == 1


def test_separate_tabs_response_opens_one_tab_per_connection():
    window = make_window()
    conns = connections(SPLIT_VIEW_CONFIRM_THRESHOLD + 1)

    window._open_connections_in_split_view(conns)
    FakeAlertDialog.instances[0].emit_response("tabs")

    assert window.created == [], "the split tab must not be built as well"
    assert window.tabbed == conns
    assert FakeAlertDialog.instances[0].close_calls == 1


def test_cancelling_the_confirmation_opens_nothing():
    window = make_window()

    window._open_connections_in_split_view(connections(SPLIT_VIEW_CONFIRM_THRESHOLD + 1))
    FakeAlertDialog.instances[0].emit_response("cancel")

    assert window.created == []
    assert window.tabbed == []
    assert FakeAlertDialog.instances[0].close_calls == 1


def test_group_action_confirms_large_groups_and_keeps_the_title():
    window = make_window()
    conns = connections(SPLIT_VIEW_CONFIRM_THRESHOLD + 3)
    window.connection_manager = FakeConnectionManager(conns)
    window._context_menu_group_row = FakeGroupRow(
        "Prod", [c.nickname for c in conns]
    )

    window.on_open_group_in_split_view_action(None)

    assert window.created == []
    assert len(FakeAlertDialog.instances) == 1
    FakeAlertDialog.instances[0].emit_response("open")

    opened, title = window.created[0]
    assert opened == conns
    assert "Prod" in title


def test_small_group_opens_directly():
    window = make_window()
    conns = connections(2)
    window.connection_manager = FakeConnectionManager(conns)
    window._context_menu_group_row = FakeGroupRow("Lab", [c.nickname for c in conns])

    window.on_open_group_in_split_view_action(None)

    assert FakeAlertDialog.instances == []
    assert window.created and window.created[0][0] == conns


def test_selection_action_confirms_large_selections():
    window = make_window()
    conns = connections(SPLIT_VIEW_CONFIRM_THRESHOLD + 1)
    rows = [types.SimpleNamespace(connection=c) for c in conns]
    window.connection_list = types.SimpleNamespace(get_selected_rows=lambda: rows)

    window.on_open_in_split_view_action(None)

    assert window.created == []
    assert len(FakeAlertDialog.instances) == 1
    FakeAlertDialog.instances[0].emit_response("open")
    assert window.created == [(conns, None)]
