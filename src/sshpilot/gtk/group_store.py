"""GTK group projection and daemon mutation controller."""

from __future__ import annotations

import logging
from threading import Lock
from sshpilot.api.errors import ErrorCode, SshPilotError

logger = logging.getLogger(__name__)


def validate_group_id(value) -> str:
    """Return a verified daemon group ID.

    The daemon must identify groups with a nonempty *string*.  Integers,
    booleans, ``None``, arbitrary objects, and values merely convertible to a
    string are rejected so callers cannot thread a malformed identifier into a
    subsequent assignment or copy RPC.  ``run_sequence`` only propagates the
    previous step's return value (a bool for assignment RPCs), so a bad ID
    captured here would otherwise poison every later step.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("The daemon did not return a valid group ID")
    return value.strip()


def build_create_and_assign_steps(client, group_name, nicknames, *,
                                  is_copy=False, color=""):
    """Build ``run_sequence`` steps that create a group then assign/copy.

    The created group ID is captured once, validated via
    :func:`validate_group_id`, and reused for every assignment step.  Because
    ``run_sequence`` only threads the previous step's return value (a bool for
    assignment RPCs), the captured ID cannot be replaced by per-step results.
    """
    created_group_id = [None]

    def _validate_create(_prev):
        gid = client.create_group(group_name, parent_id="", color=color or "")
        normalized = validate_group_id(gid)
        created_group_id[0] = normalized
        return normalized

    steps = [_validate_create]
    for nick in nicknames:
        steps.append(
            lambda _prev, n=nick: _apply_assignment(
                client, n, created_group_id[0], is_copy=is_copy,
            )
        )
    return steps


def _apply_assignment(client, nick, group_id, *, is_copy):
    if is_copy:
        from sshpilot.api.models.connection_store import (
            ConnectionId, CopyConnectionToGroupRequest, GroupId,
        )
        return client.copy_connection_to_group(
            CopyConnectionToGroupRequest(
                connection_id=ConnectionId(nick),
                group_id=GroupId(group_id),
            )
        )
    return client.assign_connection_to_group(nick, group_id)


class GroupPresentationStore:
    """Read groups and membership exclusively from the connection snapshot."""

    def __init__(self, connection_store):
        self.connection_store = connection_store

    @property
    def groups(self):
        return self.connection_store.groups

    def get_group(self, group_id):
        return self.connection_store.get_group(group_id)

    def get_connection_groups(self, connection_id):
        return self.connection_store.get_connection_groups(connection_id)


class GroupMutationController:
    """Serialize frontend group commands without mutating local authority.

    For every successful mutation that requires an authoritative refresh:

    * a refresh submission failure is an *error*, never success;
    * a refresh execution failure is an *error*, never success;
    * exactly one terminal callback (``on_success`` *or* ``on_error``) is
      delivered, through :meth:`_dispatch`;
    * busy state is cleared and the lock is released exactly once;
    * no second refresh is attempted after refresh submission fails;
    * the mutation is never retried;
    * after :meth:`close`, terminal callbacks are suppressed (busy/lock are
      still released).
    """

    def __init__(self, client, *, bridge=None, submit=None, refresh=None,
                 dispatch=None, on_busy=None, on_error=None):
        self.client = client
        self.bridge = bridge
        self.submit = submit or (bridge.submit if bridge is not None else None)
        self.refresh = refresh
        self._lock = Lock()
        self._closed = False
        self._busy = False
        self._released = True  # nothing in flight yet
        self._on_busy = on_busy
        self._dispatch = dispatch or (lambda callback: callback())
        self._on_error = on_error

    @property
    def busy(self):
        return self._busy

    def close(self):
        """Mark the controller closed.

        No further terminal UI callbacks are delivered.  Pending busy state
        and lock ownership are still released by the in-flight operation.
        """
        self._closed = True

    # ------------------------------------------------------------------
    # Internal helpers — each one terminal, releases exactly once.
    # ------------------------------------------------------------------
    def _set_busy(self, value):
        self._busy = value
        if self._on_busy is not None:
            self._dispatch(lambda: self._on_busy(value))

    def _release(self):
        """Clear busy state and release the lock exactly once per operation."""
        if self._released:
            return
        self._released = True
        if self._busy:
            self._set_busy(False)
        if self._lock.locked():
            self._lock.release()

    def _finish_with_success(self, result, callback):
        """Final completion: deliver success on the dispatch thread and release."""
        def _finish():
            try:
                if not self._closed:
                    callback(result)
            finally:
                self._release()
        self._dispatch(_finish)

    def _finish_with_error(self, error, callback):
        """Final completion: deliver error on the dispatch thread and release."""
        def _finish():
            try:
                if not self._closed:
                    callback(error)
            finally:
                self._release()
        self._dispatch(_finish)

    def _handle_refresh_failure(self, error, callback):
        """Refresh already failed — terminal.

        Must never initiate another refresh and must never report success.
        """
        self._finish_with_error(error, callback)

    def _handle_mutation_failure(self, error, callback,
                                 *, refresh_on_any_failure=False):
        """Mutation failure — optionally reconcile with one refresh, then report.

        Never retries the mutation.  For ``run_sequence`` partial failures the
        repository is refreshed once regardless of error code.  For ``run`` the
        refresh is reserved for ``MUTATION_AMBIGUOUS`` (the outcome is unknown)
        and ``STALE_EDITOR`` (a stale ``STALE_CONNECTION_STATE`` rejection
        means the mutation definitely did not happen and the frontend
        projection is known stale).  A refresh submission failure is itself
        terminal: no recursive refresh is attempted and the original mutation
        error is reported.
        """
        if self._closed:
            self._release()
            return
        should_refresh = self.refresh is not None and (
            refresh_on_any_failure
            or getattr(error, "code", None)
            in (ErrorCode.MUTATION_AMBIGUOUS, ErrorCode.STALE_EDITOR)
        )
        if should_refresh:
            def _after_refresh(_result):
                # Refresh completed (success or failure) — terminal.  Report
                # the original mutation error; never start another refresh.
                self._finish_with_error(error, callback)
            try:
                self.submit(
                    self.refresh,
                    on_success=_after_refresh,
                    on_error=_after_refresh,
                )
                return
            except Exception:
                # Refresh submission itself failed — terminal, no retry.
                self._finish_with_error(error, callback)
                return
        self._finish_with_error(error, callback)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, operation, *, on_success, on_error, refresh_after=True):
        """Execute a single async group operation.

        RPC work runs off the GTK thread.  The authoritative refresh also
        runs off the GTK thread.  Final *on_success* / *on_error* callbacks
        run on the GTK thread via :meth:`_dispatch`.
        """
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a group operation is already in progress")
        if self._closed:
            self._lock.release()
            raise RuntimeError("group mutation controller is closed")
        self._released = False
        self._set_busy(True)

        def finish_success(result):
            if self._closed:
                self._release()
                return
            if refresh_after and self.refresh is not None:
                try:
                    self.submit(
                        self.refresh,
                        on_success=lambda _snapshot: self._finish_with_success(
                            result, on_success,
                        ),
                        on_error=lambda exc: self._handle_refresh_failure(
                            exc, on_error,
                        ),
                    )
                except Exception:
                    # Sync refresh submission failure — terminal error.
                    self._handle_refresh_failure(
                        SshPilotError(
                            ErrorCode.DAEMON_UNAVAILABLE,
                            "Failed to submit refresh",
                        ),
                        on_error,
                    )
            else:
                self._finish_with_success(result, on_success)

        def failure(error):
            # Mutation failure — reconcile with at most one refresh on
            # ambiguity, never retry.  The terminal callback is dispatched by
            # the helper.
            self._handle_mutation_failure(error, on_error)

        if self.submit is None:
            self._release()
            raise RuntimeError("a GTK client bridge is required")
        try:
            return self.submit(
                operation, on_success=finish_success, on_error=failure,
                on_discard=lambda _result: failure(
                    SshPilotError(ErrorCode.OPERATION_CANCELLED, "Group operation cancelled")
                ),
            )
        except Exception:
            self._release()
            raise

    def run_sequence(self, steps, *, on_success, on_error):
        """Execute a chain of async operations; each step may consume the
        previous step's result.

        Requirements:
        - Exactly one complete sequence may be active.
        - Internal steps do not conflict with the busy guard.
        - No lock is held during RPCs or refreshes.
        - Each RPC result is preserved (especially new group IDs).
        - On any partial failure, refresh once (without retrying) and report
          the error.  A refresh submission or execution failure is terminal.
        """
        steps = tuple(steps)
        if not steps:
            raise ValueError("group operation sequence must not be empty")

        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a group operation is already in progress")
        if self._closed:
            self._lock.release()
            raise RuntimeError("group mutation controller is closed")
        self._released = False
        self._set_busy(True)

        def advance(index, previous):
            operation = steps[index]
            self._submit_internal(
                lambda: operation(previous),
                lambda result: advance(index + 1, result)
                if index + 1 < len(steps)
                else self._complete_sequence(result, on_success),
                on_error,
                refresh_after=index + 1 == len(steps),
            )

        self._submit_internal(
            lambda: steps[0](None),
            lambda result: advance(1, result) if len(steps) > 1
            else self._complete_sequence(result, on_success),
            on_error,
            refresh_after=len(steps) == 1,
            owns_lock=True,
        )

    def _submit_internal(self, operation, success, error, *,
                         refresh_after=False, owns_lock=False):
        def ok(result):
            if self._closed:
                self._release()
                return
            if refresh_after and self.refresh is not None:
                try:
                    self.submit(
                        self.refresh,
                        on_success=lambda _snapshot: success(result),
                        on_error=lambda exc: self._handle_refresh_failure(
                            exc, error,
                        ),
                    )
                except Exception:
                    # Sync refresh submission failure — terminal error.
                    self._handle_refresh_failure(
                        SshPilotError(
                            ErrorCode.DAEMON_UNAVAILABLE,
                            "Failed to submit refresh",
                        ),
                        error,
                    )
            else:
                success(result)

        def fail(exc):
            if self._closed:
                self._release()
                return
            self._handle_mutation_failure(
                exc, error, refresh_on_any_failure=True,
            )

        try:
            self.submit(operation, on_success=ok, on_error=fail,
                        on_discard=lambda _result: fail(
                            SshPilotError(ErrorCode.OPERATION_CANCELLED, "Group operation cancelled")
                        ))
        except Exception:
            self._release()
            raise

    def _complete_sequence(self, result, callback):
        self._finish_with_success(result, callback)

    def _fire_and_forget(self, operation, *, label="group operation"):
        """Fire-and-forget compatibility wrapper.

        Logs an error instead of silently swallowing failures.  Prefer
        ``run()`` / ``run_sequence()`` for new callers.
        """
        def _on_error(error):
            logger.error("%s failed: %s", label, error)
        try:
            return self.run(
                operation,
                on_success=lambda _result: None,
                on_error=_on_error,
            )
        except RuntimeError:
            logger.warning("Cannot submit %s: controller busy or closed", label)
            return None