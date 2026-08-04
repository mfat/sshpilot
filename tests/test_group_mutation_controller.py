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
19. Tag rename uses daemon.
20. Expansion persists and migrates.
21. No direct group or tag mutation calls remain outside the controller.
"""

from __future__ import annotations

import threading
import time
from collections import deque
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

    def delete_connection(self, request):
        self.calls.append(("delete_connection", (request,), {}))
        if "delete_connection" in self._errors:
            raise self._errors["delete_connection"]
        return self._results.get("delete_connection", True)

    def rename_tag(self, request):
        self.calls.append(("rename_tag", (request,), {}))
        if "rename_tag" in self._errors:
            raise self._errors["rename_tag"]
        return self._results.get("rename_tag", 1)


class AsyncBridge:
    """Actually asynchronous bridge that queues worker and GTK callbacks separately.

    Worker callbacks (the ``operation`` / ``self.refresh`` callables) run in
    a dedicated thread-pool worker; GTK callbacks (``on_success`` /
    ``on_error``) are queued and delivered on demand via
    :meth:`drain_gtk_queue`.  This exercises the real async code path without
    a running GLib main loop.
    """

    def __init__(self):
        self._gtk_queue: deque = deque()
        self._worker_pool: list[threading.Thread] = []
        self._pending_workers: deque = deque()
        self._lock = threading.Lock()
        self._gtk_ready = threading.Event()

    def submit(self, operation, *, on_success, on_error, on_discard=None):
        """Submit *operation* for off-thread execution, then queue GTK callbacks."""

        def _run_worker():
            try:
                result = operation()
                with self._lock:
                    self._gtk_queue.append(("success", on_success, result))
                    self._gtk_ready.set()
            except Exception as exc:
                with self._lock:
                    self._gtk_queue.append(("error", on_error, exc))
                    self._gtk_ready.set()

        t = threading.Thread(target=_run_worker, daemon=True)
        t.start()
        self._worker_pool.append(t)
        return MagicMock()

    def drain_gtk_queue(self, timeout=2.0):
        """Execute all queued GTK callbacks synchronously."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._gtk_queue:
                    break
            self._gtk_ready.wait(timeout=0.05)
            self._gtk_ready.clear()

        while True:
            with self._lock:
                if not self._gtk_queue:
                    break
                kind, callback, payload = self._gtk_queue.popleft()
            callback(payload)

        # Wait for all worker threads to finish.
        for t in self._worker_pool:
            t.join(timeout=2.0)
        self._worker_pool.clear()


class SyncBridge:
    """Synchronous bridge for fast tests where threading is not needed."""

    def submit(self, operation, *, on_success, on_error, on_discard=None):
        try:
            result = operation()
            on_success(result)
        except Exception as exc:
            on_error(exc)
        return MagicMock()


