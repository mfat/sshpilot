"""The sort is a display choice that survives daemon refreshes and restarts.

Sorting reorders the ``GroupManager`` projection, which the daemon replaces on
every ``projection-reset`` (opening a connection stamps ``last_used``, which is
one). These tests pin the contract from issue #1312: every rebuild re-applies
the selected sort, the choice is saved in config and loaded at startup
(A-Z when nothing is saved), and a drag in a sorted view first saves the
sorted order as the manual order.
"""

from types import SimpleNamespace

import pytest

from sshpilot.connection_sort import (
    CONNECTION_SORT_MENU,
    CONNECTION_SORT_PRESETS,
    CONNECTION_SORT_SETTING,
    DEFAULT_CONNECTION_SORT,
    MANUAL_CONNECTION_SORT,
    apply_connection_sort,
    layout_request_from_projection,
    layout_request_from_snapshot,
    load_connection_sort,
)

try:
    import sshpilot.window as window_module
    from sshpilot.window import MainWindow
except Exception:  # pragma: no cover - depends on GTK test stub state
    window_module = None
    MainWindow = None

needs_window = pytest.mark.skipif(
    MainWindow is None,
    reason="GTK stubs unavailable or polluted by sibling tests",
)


class _Connection(SimpleNamespace):
    def __init__(self, nickname, connection_id=None):
        super().__init__(
            id=connection_id or nickname,
            nickname=nickname,
            hostname="",
            host=nickname,
            display_name="",
        )


class _GroupManager:
    """Projection double: ``bind_connections`` restores the daemon ordering."""

    def __init__(self, daemon_order):
        self._daemon_order = list(daemon_order)
        self.root_connections = list(daemon_order)
        self.groups = {}
        self.bind_calls = 0

    def bind_connections(self, _connections):
        self.bind_calls += 1
        self.root_connections = list(self._daemon_order)


class _ConnectionManager:
    def __init__(self, connections, generation=7):
        self.connections = list(connections)
        self.generation = generation

    def get_connections(self):
        return list(self.connections)

    def snapshot(self):
        return SimpleNamespace(
            generation=self.generation,
            groups=(),
            root_connection_ids=tuple(c.id for c in self.connections),
        )


class _Config:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


def _window(daemon_order=("zulu", "alpha"), sort="name-desc"):
    connections = [_Connection(name) for name in daemon_order]
    window = SimpleNamespace(
        group_manager=_GroupManager(daemon_order),
        connection_manager=_ConnectionManager(connections),
        config=_Config(),
        toast_overlay=None,
        sort_button=None,
        _connection_sort_last=sort,
        _initial_connection_list_focus_done=True,
        rebuild_calls=[],
        notified=[],
    )
    for name in (
        "_set_connection_sort",
        "_apply_connection_sort_to_projection",
        "apply_connection_sort_preset",
        "begin_saving_sorted_view",
        "sorted_view_save_failed",
    ):
        setattr(window, name, getattr(MainWindow, name).__get__(window))

    def rebuild():
        # The real rebuild re-applies the sort before building rows.
        window._apply_connection_sort_to_projection()
        window.rebuild_calls.append(list(window.group_manager.root_connections))

    window.rebuild_connection_list = rebuild
    window._update_sort_button = lambda: None
    window._notify_sort_result = lambda preset: window.notified.append(preset)
    return window


def test_manual_preset_is_a_no_op_on_the_projection():
    manager = _GroupManager(["zulu", "alpha"])
    connections = [_Connection("zulu"), _Connection("alpha")]

    assert apply_connection_sort(manager, connections, MANUAL_CONNECTION_SORT) is False
    # Manual order *is* the daemon order, so nothing may be reshuffled locally.
    assert manager.root_connections == ["zulu", "alpha"]


def test_a_to_z_is_the_default():
    assert DEFAULT_CONNECTION_SORT == "name-asc"
    assert CONNECTION_SORT_PRESETS[MANUAL_CONNECTION_SORT].manual is True


def test_startup_loads_the_saved_sort():
    assert load_connection_sort(_Config()) == "name-asc"
    assert load_connection_sort(_Config({CONNECTION_SORT_SETTING: "manual"})) == "manual"
    assert load_connection_sort(_Config({CONNECTION_SORT_SETTING: "name-desc"})) == "name-desc"
    # An unknown value (older build, hand edit) falls back to the default.
    assert load_connection_sort(_Config({CONNECTION_SORT_SETTING: "size"})) == "name-asc"


