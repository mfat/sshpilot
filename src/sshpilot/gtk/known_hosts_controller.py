"""GTK-free known-hosts controller.

Stages removal decisions and performs one batched, revision-checked mutation
through a daemon-backed ``SshPilotClient``. No filesystem or GTK imports.

Threading: a single ``RLock`` guards the snapshot/pending state so a worker
thread (``load``/``save``) can never interleave with the GTK main thread
(``stage_remove``/``pending_entry_ids``/``clear_pending``). Calls into the
client happen while holding the lock.
"""
from __future__ import annotations

import threading

from sshpilot.api.client import SshPilotClient
from sshpilot.api.models.known_hosts import (
    KnownHostEntryId,
    KnownHostsMutationResult,
    KnownHostsSnapshot,
    RemoveKnownHostEntriesRequest,
)


class KnownHostsController:
    """Stateful frontend controller for the known-hosts editor."""

    def __init__(self, client: SshPilotClient) -> None:
        self._client = client
        self._lock = threading.RLock()
        self._snapshot: KnownHostsSnapshot | None = None
        self._pending: list[KnownHostEntryId] = []

    def load(self) -> KnownHostsSnapshot:
        snapshot = self._client.list_known_hosts()
        with self._lock:
            self._snapshot = snapshot
            self._pending = []
        return snapshot

    def stage_remove(self, entry_id: KnownHostEntryId) -> None:
        with self._lock:
            if entry_id in self._pending:
                return
            self._pending.append(entry_id)

    def pending_entry_ids(self) -> tuple[KnownHostEntryId, ...]:
        with self._lock:
            return tuple(self._pending)

    def save(self) -> KnownHostsMutationResult:
        with self._lock:
            snapshot = self._snapshot
            if snapshot is None:
                raise RuntimeError(
                    "known-hosts snapshot must be loaded before saving"
                )
            request = RemoveKnownHostEntriesRequest(
                revision=snapshot.revision,
                entry_ids=tuple(self._pending),
            )
        result = self._client.remove_known_host_entries(request)
        with self._lock:
            self._snapshot = KnownHostsSnapshot(
                revision=result.revision,
                entries=result.entries,
            )
            self._pending = []
        return result

    def clear_pending(self) -> None:
        with self._lock:
            self._pending = []