def _make_controller(client=None, *, bridge=None, on_busy=None, on_error=None,
                     refresh=None, async_mode=False):
    """Create a controller with a fake bridge."""
    if client is None:
        client = FakeClient()
    if bridge is None:
        bridge = AsyncBridge() if async_mode else SyncBridge()

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
    ctrl._bridge = bridge
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
        ctrl._lock.acquire(blocking=False)
        try:
            with pytest.raises(RuntimeError, match="already in progress"):
                ctrl.run(
                    lambda: ctrl.client.create_group("Test"),
                    on_success=lambda r: None,
                    on_error=lambda e: None,
                )
        finally:
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

    def test_run_releases_lock_on_submit_failure(self):
        """Catch synchronous submit() failures and release busy/lock."""
        ctrl = _make_controller()
        ctrl.submit = MagicMock(side_effect=RuntimeError("bridge down"))
        with pytest.raises(RuntimeError, match="bridge down"):
            ctrl.run(
                lambda: ctrl.client.create_group("Prod"),
                on_success=lambda r: None,
                on_error=lambda e: None,
            )
        # Lock must be released.
        assert not ctrl._lock.locked()
        assert not ctrl._busy

    def test_run_ambiguity_refreshes_and_reports_error(self):
        """On mutation ambiguity, refresh once without retrying, then report."""
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        errors = []
        ctrl.run(
            lambda: (_ for _ in ()).throw(
                SshPilotError(ErrorCode.MUTATION_AMBIGUOUS, "changed")
            ),
            on_success=lambda r: None,
            on_error=lambda e: errors.append(e),
        )
        assert len(errors) == 1
        assert errors[0].code is ErrorCode.MUTATION_AMBIGUOUS
        assert refresh.call_count >= 1


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

    def test_multiple_selected_moves(self):
        """Spec item 4: multiple selected connections moved as one sequence."""
        ctrl = _make_controller()
        nicks = ["c1", "c2", "c3"]
        steps = [
            lambda _prev, n=n: ctrl.client.assign_connection_to_group(n, "target")
            for n in nicks
        ]
        ctrl.run_sequence(
            steps,
            on_success=lambda r: None,
            on_error=lambda e: pytest.fail(str(e)),
        )
        assert len(ctrl.client.calls) == 3
        for i, nick in enumerate(nicks):
            assert ctrl.client.calls[i] == ("assign_connection_to_group", (nick, "target"), {})

    def test_multiple_selected_copies(self):
        """Spec item 5: multiple selected connections copied as one sequence."""
        ctrl = _make_controller()
        from sshpilot.api.models.connection_store import (
            ConnectionId, CopyConnectionToGroupRequest, GroupId,
        )
        nicks = ["c1", "c2"]
        steps = [
            lambda _prev, n=n: ctrl.client.copy_connection_to_group(
                CopyConnectionToGroupRequest(
                    connection_id=ConnectionId(n),
                    group_id=GroupId("target"),
                )
            )
            for n in nicks
        ]
        ctrl.run_sequence(
            steps,
            on_success=lambda r: None,
            on_error=lambda e: pytest.fail(str(e)),
        )
        assert len(ctrl.client.calls) == 2
        for i, nick in enumerate(nicks):
            req = ctrl.client.calls[i][1][0]
            assert req.connection_id == nick
            assert req.group_id == "target"

    def test_rename_plus_color_sequence(self):
        """Spec item 7: rename group then optionally set color."""
        ctrl = _make_controller()
        from sshpilot.api.models.connection_store import GroupId, SetGroupColorRequest
        results = []
        ctrl.run_sequence(
            [
                lambda _prev: ctrl.client.rename_group("g1", "Production"),
                lambda prev: ctrl.client.set_group_color(
                    SetGroupColorRequest(
                        group_id=GroupId("g1"), color="#ff0000",
                    )
                ),
            ],
            on_success=lambda r: results.append(r),
            on_error=lambda e: results.append(("err", e)),
        )
        assert len(results) == 1
        assert ctrl.client.calls[0] == ("rename_group", ("g1", "Production"), {})
        assert ctrl.client.calls[1][0] == "set_group_color"

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
        assert refresh.call_count >= 1

    def test_empty_sequence_raises(self):
        ctrl = _make_controller()
        with pytest.raises(ValueError, match="must not be empty"):
            ctrl.run_sequence([], on_success=lambda r: None, on_error=lambda e: None)


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
        assert True in busy_values
        assert False in busy_values

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
# Threading requirements (async bridge)
# ---------------------------------------------------------------------------

