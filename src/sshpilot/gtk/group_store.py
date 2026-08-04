"""GTK group projection and daemon mutation controller."""

from __future__ import annotations

from threading import Lock
from sshpilot.api.errors import ErrorCode, SshPilotError


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
        self._closed = True

    def _set_busy(self, value):
        self._busy = value
        if self._on_busy is not None:
            self._dispatch(lambda: self._on_busy(value))

    def _run(self, operation):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a group operation is already in progress")
        if self._closed:
            self._lock.release()
            raise RuntimeError("group mutation controller is closed")
        self._set_busy(True)

        def success(result):
            def finish():
                try:
                    if not self._closed and self.refresh is not None:
                        self.refresh()
                finally:
                    self._set_busy(False)
                    self._lock.release()
            self._dispatch(finish)

        def failure(error):
            def finish():
                try:
                    if not self._closed and self.refresh is not None and getattr(error, "code", None) is ErrorCode.MUTATION_AMBIGUOUS:
                        self.refresh()
                    if not self._closed and self._on_error is not None:
                        self._on_error(error)
                finally:
                    self._set_busy(False)
                    self._lock.release()
            self._dispatch(finish)

        if self.submit is None:
            self._set_busy(False)
            self._lock.release()
            raise RuntimeError("a GTK client bridge is required")
        return self.submit(operation, on_success=success, on_error=failure,
                           on_discard=lambda _result: failure(
                               SshPilotError(ErrorCode.OPERATION_CANCELLED, "Group operation cancelled")
                           ))

    def create_group(self, name, parent_id=None, color=""):
        return self._run(lambda: self.client.create_group(name, parent_id or "", color))

    def delete_group(self, group_id):
        return self._run(lambda: self.client.delete_group(group_id))

    def rename_group(self, group_id, name):
        return self._run(lambda: self.client.rename_group(group_id, name))

    def set_group_color(self, request):
        return self._run(lambda: self.client.set_group_color(request))

    def move_connection(self, connection_id, group_id=None):
        return self._run(
            lambda: self.client.assign_connection_to_group(connection_id, group_id or "")
        )

    def copy_connection_to_group(self, request):
        return self._run(lambda: self.client.copy_connection_to_group(request))

    def remove_connection_from_group(self, request):
        return self._run(lambda: self.client.remove_connection_from_group(request))

    def place_group(self, request):
        return self._run(lambda: self.client.place_group(request))

    def reorder_connection(self, request):
        return self._run(lambda: self.client.reorder_connection(request))