def test_sort_menu_offers_each_preset_once():
    assert CONNECTION_SORT_MENU == ("name-asc", "name-desc", MANUAL_CONNECTION_SORT)
    assert set(CONNECTION_SORT_MENU) == set(CONNECTION_SORT_PRESETS)


class _Action:
    """Stands in for the stateful ``win.sort-connections`` action."""

    def __init__(self, state):
        self.state = state
        self.set_calls = 0

    def get_state(self):
        return SimpleNamespace(get_string=lambda: self.state)

    def set_state(self, variant):
        self.set_calls += 1
        self.state = variant.get_string()


@needs_window
def test_choosing_a_sort_moves_the_menu_radio_mark(monkeypatch):
    monkeypatch.setattr(
        window_module.GLib,
        "Variant",
        lambda _type, value: SimpleNamespace(get_string=lambda: value),
        raising=False,
    )
    window = _window(sort=MANUAL_CONNECTION_SORT)
    window.sort_connections_action = _Action(MANUAL_CONNECTION_SORT)
    window._update_sort_button = MainWindow._update_sort_button.__get__(window)

    window.apply_connection_sort_preset("name-desc")

    assert window.sort_connections_action.state == "name-desc"
    # A drag in a sorted view switches to manual; the mark follows.
    window.begin_saving_sorted_view()
    assert window.sort_connections_action.state == MANUAL_CONNECTION_SORT


@needs_window
def test_menu_selection_applies_the_chosen_preset():
    applied = []
    window = SimpleNamespace(apply_connection_sort_preset=applied.append)

    MainWindow.on_sort_connections_action(
        window, None, SimpleNamespace(get_string=lambda: "name-desc")
    )

    assert applied == ["name-desc"]


@needs_window
def test_projection_reset_keeps_the_sort():
    # Issue #1312: opening a connection refreshes the projection, which used
    # to drop the sort back to manual order.
    window = _window(sort="name-asc")

    MainWindow.on_projection_reset(window, SimpleNamespace(connections=[]))

    assert window._connection_sort_last == "name-asc"
    assert window.group_manager.bind_calls == 1
    assert window.rebuild_calls == [["alpha", "zulu"]]


@needs_window
def test_choosing_a_sort_saves_it_and_shows_it():
    window = _window(sort=MANUAL_CONNECTION_SORT)

    window.apply_connection_sort_preset("name-desc")

    assert window.config.values[CONNECTION_SORT_SETTING] == "name-desc"
    assert window.rebuild_calls == [["zulu", "alpha"]]
    assert window.notified == [CONNECTION_SORT_PRESETS["name-desc"]]


@needs_window
def test_choosing_manual_restores_the_daemon_ordering():
    window = _window(daemon_order=("zulu", "mike", "alpha"), sort="name-asc")
    window.rebuild_connection_list()
    assert window.group_manager.root_connections == ["alpha", "mike", "zulu"]

    window.apply_connection_sort_preset(MANUAL_CONNECTION_SORT)

    assert window.group_manager.root_connections == ["zulu", "mike", "alpha"]
    assert window.config.values[CONNECTION_SORT_SETTING] == MANUAL_CONNECTION_SORT


@needs_window
def test_unknown_preset_falls_back_to_the_default():
    window = _window(sort=MANUAL_CONNECTION_SORT)

    window.apply_connection_sort_preset("not-a-preset")

    assert window._connection_sort_last == DEFAULT_CONNECTION_SORT


@needs_window
def test_nothing_to_save_in_manual_order():
    window = _window(sort=MANUAL_CONNECTION_SORT)

    assert window.begin_saving_sorted_view() is None
    assert CONNECTION_SORT_SETTING not in window.config.values


@needs_window
def test_drag_in_a_sorted_view_saves_what_is_on_screen():
    window = _window(daemon_order=("zulu", "mike", "alpha"), sort="name-asc")
    window.rebuild_connection_list()

    layout, undo, sorted_preset = window.begin_saving_sorted_view(expected_generation=5)

    assert layout.root_connection_ids == ("alpha", "mike", "zulu")
    assert layout.expected_generation == 5
    # Undo restores the daemon's order as it was before the drag.
    assert undo.root_connection_ids == ("zulu", "mike", "alpha")
    assert undo.expected_generation is None
    assert sorted_preset == "name-asc"
    # The view is manual from now on, and the next rebuild must not re-sort
    # the projection (it already holds the order being saved).
    assert window._connection_sort_last == MANUAL_CONNECTION_SORT
    assert window.config.values[CONNECTION_SORT_SETTING] == MANUAL_CONNECTION_SORT


@needs_window
def test_drag_uses_the_current_generation_when_none_was_captured():
    window = _window(sort="name-asc")
    window.connection_manager.generation = 11

    layout, _undo, _preset = window.begin_saving_sorted_view()

    assert layout.expected_generation == 11


