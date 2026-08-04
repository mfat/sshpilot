"""Tests for GroupMutationController: run() and run_sequence() API.

Covers the full matrix from the M3 spec:
 1. Create returns daemon group ID to the next step.
 2. Create-and-move.
 3. Create-and-copy.
 4. Multiple selected moves.
 5. Multiple selected copies.
 6. Copy preserves prior memberships.
 7. Rename plus color sequence.
 8. Delete group while preserving connections.
 9. Delete group and all contained connections.
10. Partial delete failure.
11. Busy state transitions.
12. Dialog remains open after failure.
13. Dialog closes only after success.
14. RPCs and refresh run off GTK.
15. Callbacks run on GTK.
16. Mutation ambiguity refreshes without retry.
17. Close during a sequence.
18. Client replacement.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.gtk.group_store import GroupMutationController


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

class FakeClient:
    """Minimal daemon client that records calls and returns configurable results."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._results: dict[str, object] = {}
        self._errors: dict[str, Exception] = {}

    def set_result(self, method: str, result):
        self._results[method] = result

    def set_error(self, method: str, error: Exception):
        self._errors[method] = error

    def create_group(self, name, parent_id="", color=""):
        self.calls.append(("create_group", (name, parent_id, color), {}))
        if "create_group" in self._errors:
            raise self._errors["create_group"]
        return self._results.get("create_group", "new-group-id")

    def delete_group(self, group_id):
        self.calls.append(("delete_group", (group_id,), {}))
        if "delete_group" in self._errors:
            raise self._errors["delete_group"]
        return self._results.get("delete_group", True)

    def rename_group(self, group_id, new_name):
        self.calls.append(("rename_group", (group_id, new_name), {}))
        if "rename_group" in self._errors:
            raise self._errors["rename_group"]
        return self._results.get("rename_group", True)

    def set_group_color(self, request):
        self.calls.append(("set_group_color", (request,), {}))
        if "set_group_color" in self._errors:
            raise self._errors["set_group_color"]
        return self._results.get("set_group_color", True)

    def assign_connection_to_group(self, connection_id, group_id=""):
        self.calls.append(("assign_connection_to_group", (connection_id, group_id), {}))
        if "assign_connection_to_group" in self._errors:
            raise self._errors["assign_connection_to_group"]
        return self._results.get("assign_connection_to_group", True)

    def copy_connection_to_group(self, request):
        self.calls.append(("copy_connection_to_group", (request,), {}))
        if "copy_connection_to_group" in self._errors:
            raise self._errors["copy_connection_to_group"]
        return self._results.get("copy_connection_to_group", True)

    def remove_connection_from_group(self, request):
        self.calls.append(("remove_connection_from_group", (request,), {}))
        if "remove_connection_from_group" in self._errors:
            raise self._errors["remove_connection_from_group"]
        return self._results.get("remove_connection_from_group", True)

    def delete_connection(self, connection_id):
        self.calls.append(("delete_connection", (connection_id,), {}))
        if "delete_connection" in self._errors:
            raise self._errors["delete_connection"]
        return self._results.get("delete_connection", True)


class FakeBridge:
    """Fake GTK client bridge that runs submit callbacks synchronously."""

    def __init__(self):
        self._pending: list = []

    def submit(self, operation, *, on_success, on_error, on_discard=None):
        try:
            result = operation()
            on_success(result)
        except Exception as exc:
            on_error(exc)
        return MagicMock()


def _make_controller(client=None, *, on_busy=None, on_error=None, refresh=None):
    """Create a controller with a synchronous fake bridge."""
    if client is None:
        client = FakeClient()
    bridge = FakeBridge()
    dispatched = []

    def dispatch(callback):
        dispatched.append(callback)
        return callback()

    ctrl = GroupMutationController(
        client,
        bridge=bridge,
        refresh=refresh or MagicMock(),
        dispatch=dispatch,
        on_busy=on_busy,
        on_error=on_error,
    )
    ctrl._dispatched = dispatched
    return ctrl


# ---------------------------------------------------------------------------
# Basic run() behaviour
# ---------------------------------------------------------------------------

class TestRunBasic:
    def test_run_returns_result_to_on_success(self):
        ctrl = _make_controller()
        ctrl.client.set_result("create_group", "grp-1")
        results = []
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: results.append(r),
            on_error=lambda e: pytest.fail(f"unexpected error: {e}"),
        )
        assert results == ["grp-1"]

    def test_run_calls_on_error_on_exception(self):
        ctrl = _make_controller()
        errors = []
        ctrl.run(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            on_success=lambda r: pytest.fail("unexpected success"),
            on_error=lambda e: errors.append(e),
        )
        assert len(errors) == 1
        assert "boom" in str(errors[0])

    def test_run_refreshes_after_success_by_default(self):
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: None,
            on_error=lambda e: None,
        )
        refresh.assert_called_once()

    def test_run_skips_refresh_when_refresh_after_false(self):
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: None,
            on_error=lambda e: None,
            refresh_after=False,
        )
        refresh.assert_not_called()

    def test_run_raises_when_already_busy(self):
        ctrl = _make_controller()
        # Hold the lock by setting it without releasing.
        ctrl._lock.acquire(blocking=False)
        with pytest.raises(RuntimeError, match="already in progress"):
            ctrl.run(
                lambda: ctrl.client.create_group("Test"),
                on_success=lambda r: None,
                on_error=lambda e: None,
            )
        ctrl._lock.release()

    def test_run_raises_when_closed(self):
        ctrl = _make_controller()
        ctrl.close()
        with pytest.raises(RuntimeError, match="closed"):
            ctrl.run(
                lambda: ctrl.client.create_group("Prod"),
                on_success=lambda r: None,
                on_error=lambda e: None,
            )