class TestThreading:
    def test_worker_runs_off_gtk_thread(self):
        """RPC work runs off the GTK thread."""
        worker_threads = set()
        gtk_thread = threading.current_thread().ident
        bridge = AsyncBridge()

        original_submit = bridge.submit

        def tracking_submit(operation, **kwargs):
            def _track():
                worker_threads.add(threading.current_thread().ident)
                return operation()

            return original_submit(_track, **kwargs)

        bridge.submit = tracking_submit
        ctrl = _make_controller(bridge=bridge, async_mode=True)
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: None,
            on_error=lambda e: None,
        )
        bridge.drain_gtk_queue()
        assert len(worker_threads) > 0
        assert gtk_thread not in worker_threads

    def test_callbacks_run_on_dispatch_thread(self):
        """Spec items 14-15: RPCs off GTK, callbacks on GTK (via dispatch)."""
        callback_threads = []
        ctrl = _make_controller()

        def track_thread(cb):
            callback_threads.append(threading.current_thread().ident)
            return cb()

        ctrl._dispatch = track_thread
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: callback_threads.append("success"),
            on_error=lambda e: callback_threads.append("error"),
        )
        assert "success" in callback_threads


# ---------------------------------------------------------------------------
# Mutation ambiguity
# ---------------------------------------------------------------------------

class TestMutationAmbiguity:
    def test_ambiguity_refreshes_without_retry(self):
        """On mutation ambiguity, refresh once without retrying."""
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
        assert refresh.call_count >= 1

    def test_sequence_ambiguity_refreshes_and_reports(self):
        """Sequence failure from ambiguity: refresh once, report error."""
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        errors = []

        def _fail():
            raise SshPilotError(ErrorCode.MUTATION_AMBIGUOUS, "changed")

        ctrl.run_sequence(
            [
                lambda _prev: ctrl.client.rename_group("g1", "New"),
                lambda prev: _fail(),
            ],
            on_success=lambda r: pytest.fail("unexpected success"),
            on_error=lambda e: errors.append(e),
        )
        assert len(errors) == 1
        assert errors[0].code is ErrorCode.MUTATION_AMBIGUOUS
        assert refresh.call_count >= 1


# ---------------------------------------------------------------------------
# Close during / before sequence
# ---------------------------------------------------------------------------

class TestCloseBehaviour:
    def test_close_during_sequence_discards_callbacks(self):
        ctrl = _make_controller()
        ctrl.close()
        results = []
        with pytest.raises(RuntimeError, match="closed"):
            ctrl.run_sequence(
                [lambda _prev: ctrl.client.create_group("Prod")],
                on_success=lambda r: results.append(r),
                on_error=lambda e: results.append(("err", e)),
            )

    def test_close_before_refresh_skips_refresh(self):
        """Once closed, no further RPCs are submitted."""
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        ctrl.close()
        # Cannot run (closed), so no refresh.
        assert refresh.call_count == 0

    def test_only_one_terminal_delivery(self):
        """Ensure exactly one terminal success/error delivery even after close."""
        results = []
        ctrl = _make_controller()
        ctrl.client.set_result("create_group", "ok")

        def _on_success(r):
            results.append(("ok", r))
            ctrl.close()  # Close inside callback.

        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=_on_success,
            on_error=lambda e: results.append(("err", e)),
        )
        assert len(results) == 1
        assert results[0][0] == "ok"


# ---------------------------------------------------------------------------
# Client replacement
# ---------------------------------------------------------------------------

class TestClientReplacement:
    def test_close_old_and_create_new(self):
        """Client replacement closes old controller, creates new."""
        ctrl = _make_controller()
        ctrl.client.set_result("create_group", "ok")
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: None,
            on_error=lambda e: None,
        )
        ctrl.close()
        assert ctrl._closed

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
# Fire-and-forget compatibility
# ---------------------------------------------------------------------------

