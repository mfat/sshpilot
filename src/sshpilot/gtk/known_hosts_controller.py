"""GTK-free known-hosts controller.

Stages removal decisions and performs one batched, revision-checked mutation
through a daemon-backed ``SshPilotClient``. No filesystem or GTK imports.

Threading: one ``RLock`` guards the snapshot/pending state and the
``_loading``/``_saving`` busy flags so overlapping operations are rejected
deterministically. Client RPCs run *outside* the lock so a blocked request
never stalls other controller callers. A save with nothing staged is a
successful no-op that never touches the client.
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
        self._loading = False
        self._saving = False

    def _ensure_idle(self) -> None:
        if self._loading or self._saving:
            raise RuntimeError("a known-hosts operation is already in progress")

    def load(self) -> KnownHostsSnapshot:
        with self._lock:
            self._ensure_idle()
            self._loading = True

        try:
            snapshot = self._client.list_known_hosts()
        except BaseException:
            with self._lock:
                self._loading = False
            raise

        with self._lock:
            self._snapshot = snapshot
            self._pending = []
            self._loading = False

        return snapshot

    def stage_remove(self, entry_id: KnownHostEntryId) -> None:
        with self._lock:
            self._ensure_idle()
            if entry_id in self._pending:
                return
            self._pending.append(entry_id)

    def pending_entry_ids(self) -> tuple[KnownHostEntryId, ...]:
        with self._lock:
            return tuple(self._pending)

    def save(self) -> KnownHostsMutationResult:
        with self._lock:
            self._ensure_idle()
            snapshot = self._snapshot
            if snapshot is None:
                raise RuntimeError(
                    "known-hosts snapshot must be loaded before saving"
                )
            saved_ids = tuple(self._pending)
            self._saving = True

        try:
            if saved_ids:
                request = RemoveKnownHostEntriesRequest(
                    revision=snapshot.revision,
                    entry_ids=saved_ids,
                )
                result = self._client.remove_known_host_entries(request)
            else:
                result = KnownHostsMutationResult(
                    revision=snapshot.revision,
                    removed_count=0,
                    entries=snapshot.entries,
                )

            next_snapshot = KnownHostsSnapshot(
                revision=result.revision,
                entries=result.entries,
            )
        except BaseException:
            with self._lock:
                self._saving = False
            raise

        with self._lock:
            self._snapshot = next_snapshot
            # Clear only the IDs this save sent; never discard a staged ID
            # that was not part of the request.
            self._pending = [
                entry_id
                for entry_id in self._pending
                if entry_id not in saved_ids
            ]
            self._saving = False

        return result

    def clear_pending(self) -> None:
        with self._lock:
            if self._saving:
                raise RuntimeError("a known-hosts save is in progress")
            self._pending = []