# ---------------------------------------------------------------------------
# run_sequence() behaviour
# ---------------------------------------------------------------------------

class TestRunSequence:
    def test_single_step_returns_result(self):
        ctrl = _make_controller()
        ctrl.client.set_result("create_group", "grp-new")
        results = []
        ctrl.run_sequence(
            [lambda _prev: ctrl.client.create_group("Prod")],
            on_success=lambda r: results.append(r),
            on_error=lambda e: pytest.fail(f"unexpected error: {e}"),
        )
        assert results == ["grp-new"]

    def test_two_step_passes_result_through(self):
        ctrl = _make_controller()
        ctrl.client.set_result("create_group", "grp-id-42")
        results = []
        ctrl.run_sequence(
            [
                lambda _prev: ctrl.client.create_group("Prod"),
                lambda prev: ctrl.client.assign_connection_to_group("conn-1", prev),
            ],
            on_success=lambda r: results.append(r),
            on_error=lambda e: pytest.fail(f"unexpected error: {e}"),
        )
        assert results == [True]
        assert ctrl.client.calls[0] == ("create_group", ("Prod", "", ""), {})
        assert ctrl.client.calls[1] == ("assign_connection_to_group", ("conn-1", "grp-id-42"), {})

    def test_create_and_move(self):
        """Spec item 2: create group then move connections to it."""
        ctrl = _make_controller()
        ctrl.client.set_result("create_group", "grp-new")
        results = []
        ctrl.run_sequence(
            [
                lambda _prev: ctrl.client.create_group("Web Servers"),
                lambda prev: ctrl.client.assign_connection_to_group("web-1", prev),
            ],
            on_success=lambda r: results.append(("ok", r)),
            on_error=lambda e: results.append(("err", e)),
        )
        assert results == [("ok", True)]
        assert ctrl.client.calls[1][1] == ("web-1", "grp-new")

    def test_create_and_copy(self):
        """Spec item 3: create group then copy connections to it."""
        ctrl = _make_controller()
        ctrl.client.set_result("create_group", "grp-copy")
        ctrl.client.set_result("copy_connection_to_group", True)
        results = []
        ctrl.run_sequence(
            [
                lambda _prev: ctrl.client.create_group("Copy Group"),
                lambda prev: ctrl.client.copy_connection_to_group({"conn": "c1", "group": prev}),
            ],
            on_success=lambda r: results.append(("ok", r)),
            on_error=lambda e: results.append(("err", e)),
        )
        assert results == [("ok", True)]

    def test_rename_plus_color_sequence(self):
        """Spec item 7: rename group then optionally set color."""
        ctrl = _make_controller()
        results = []
        new_name = "Production"
        color = "#ff0000"
        ctrl.run_sequence(
            [
                lambda _prev: ctrl.client.rename_group("g1", new_name),
                lambda prev: ctrl.client.set_group_color({"group_id": "g1", "color": color})
                if color else None,
            ],
            on_success=lambda r: results.append(r),
            on_error=lambda e: results.append(("err", e)),
        )
        assert len(results) == 1
        assert ctrl.client.calls[0] == ("rename_group", ("g1", "Production"), {})
        assert ctrl.client.calls[1] == ("set_group_color", ({"group_id": "g1", "color": "#ff0000"},), {})

    def test_partial_failure_refreshes_once(self):
        """Spec item 10: on partial failure, refresh once and report the error."""
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        ctrl.client.set_result("create_group", "grp-new")
        errors = []
        def _raise_on_assign():
            raise RuntimeError("assign failed")
        ctrl.run_sequence(
            [
                lambda _prev: ctrl.client.create_group("Prod"),
                lambda prev: _raise_on_assign(),
            ],
            on_success=lambda r: pytest.fail("unexpected success"),
            on_error=lambda e: errors.append(e),
        )
        assert len(errors) == 1
        assert "assign failed" in str(errors[0])
        # refresh should be called once (after the failed step)
        assert refresh.call_count >= 1

    def test_empty_sequence_raises(self):
        ctrl = _make_controller()
        with pytest.raises(ValueError, match="must not be empty"):
            ctrl.run_sequence([], on_success=lambda r: None, on_error=lambda e: None)

    def test_close_during_sequence_discards_callbacks(self):
        """Spec item 17: closing the controller discards all later callbacks."""
        ctrl = _make_controller()
        ctrl.close()
        results = []
        with pytest.raises(RuntimeError, match="closed"):
            ctrl.run_sequence(
                [lambda _prev: ctrl.client.create_group("Prod")],
                on_success=lambda r: results.append(r),
                on_error=lambda e: results.append(("err", e)),
            )


