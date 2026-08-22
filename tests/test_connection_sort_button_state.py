"""The sort button is a UI-only overlay; its state must track what's on screen.

Sorting reorders the ``GroupManager`` projection, which the daemon replaces on
every ``projection-reset``. These tests pin the resulting contract: the button
falls back to "manual" whenever that happens, never persists a preset across
restarts, and never re-applies itself during an ordinary sidebar rebuild.
"""

from types import SimpleNamespace

import pytest

from sshpilot.connection_sort import (
    CONNECTION_SORT_PRESETS,
    DEFAULT_CONNECTION_SORT,
    MANUAL_CONNECTION_SORT,
    apply_connection_sort,
)

try:
    import sshpilot.window as window_module
    from sshpilot.window import MainWindow
except Exception:  # pragma: no cover - depends on GTK test stub state
    window_module = None
    MainWindow = None

pytestmark = pytest.mark.skipif(
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
    def __init__(self, connections):
        self.connections = list(connections)

    def get_connections(self):
        return list(self.connections)


class _ExplodingConfig:
    """Any write here means the stale-preset persistence came back."""

    def get_setting(self, key, default=None):
        raise AssertionError(f"sort must not read config ({key})")

    def set_setting(self, key, value):
        raise AssertionError(f"sort must not persist config ({key}={value})")


def _window(daemon_order=("zulu", "alpha"), sort="name-desc"):
    connections = [_Connection(name) for name in daemon_order]
    return SimpleNamespace(
        group_manager=_GroupManager(daemon_order),
        connection_manager=_ConnectionManager(connections),
        config=_ExplodingConfig(),
        toast_overlay=None,
        sort_button=None,
        _connection_sort_last=sort,
        _initial_connection_list_focus_done=True,
        rebuild_calls=[],
        button_updates=[],
        notified=[],
    )


def _wire(window):
    """Attach the collaborators the unbound MainWindow methods call on self."""
    window.rebuild_connection_list = lambda: window.rebuild_calls.append(True)
    window._update_sort_button = lambda: window.button_updates.append(
        window._connection_sort_last
    )
    window._notify_sort_result = lambda preset: window.notified.append(preset)
    window._reset_sort_to_manual = lambda: MainWindow._reset_sort_to_manual(window)
    return window


def test_manual_preset_is_a_no_op_on_the_projection():
    manager = _GroupManager(["zulu", "alpha"])
    connections = [_Connection("zulu"), _Connection("alpha")]

    assert apply_connection_sort(manager, connections, MANUAL_CONNECTION_SORT) is False
    # Manual order *is* the daemon order, so nothing may be reshuffled locally.
    assert manager.root_connections == ["zulu", "alpha"]


def test_manual_is_the_startup_default():
    # Nothing applies a sort at startup, so the default must not claim one.
    assert DEFAULT_CONNECTION_SORT == MANUAL_CONNECTION_SORT
    assert CONNECTION_SORT_PRESETS[MANUAL_CONNECTION_SORT].manual is True
    assert CONNECTION_SORT_PRESETS["name-asc"].manual is False


def test_sort_button_cycles_manual_then_ascending_then_descending():
    window = SimpleNamespace()
    nxt = lambda current: MainWindow._next_sort_preset_id(window, current)

    assert nxt(MANUAL_CONNECTION_SORT) == "name-asc"
    assert nxt("name-asc") == "name-desc"
    assert nxt("name-desc") == MANUAL_CONNECTION_SORT
    # An unknown preset lands on manual rather than inventing a sort.
    assert nxt("size-asc") == MANUAL_CONNECTION_SORT


def test_projection_reset_drops_a_sort_the_daemon_just_overwrote():
    window = _wire(_window(sort="name-desc"))
    manager = SimpleNamespace(connections=[])

    MainWindow.on_projection_reset(window, manager)

    assert window._connection_sort_last == MANUAL_CONNECTION_SORT
    assert window.button_updates == [MANUAL_CONNECTION_SORT]
    assert window.group_manager.bind_calls == 1
    assert window.rebuild_calls == [True]


def test_projection_reset_leaves_an_already_manual_button_alone():
    window = _wire(_window(sort=MANUAL_CONNECTION_SORT))

    MainWindow.on_projection_reset(window, SimpleNamespace(connections=[]))

    assert window._connection_sort_last == MANUAL_CONNECTION_SORT
    # No spurious icon/tooltip churn on every daemon refresh.
    assert window.button_updates == []


def test_applying_a_sort_reorders_and_never_touches_config():
    window = _wire(_window(daemon_order=("zulu", "alpha"), sort=MANUAL_CONNECTION_SORT))

    MainWindow.apply_connection_sort_preset(window, "name-asc")

    assert window.group_manager.root_connections == ["alpha", "zulu"]
    assert window._connection_sort_last == "name-asc"
    assert window.rebuild_calls == [True]
    assert window.notified == [CONNECTION_SORT_PRESETS["name-asc"]]


def test_choosing_manual_restores_the_daemon_ordering():
    window = _wire(_window(daemon_order=("zulu", "alpha"), sort=MANUAL_CONNECTION_SORT))
    MainWindow.apply_connection_sort_preset(window, "name-asc")
    assert window.group_manager.root_connections == ["alpha", "zulu"]

    MainWindow.apply_connection_sort_preset(window, MANUAL_CONNECTION_SORT)

    assert window.group_manager.root_connections == ["zulu", "alpha"]
    assert window._connection_sort_last == MANUAL_CONNECTION_SORT
    assert window.group_manager.bind_calls == 1
    assert window.rebuild_calls == [True, True]


def test_unknown_preset_falls_back_to_manual():
    window = _wire(_window(sort="name-asc"))

    MainWindow.apply_connection_sort_preset(window, "not-a-preset")

    assert window._connection_sort_last == MANUAL_CONNECTION_SORT
    assert window.group_manager.bind_calls == 1