@needs_window
def test_a_failed_save_goes_back_to_the_sorted_view():
    window = _window(daemon_order=("zulu", "alpha"), sort="name-asc")
    window.begin_saving_sorted_view()

    window.sorted_view_save_failed("name-asc")

    assert window._connection_sort_last == "name-asc"
    assert window.config.values[CONNECTION_SORT_SETTING] == "name-asc"
    assert window.rebuild_calls[-1] == ["alpha", "zulu"]


def _group(gid, order, parent=None, members=()):
    return SimpleNamespace(
        id=gid, parent_id=parent, order=order, connection_ids=tuple(members),
    )


def test_snapshot_layout_keeps_sibling_order_and_membership():
    snapshot = SimpleNamespace(
        generation=3,
        # Ties on ``order`` keep the daemon's sequence, as the sidebar does.
        groups=(
            _group("web", 1, members=("w2", "w1")),
            _group("db", 0, members=("d1",)),
            _group("sub", 0, parent="web", members=("s1",)),
            _group("ops", 1),
        ),
        root_connection_ids=("r2", "r1"),
    )

    request = layout_request_from_snapshot(snapshot)

    assert request.root_connection_ids == ("r2", "r1")
    assert [g.group_id for g in request.groups] == ["db", "sub", "web", "ops"]
    by_id = {g.group_id: g for g in request.groups}
    assert by_id["web"].connection_ids == ("w2", "w1")
    assert by_id["sub"].parent_id == "web"
    assert by_id["db"].parent_id is None


def test_projection_layout_matches_the_sorted_projection():
    manager = _GroupManager(["zulu", "alpha"])
    manager.groups = {
        "b-group": {"name": "Beta", "parent_id": None, "connections": ["y", "x"], "order": 0},
        "a-group": {"name": "Alpha", "parent_id": None, "connections": [], "order": 1},
    }
    apply_connection_sort(
        manager,
        [_Connection(n) for n in ("zulu", "alpha", "x", "y")],
        "name-asc",
    )

    request = layout_request_from_projection(manager, expected_generation=9)

    assert request.root_connection_ids == ("alpha", "zulu")
    assert [g.group_id for g in request.groups] == ["a-group", "b-group"]
    assert request.groups[1].connection_ids == ("x", "y")
    assert request.expected_generation == 9


class _UndoController:
    def __init__(self):
        self.operations = []

    def run(self, operation, *, on_success, on_error):
        self.operations.append(operation)
        on_success(operation())


def _undo_window(daemon_order, generation):
    window = _window(daemon_order=daemon_order, sort=MANUAL_CONNECTION_SORT)
    window.connection_manager.generation = generation
    window.group_manager.controller = _UndoController()
    window.client = SimpleNamespace(
        restored=[],
        set_connection_layout=lambda request: window.client.restored.append(request) or 99,
    )
    window.refusals = []
    window._refuse_sorted_view_undo = lambda: window.refusals.append(True)
    return window


@needs_window
def test_undo_restores_when_only_the_generation_moved():
    # Opening a connection stamps last_used, which bumps the generation but
    # leaves the arrangement alone; Undo must still work.
    window = _undo_window(("alpha", "zulu"), generation=5)
    saved = layout_request_from_snapshot(window.connection_manager.snapshot())
    window.connection_manager.generation = 6

    MainWindow._undo_sorted_view_save(window, "undo-layout", "name-asc", saved)

    assert window.client.restored == ["undo-layout"]
    assert window.refusals == []
    assert window._connection_sort_last == "name-asc"


@needs_window
def test_undo_is_refused_after_a_later_rearrangement():
    window = _undo_window(("alpha", "zulu"), generation=5)
    saved = layout_request_from_snapshot(window.connection_manager.snapshot())
    # A second drag reorders the list while the Undo toast is still up.
    window.connection_manager.connections.reverse()

    MainWindow._undo_sorted_view_save(window, "undo-layout", "name-asc", saved)

    assert window.client.restored == []
    assert window.group_manager.controller.operations == []
    assert window.refusals == [True]
    assert window._connection_sort_last == MANUAL_CONNECTION_SORT


@needs_window
def test_picking_the_current_menu_choice_does_nothing():
    applied = []
    window = SimpleNamespace(
        _connection_sort_last="name-asc",
        apply_connection_sort_preset=applied.append,
    )

    MainWindow.on_sort_connections_action(
        window, None, SimpleNamespace(get_string=lambda: "name-asc")
    )

    assert applied == []