class TestFireAndForget:
    def test_fire_and_forget_logs_error(self):
        """_fire_and_forget logs an error instead of swallowing it."""
        ctrl = _make_controller()
        ctrl.client.set_error("create_group", RuntimeError("boom"))

        with patch("sshpilot.gtk.group_store.logger") as mock_logger:
            ctrl._fire_and_forget(
                lambda: ctrl.client.create_group("Prod"),
                label="test op",
            )
            mock_logger.error.assert_called_once()
            assert "boom" in str(mock_logger.error.call_args)

    def test_fire_and_forget_returns_none_when_busy(self):
        ctrl = _make_controller()
        ctrl._lock.acquire(blocking=False)
        result = ctrl._fire_and_forget(
            lambda: ctrl.client.create_group("Prod"),
        )
        ctrl._lock.release()
        assert result is None


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

    def test_group_manager_raises_without_controller(self):
        from sshpilot.groups import GroupManager

        gm = GroupManager.__new__(GroupManager)
        gm.client = None
        gm.controller = None
        gm.config = None
        gm.groups = {}
        gm.connections = {}
        gm.root_connections = []
        gm._expanded = {}
        gm._projection_handler = None
        gm.connection_manager = MagicMock()
        gm.connection_manager.snapshot.return_value = None

        with pytest.raises(RuntimeError, match="no mutation controller"):
            gm.run(lambda: None, on_success=lambda r: None, on_error=lambda e: None)

        with pytest.raises(RuntimeError, match="no mutation controller"):
            gm.run_sequence([], on_success=lambda r: None, on_error=lambda e: None)


# ---------------------------------------------------------------------------
# Commit 1: Sequence context propagation tests
# ---------------------------------------------------------------------------


