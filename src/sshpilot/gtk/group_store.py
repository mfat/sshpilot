"""GTK group projection and daemon mutation controller."""

from __future__ import annotations

from threading import Lock


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

    def __init__(self, client, *, refresh=None):
        self.client = client
        self.refresh = refresh
        self._lock = Lock()

    def _run(self, operation):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("a group operation is already in progress")
        try:
            result = operation()
            if self.refresh is not None:
                self.refresh()
            return result
        finally:
            self._lock.release()

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