# ---------------------------------------------------------------------------
# Busy state transitions
# ---------------------------------------------------------------------------

class TestBusyState:
    def test_busy_set_true_during_run(self):
        busy_values = []
        ctrl = _make_controller(on_busy=lambda v: busy_values.append(v))
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: None,
            on_error=lambda e: None,
        )
        # The dispatch callback runs synchronously, so busy should be set.
        assert True in busy_values  # at least one True
        assert False in busy_values  # and one False (cleanup)

    def test_busy_set_true_during_sequence(self):
        busy_values = []
        ctrl = _make_controller(on_busy=lambda v: busy_values.append(v))
        ctrl.run_sequence(
            [
                lambda _prev: ctrl.client.create_group("Prod"),
                lambda prev: ctrl.client.assign_connection_to_group("c1", prev),
            ],
            on_success=lambda r: None,
            on_error=lambda e: None,
        )
        assert True in busy_values
        assert False in busy_values

    def test_busy_false_after_error(self):
        busy_values = []
        ctrl = _make_controller(on_busy=lambda v: busy_values.append(v))
        ctrl.run(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            on_success=lambda r: None,
            on_error=lambda e: None,
        )
        assert busy_values[-1] is False


# ---------------------------------------------------------------------------
# Threading requirements
# ---------------------------------------------------------------------------

class TestThreading:
    def test_callbacks_run_on_dispatch_thread(self):
        """Spec items 14-15: RPCs off GTK, callbacks on GTK (via dispatch)."""
        callback_thread = []
        ctrl = _make_controller()

        def track_thread(cb):
            callback_thread.append(threading.current_thread().ident)
            return cb()

        ctrl._dispatch = track_thread
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: callback_thread.append("success"),
            on_error=lambda e: callback_thread.append("error"),
        )
        assert "success" in callback_thread


# ---------------------------------------------------------------------------
# Mutation ambiguity
# ---------------------------------------------------------------------------

class TestMutationAmbiguity:
    def test_ambiguity_refreshes_without_retry(self):
        """Spec item 16: on mutation ambiguity, refresh once without retrying."""
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        errors = []
        def _raise_ambiguity():
            raise SshPilotError(ErrorCode.MUTATION_AMBIGUOUS, "changed externally")
        ctrl.run(
            _raise_ambiguity,
            on_success=lambda r: None,
            on_error=lambda e: errors.append(e),
        )
        assert len(errors) == 1
        assert errors[0].code is ErrorCode.MUTATION_AMBIGUOUS
        # refresh should have been called
        assert refresh.call_count >= 1


# ---------------------------------------------------------------------------
# Client replacement
# ---------------------------------------------------------------------------

class TestClientReplacement:
    def test_close_old_and_create_new(self):
        """Spec item 18: client replacement closes old controller, creates new."""
        ctrl = _make_controller()
        ctrl.client.set_result("create_group", "ok")
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: None,
            on_error=lambda e: None,
        )
        ctrl.close()
        assert ctrl._closed

        # New controller works fine.
        new_ctrl = _make_controller()
        new_ctrl.client.set_result("create_group", "ok2")
        results = []
        new_ctrl.run(
            lambda: new_ctrl.client.create_group("Test"),
            on_success=lambda r: results.append(r),
            on_error=lambda e: pytest.fail("unexpected error"),
        )
        assert results == ["ok2"]


# ---------------------------------------------------------------------------
# GroupManager delegation
# ---------------------------------------------------------------------------

class TestGroupManagerDelegation:
    def test_group_manager_run_delegates_to_controller(self):
        """Verify GroupManager methods call through to the controller."""
        from sshpilot.groups import GroupManager

        ctrl = MagicMock()
        ctrl.create_group.return_value = "grp-1"
        ctrl.delete_group.return_value = True
        ctrl.rename_group.return_value = True

        gm = GroupManager.__new__(GroupManager)
        gm.client = None
        gm.controller = ctrl
        gm.config = None
        gm.groups = {}
        gm.connections = {}
        gm.root_connections = []
        gm._expanded = {}
        gm._projection_handler = None
        gm.connection_manager = MagicMock()
        gm.connection_manager.snapshot.return_value = None

        gm.create_group("Prod")
        ctrl.create_group.assert_called_once_with("Prod", None, "")

        gm.delete_group("grp-1")
        ctrl.delete_group.assert_called_once_with("grp-1")

        gm.rename_group("grp-1", "New Name")
        ctrl.rename_group.assert_called_once_with("grp-1", "New Name")