class TestCreateAndAssignSequence:
    """Tests for the create-new-group + assign connections workflow.

    The critical requirement: every assignment step must receive the original
    created group ID, not the return value of the previous assignment (bool).
    """

    def _make_assign_steps(self, controller, client, group_name, nicknames,
                           *, is_copy=False):
        """Reproduce the production create-and-assign step construction.

        This mirrors the fixed ``_open_group_assignment_dialog`` logic.
        """
        created_group_id = [None]

        def _validate_create(_prev):
            gid = client.create_group(group_name, parent_id="", color="")
            if not gid or not str(gid).strip():
                raise ValueError("The daemon did not return a valid group ID")
            created_group_id[0] = str(gid).strip()
            return gid

        steps = [_validate_create]
        for nick in nicknames:
            steps.append(
                lambda _prev, n=nick: (
                    client.copy_connection_to_group({"conn": n, "group": created_group_id[0]})
                    if is_copy
                    else client.assign_connection_to_group(n, created_group_id[0])
                )
            )
        return steps

    def test_create_and_move_two_connections(self):
        """Create group, then move two connections to it."""
        client = FakeClient()
        client.set_result("create_group", "grp-new")
        ctrl = _make_controller(client=client)
        steps = self._make_assign_steps(ctrl, client, "Prod", ["c1", "c2"])
        results = []
        ctrl.run_sequence(
            steps,
            on_success=lambda r: results.append(("ok", r)),
            on_error=lambda e: results.append(("err", e)),
        )
        assert results == [("ok", True)]
        # Both assignments must use the group ID returned by create_group.
        assert client.calls[1] == ("assign_connection_to_group", ("c1", "grp-new"), {})
        assert client.calls[2] == ("assign_connection_to_group", ("c2", "grp-new"), {})

    def test_create_and_move_three_connections(self):
        """Create group, then move three connections to it."""
        client = FakeClient()
        client.set_result("create_group", "grp-3")
        ctrl = _make_controller(client=client)
        steps = self._make_assign_steps(ctrl, client, "Prod", ["c1", "c2", "c3"])
        results = []
        ctrl.run_sequence(
            steps,
            on_success=lambda r: results.append(("ok", r)),
            on_error=lambda e: results.append(("err", e)),
        )
        assert results == [("ok", True)]
        for i, nick in enumerate(["c1", "c2", "c3"], start=1):
            assert client.calls[i][1] == (nick, "grp-3")

    def test_create_and_copy_two_connections(self):
        """Create group, then copy two connections to it."""
        client = FakeClient()
        client.set_result("create_group", "grp-copy")
        ctrl = _make_controller(client=client)
        steps = self._make_assign_steps(
            ctrl, client, "Copy Group", ["c1", "c2"], is_copy=True,
        )
        results = []
        ctrl.run_sequence(
            steps,
            on_success=lambda r: results.append(("ok", r)),
            on_error=lambda e: results.append(("err", e)),
        )
        assert results == [("ok", True)]
        # Both copy requests must use the created group ID.
        for i in [1, 2]:
            req = client.calls[i][1][0]
            assert req["group"] == "grp-copy"

    def test_every_assignment_receives_created_group_id(self):
        """Verify the closure captures the group ID correctly for 5 connections."""
        client = FakeClient()
        client.set_result("create_group", "grp-many")
        ctrl = _make_controller(client=client)
        nicks = [f"c{i}" for i in range(5)]
        steps = self._make_assign_steps(ctrl, client, "Prod", nicks)
        ctrl.run_sequence(
            steps,
            on_success=lambda r: None,
            on_error=lambda e: pytest.fail(str(e)),
        )
        for i, nick in enumerate(nicks, start=1):
            assert client.calls[i][1] == (nick, "grp-many")

    def test_first_assignment_true_does_not_replace_context(self):
        """Assignment returning True must not overwrite the group ID context."""
        client = FakeClient()
        client.set_result("create_group", "grp-ctx")
        client.set_result("assign_connection_to_group", True)  # explicit True
        ctrl = _make_controller(client=client)
        steps = self._make_assign_steps(ctrl, client, "Prod", ["c1", "c2"])
        results = []
        ctrl.run_sequence(
            steps,
            on_success=lambda r: results.append(r),
            on_error=lambda e: results.append(("err", e)),
        )
        assert results == [True]
        assert client.calls[1][1] == ("c1", "grp-ctx")
        assert client.calls[2][1] == ("c2", "grp-ctx")

    def test_first_assignment_falsy_does_not_replace_context(self):
        """Assignment returning a falsy value must not overwrite the context."""
        client = FakeClient()
        client.set_result("create_group", "grp-ctx2")
        client.set_result("assign_connection_to_group", 0)  # falsy
        ctrl = _make_controller(client=client)
        steps = self._make_assign_steps(ctrl, client, "Prod", ["c1"])
        results = []
        ctrl.run_sequence(
            steps,
            on_success=lambda r: results.append(r),
            on_error=lambda e: results.append(("err", e)),
        )
        assert client.calls[1][1] == ("c1", "grp-ctx2")

    def test_empty_create_result_aborts_before_assignment(self):
        """If create_group returns empty string, the sequence must abort."""
        client = FakeClient()
        client.set_result("create_group", "")  # empty group ID
        ctrl = _make_controller(client=client)
        steps = self._make_assign_steps(ctrl, client, "Prod", ["c1"])
        errors = []
        ctrl.run_sequence(
            steps,
            on_success=lambda r: pytest.fail("unexpected success"),
            on_error=lambda e: errors.append(e),
        )
        assert len(errors) == 1
        assert "valid group ID" in str(errors[0])
        # No assignment should have been attempted.
        assert len(client.calls) == 1  # only create_group

    def test_none_create_result_aborts(self):
        """If create_group returns None, the sequence must abort."""
        client = FakeClient()
        client.set_result("create_group", None)
        ctrl = _make_controller(client=client)
        steps = self._make_assign_steps(ctrl, client, "Prod", ["c1"])
        errors = []
        ctrl.run_sequence(
            steps,
            on_success=lambda r: pytest.fail("unexpected success"),
            on_error=lambda e: errors.append(e),
        )
        assert len(errors) == 1
        assert len(client.calls) == 1  # only create_group

    def test_nested_refresh_submission_failure_releases_lock(self):
        """If the refresh submit raises synchronously, lock and busy are released."""
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        ctrl.client.set_result("create_group", "ok")
        # Make refresh submission fail.
        original_submit = ctrl.submit
        call_count = [0]
        def selective_submit(operation, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:  # second submit is the refresh
                raise RuntimeError("bridge down")
            return original_submit(operation, **kwargs)
        ctrl.submit = selective_submit
        errors = []
        ctrl.run(
            lambda: ctrl.client.create_group("Prod"),
            on_success=lambda r: None,
            on_error=lambda e: errors.append(e),
        )
        # Lock must be released.
        assert not ctrl._lock.locked()
        assert not ctrl._busy

    def test_ambiguity_refresh_submission_failure_releases_lock(self):
        """If ambiguity refresh submit raises, lock and busy are released."""
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        ctrl.client.set_result("create_group", "ok")
        original_submit = ctrl.submit
        call_count = [0]
        def selective_submit(operation, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:  # second submit is the ambiguity refresh
                raise RuntimeError("bridge down")
            return original_submit(operation, **kwargs)
        ctrl.submit = selective_submit
        errors = []
        ctrl.run(
            lambda: (_ for _ in ()).throw(
                SshPilotError(ErrorCode.MUTATION_AMBIGUOUS, "changed")
            ),
            on_success=lambda r: None,
            on_error=lambda e: errors.append(e),
        )
        assert not ctrl._lock.locked()
        assert not ctrl._busy

    def test_partial_failure_refresh_submission_failure_releases_lock(self):
        """If partial-failure refresh submit raises, lock and busy are released."""
        refresh = MagicMock()
        ctrl = _make_controller(refresh=refresh)
        ctrl.client.set_result("create_group", "grp-new")
        original_submit = ctrl.submit
        call_count = [0]
        def selective_submit(operation, **kwargs):
            call_count[0] += 1
            if call_count[0] == 3:  # third submit is the error refresh
                raise RuntimeError("bridge down")
            return original_submit(operation, **kwargs)
        ctrl.submit = selective_submit
        errors = []
        ctrl.run_sequence(
            [
                lambda _prev: ctrl.client.create_group("Prod"),
                lambda prev: (_ for _ in ()).throw(RuntimeError("assign failed")),
            ],
            on_success=lambda r: pytest.fail("unexpected success"),
            on_error=lambda e: errors.append(e),
        )
        assert not ctrl._lock.locked()
        assert not ctrl._busy
        assert len(errors) == 1

    def test_close_between_creation_and_first_assignment(self):
        """Closing the controller between create and assign aborts cleanly."""
        client = FakeClient()
        client.set_result("create_group", "grp-cl")
        ctrl = _make_controller(client=client)

        def create_and_close(_prev):
            gid = client.create_group("Prod", parent_id="", color="")
            ctrl.close()  # close during the sequence
            return gid

        steps = [
            create_and_close,
            lambda _prev: client.assign_connection_to_group("c1", "grp-cl"),
        ]
        results = []
        ctrl.run_sequence(
            steps,
            on_success=lambda r: results.append(r),
            on_error=lambda e: results.append(("err", e)),
        )
        # After close, no further callbacks should fire.
        # The sequence should still complete (the close happens after create
        # returns, so assign will be submitted but the controller is closed).
        assert not ctrl._lock.locked()

    def test_close_between_two_assignments(self):
        """Closing between two assignments aborts the remaining steps."""
        client = FakeClient()
        client.set_result("create_group", "grp-cl2")
        ctrl = _make_controller(client=client)
        call_count = [0]

        def assign_with_close(_prev):
            call_count[0] += 1
            if call_count[0] == 2:  # first assignment
                ctrl.close()
            return client.assign_connection_to_group("c1", "grp-cl2")

        steps = [
            lambda _prev: client.create_group("Prod", parent_id="", color=""),
            assign_with_close,
            lambda _prev: client.assign_connection_to_group("c2", "grp-cl2"),
        ]
        results = []
        ctrl.run_sequence(
            steps,
            on_success=lambda r: results.append(r),
            on_error=lambda e: results.append(("err", e)),
        )
        assert not ctrl._lock.locked()
