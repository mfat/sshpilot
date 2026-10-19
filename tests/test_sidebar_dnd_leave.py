"""The connection list must not unparent rows inside its drop "leave" (#1334).

GTK emits "leave" in the middle of a drop-crossing walk. If the pointer rests
on the placeholder's bar, removing the placeholder there leaves GTK walking a
detached widget up to NULL and the app segfaults. The leave handler defers the
cleanup to an idle, and coming back over the list cancels it.
"""

import types
from unittest.mock import MagicMock

import sshpilot.sidebar as sidebar_module


class _FakeGLib:
    SOURCE_REMOVE = False

    def __init__(self):
        self.pending = {}
        self.removed = []
        self._next = 1

    def idle_add(self, func, *args):
        source_id = self._next
        self._next += 1
        self.pending[source_id] = (func, args)
        return source_id

    def source_remove(self, source_id):
        self.removed.append(source_id)
        self.pending.pop(source_id, None)

    def run_idles(self):
        while self.pending:
            source_id = next(iter(self.pending))
            func, args = self.pending.pop(source_id)
            func(*args)


def _window():
    window = types.SimpleNamespace()
    window.connection_list = MagicMock()
    window._drag_in_progress = True
    window._connection_list_leave_idle_id = 0
    return window


def _patch(monkeypatch):
    fake = _FakeGLib()
    monkeypatch.setattr(sidebar_module, "GLib", fake)
    calls = []
    monkeypatch.setattr(sidebar_module, "_clear_drop_indicator", lambda w: calls.append("clear"))
    monkeypatch.setattr(sidebar_module, "_hide_ungrouped_area", lambda w: calls.append("hide"))
    monkeypatch.setattr(sidebar_module, "_stop_connection_autoscroll", lambda w: calls.append("stop"))
    return fake, calls


def test_leave_defers_row_removal_until_idle(monkeypatch):
    fake, calls = _patch(monkeypatch)
    window = _window()

    sidebar_module._on_connection_list_leave(window, None)

    assert calls == ["stop"]
    window.connection_list.set_selection_mode.assert_not_called()
    assert len(fake.pending) == 1

    fake.run_idles()

    assert calls == ["stop", "clear", "hide"]
    assert window._drag_in_progress is False
    window.connection_list.set_selection_mode.assert_called_once_with(
        sidebar_module.Gtk.SelectionMode.MULTIPLE
    )
    assert window._connection_list_leave_idle_id == 0


def test_repeated_leave_schedules_one_cleanup(monkeypatch):
    fake, _calls = _patch(monkeypatch)
    window = _window()

    sidebar_module._on_connection_list_leave(window, None)
    sidebar_module._on_connection_list_leave(window, None)

    assert len(fake.pending) == 1


def test_motion_cancels_pending_leave_cleanup(monkeypatch):
    fake, calls = _patch(monkeypatch)
    window = _window()

    sidebar_module._on_connection_list_leave(window, None)
    pending_id = window._connection_list_leave_idle_id
    monkeypatch.setattr(sidebar_module, "_update_connection_autoscroll", lambda w, y: None)
    monkeypatch.setattr(sidebar_module, "_row_at_y_or_nearest", lambda w, y: None)
    fake.get_monotonic_time = lambda: 100_000
    sidebar_module._on_connection_list_motion(window, None, 0, 10)

    assert fake.removed == [pending_id]
    assert window._connection_list_leave_idle_id == 0
    fake.run_idles()
    assert "hide" not in calls
    assert window._drag_in_progress is True


def test_connection_list_ignores_drags_from_other_apps(monkeypatch):
    target = MagicMock()
    monkeypatch.setattr(sidebar_module, "new_internal_drop_target", lambda: target)
    wired = []
    monkeypatch.setattr(sidebar_module, "accept_only_in_app_drops", wired.append)
    window = types.SimpleNamespace(connection_list=MagicMock())

    sidebar_module.setup_connection_list_dnd(window)

    assert wired == [target]
    assert window._connection_list_leave_idle_id == 0
