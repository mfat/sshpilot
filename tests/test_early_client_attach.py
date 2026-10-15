"""The first daemon client builds the sidebar rows before the next frame.

``daemon_prelaunch`` often has a connected client before the window is built.
``MainWindow._apply_ready_early_client`` takes it on the GTK thread, and the
first client from either path builds the sidebar rows at once
(``_apply_startup_client_selection``), so the next frame already shows them;
the projection reset that the attach queued must then not rebuild them again.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sshpilot import daemon_prelaunch
from sshpilot.window import MainWindow


class _Client:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _finished_state(*, result=None, error=None, launcher=None, taken=False):
    state = daemon_prelaunch._Prelaunch(False, False)
    state.launcher = launcher
    state.result = result
    state.error = error
    state.taken = taken
    state.done.set()
    return state


@pytest.fixture(autouse=True)
def _no_prelaunch(monkeypatch):
    monkeypatch.setattr(daemon_prelaunch, "_state", None)
    yield


def _result():
    return SimpleNamespace(client=_Client(), process=None)


def test_ready_needs_a_finished_successful_untaken_attempt(monkeypatch):
    assert daemon_prelaunch.ready() is False

    running = daemon_prelaunch._Prelaunch(False, False)
    running.result = _result()
    monkeypatch.setattr(daemon_prelaunch, "_state", running)
    assert daemon_prelaunch.ready() is False

    monkeypatch.setattr(daemon_prelaunch, "_state", _finished_state(result=None))
    assert daemon_prelaunch.ready() is False  # eviction needed: window connects

    monkeypatch.setattr(
        daemon_prelaunch, "_state", _finished_state(error=RuntimeError("down"))
    )
    assert daemon_prelaunch.ready() is False

    monkeypatch.setattr(
        daemon_prelaunch, "_state", _finished_state(result=_result(), taken=True)
    )
    assert daemon_prelaunch.ready() is False

    monkeypatch.setattr(daemon_prelaunch, "_state", _finished_state(result=_result()))
    assert daemon_prelaunch.ready() is True


def test_ready_respects_the_callers_launcher(monkeypatch):
    own = object()
    monkeypatch.setattr(
        daemon_prelaunch, "_state", _finished_state(result=_result(), launcher=own)
    )

    assert daemon_prelaunch.ready(own) is True
    assert daemon_prelaunch.ready(object()) is False
    assert daemon_prelaunch.ready() is True


class _EarlyWindow:
    _apply_ready_early_client = MainWindow._apply_ready_early_client
    _apply_startup_client_selection = MainWindow._apply_startup_client_selection
    on_projection_reset = MainWindow.on_projection_reset
    _flush_projection_rebuild = MainWindow._flush_projection_rebuild
    _sidebar_store_generation = MainWindow._sidebar_store_generation

    def __init__(self, generation=5):
        self.client = None
        self.selections = []
        self.rebuilds = 0
        self.populated = 0
        self.focus_calls = 0
        self._initial_connection_list_focus_done = False
        self.connection_manager = SimpleNamespace(
            generation=generation, connections=()
        )
        self.group_manager = SimpleNamespace(bind_connections=lambda _c: None)

    def _apply_client_selection(self, selection):
        self.selections.append(selection)
        self.client = selection.client

    def rebuild_connection_list(self):
        self._connection_list_rebuild_count = (
            getattr(self, '_connection_list_rebuild_count', 0) + 1
        )
        self.rebuilds += 1

    def _mark_sidebar_populated(self):
        self.populated += 1

    def _focus_connection_list_first_row(self):
        self.focus_calls += 1


def _capture_idle(monkeypatch):
    import sshpilot.window as window_module

    queued = []
    monkeypatch.setattr(
        window_module.GLib,
        "idle_add",
        lambda callback, *args, **_kwargs: queued.append((callback, args)),
    )

    def run_all():
        while queued:
            callback, args = queued.pop(0)
            callback(*args)

    return run_all


def test_ready_early_client_is_attached_and_rows_built_at_once(monkeypatch):
    run_idle = _capture_idle(monkeypatch)
    result = _result()
    monkeypatch.setattr(daemon_prelaunch, "_state", _finished_state(result=result))
    window = _EarlyWindow(generation=5)

    assert window._apply_ready_early_client(None) is True

    assert [s.client for s in window.selections] == [result.client]
    assert window.rebuilds == 1
    assert window.populated == 1

    # The reset queued by the attach arrives later: no second rebuild, but the
    # startup focus still happens.
    window.on_projection_reset(window.connection_manager, None)
    run_idle()
    assert window.rebuilds == 1
    assert window.focus_calls == 1

    # Later resets rebuild as usual.
    window.on_projection_reset(window.connection_manager, None)
    run_idle()
    assert window.rebuilds == 2


def test_a_newer_snapshot_after_the_early_rows_still_rebuilds(monkeypatch):
    run_idle = _capture_idle(monkeypatch)
    monkeypatch.setattr(daemon_prelaunch, "_state", _finished_state(result=_result()))
    window = _EarlyWindow(generation=5)
    window._apply_ready_early_client(None)

    window.connection_manager.generation = 6
    window.on_projection_reset(window.connection_manager, None)
    run_idle()

    assert window.rebuilds == 2


def test_no_ready_early_client_leaves_selection_to_the_bridge(monkeypatch):
    running = daemon_prelaunch._Prelaunch(False, False)
    monkeypatch.setattr(daemon_prelaunch, "_state", running)
    window = _EarlyWindow()

    assert window._apply_ready_early_client(None) is False
    assert window.selections == []
    assert window.rebuilds == 0


def test_an_early_client_for_another_launcher_is_not_taken(monkeypatch):
    monkeypatch.setattr(
        daemon_prelaunch,
        "_state",
        _finished_state(result=_result(), launcher=object()),
    )
    window = _EarlyWindow()

    assert window._apply_ready_early_client(object()) is False
    assert window.selections == []


def test_bridge_selection_builds_rows_only_for_the_first_client(monkeypatch):
    run_idle = _capture_idle(monkeypatch)
    window = _EarlyWindow(generation=3)

    window._apply_startup_client_selection(_result())
    assert window.rebuilds == 1
    window.on_projection_reset(window.connection_manager, None)
    run_idle()
    assert window.rebuilds == 1

    # A reconnect's selection rebuilds through its projection reset instead.
    window._apply_startup_client_selection(_result())
    assert window.rebuilds == 1
    window.on_projection_reset(window.connection_manager, None)
    run_idle()
    assert window.rebuilds == 2


def test_a_selection_dropped_while_quitting_builds_nothing():
    window = _EarlyWindow()

    def quitting(selection):
        selection.client.close()

    window._apply_client_selection = quitting
    window._apply_startup_client_selection(_result())

    assert window.rebuilds == 0
    assert window.populated == 0


def test_bridge_selection_is_delivered_ahead_of_the_frame_clock(monkeypatch):
    import sshpilot.window as window_module

    submits = []

    class _Bridge:
        def submit(self, operation, **kwargs):
            submits.append(kwargs)
            return object()

    monkeypatch.setattr(daemon_prelaunch, "_state", None)
    window = MainWindow.__new__(MainWindow)
    window.client_bridge = _Bridge()
    window.get_application = lambda: None

    MainWindow._begin_daemon_client_selection(window)

    assert submits[0]["priority"] == window_module.GLib.PRIORITY_HIGH_IDLE
    assert submits[0]["on_success"] == window._apply_startup_client_selection
