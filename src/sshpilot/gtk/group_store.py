"""GTK group projection and daemon mutation controller."""

from __future__ import annotations

import logging
from threading import Lock
from sshpilot.api.errors import ErrorCode, SshPilotError

logger = logging.getLogger(__name__)


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
    """Serialize frontend group commands without mutating local authority."""

    def __init__(self, client, *, bridge=None, submit=None, refresh=None,
                 dispatch=None, on_busy=None, on_error=None):
        self.client = client
        self.bridge = bridge
        self.submit = submit or (bridge.submit if bridge is not None else None)
        self.refresh = refresh
        self._lock = Lock()
        self._closed = False
        self._busy = False
        self._on_busy = on_busy
        self._dispatch = dispatch or (lambda callback: callback())
        self._on_error = on_error

    @property
    def busy(self):
        return self._busy

    def close(self):
        """Mark the controller closed.  No further RPCs are submitted and
        pending callbacks are discarded."""
        self._closed = True

    def _set_busy(self, value):
        self._busy = value
        if self._on_busy is not None:
            self._dispatch(lambda: self._on_busy(value))

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
        self._set_busy(True)

        def finish_success(result):
            def finish():
                try:
                    if not self._closed:
                        on_success(result)
                finally:
                    self._set_busy(False)
                    self._lock.release()
            if refresh_after and not self._closed and self.refresh is not None:
                try:
                    self.submit(
                        self.refresh,
                        on_success=lambda _snapshot: self._dispatch(finish),
                        on_error=failure,
                    )
                except Exception:
                    self._dispatch(finish)
            else:
                self._dispatch(finish)

        def failure(error):
            # On mutation ambiguity refresh once (without retrying the mutation).
            if (
                not self._closed
                and getattr(error, "code", None) is ErrorCode.MUTATION_AMBIGUOUS
                and self.refresh is not None
            ):
                def after_refresh(_result):
                    def finish():
                        try:
                            if not self._closed:
                                on_error(error)
                        finally:
                            self._set_busy(False)
                            self._lock.release()
                    self._dispatch(finish)
                try:
                    self.submit(
                        self.refresh,
                        on_success=after_refresh,
                        on_error=after_refresh,
                    )
                except Exception:
                    def finish():
                        try:
                            if not self._closed:
                                on_error(error)
                        finally:
                            self._set_busy(False)
                            self._lock.release()
                    self._dispatch(finish)
                return
            def finish():
                try:
                    if not self._closed:
                        on_error(error)
                finally:
                    self._set_busy(False)
                    self._lock.release()
            self._dispatch(finish)

        if self.submit is None:
            self._set_busy(False)
            self._lock.release()
            raise RuntimeError("a GTK client bridge is required")
        try:
            return self.submit(
                operation, on_success=finish_success, on_error=failure,
                on_discard=lambda _result: failure(
                    SshPilotError(ErrorCode.OPERATION_CANCELLED, "Group operation cancelled")
                ),
            )
        except Exception as exc:
            try:
                self._set_busy(False)
            finally:
                self._lock.release()
            raise

    def run_sequence(self, steps, *, on_success, on_error):
        """Execute a chain of async operations; each step may consume the
        previous step's result.

        Requirements:
        - Exactly one complete sequence may be active.
        - Internal steps do not conflict with the busy guard.
        - No lock is held during RPCs or refreshes.
        - Each RPC result is preserved (especially new group IDs).
        - On mutation ambiguity, refresh once without retrying.
        - On partial sequence failure, refresh once and report the error.
        """
        steps = tuple(steps)
        if not steps:
            raise ValueError("group operation sequence must not be empty")

        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a group operation is already in progress")
        if self._closed:
            self._lock.release()
            raise RuntimeError("group mutation controller is closed")
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

    def _submit_internal(self, operation, success, error, *, refresh_after=False, owns_lock=False):
        def ok(result):
            if self._closed:
                self._finish_busy()
                return
            if refresh_after and self.refresh is not None:
                try:
                    self.submit(self.refresh, on_success=lambda _snapshot: success(result), on_error=error)
                except Exception:
                    self._finish_error(SshPilotError(
                        ErrorCode.CONNECTION_STATE_IO_ERROR,
                        "Failed to submit refresh",
                    ), error)
            else:
                success(result)

        def fail(exc):
            if self._closed:
                self._finish_busy()
                return
            self._dispatch(lambda: self._finish_error(exc, error))

        try:
            self.submit(operation, on_success=ok, on_error=fail,
                        on_discard=lambda _result: fail(
                            SshPilotError(ErrorCode.OPERATION_CANCELLED, "Group operation cancelled")
                        ))
        except Exception as exc:
            if not owns_lock:
                self._finish_busy()
            else:
                self._set_busy(False)
                self._lock.release()
            raise

    def _complete_sequence(self, result, callback):
        if not self._closed:
            self._dispatch(lambda: callback(result))
        self._finish_busy()

    def _finish_error(self, error, callback):
        # On any sequence failure, refresh once (without retrying the mutation)
        # then report the error.
        if self._closed:
            self._finish_busy()
            return
        if self.refresh is not None:
            def _after_refresh(_result):
                if self._closed:
                    self._finish_busy()
                    return
                callback(error)
                self._finish_busy()
            try:
                self.submit(
                    self.refresh,
                    on_success=_after_refresh,
                    on_error=_after_refresh,
                )
                return
            except Exception:
                # Refresh submission failed — still report the error.
                pass
        callback(error)
        self._finish_busy()

    def _finish_error_callback(self, error, callback):
        if not self._closed:
            callback(error)
        self._finish_busy()

    def _finish_busy(self):
        if self._lock.locked():
            self._set_busy(False)
            self._lock.release()

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
