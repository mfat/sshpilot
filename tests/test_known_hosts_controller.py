"""KnownHostsController tests (GTK-free)."""
from __future__ import annotations

import pytest

from sshpilot.api.models.known_hosts import (
    KnownHostEntryId,
    KnownHostEntrySummary,
    KnownHostsMutationResult,
    KnownHostsSnapshot,
    RemoveKnownHostEntriesRequest,
)
from sshpilot.gtk.known_hosts_controller import KnownHostsController


class _FakeClient:
    def __init__(self):
        self.listed = 0
        self.removed = 0
        self.last_request = None
        self.snapshot = None
        self.remove_error = None

    def list_known_hosts(self):
        self.listed += 1
        return self.snapshot

    def remove_known_host_entries(self, request):
        self.removed += 1
        self.last_request = request
        if self.remove_error is not None:
            raise self.remove_error
        return KnownHostsMutationResult(
            revision="revision-2",
            removed_count=len(request.entry_ids),
            entries=tuple(
                entry
                for entry in self.snapshot.entries
                if entry.entry_id not in request.entry_ids
            ),
        )


def _entry(entry_id: str, hostname: str = "example.test") -> KnownHostEntrySummary:
    return KnownHostEntrySummary(
        entry_id=KnownHostEntryId(entry_id),
        hostname=hostname,
        key_type="ssh-ed25519",
        display_line=f"{hostname} ssh-ed25519 AAAA",
    )


def _snapshot(*entries):
    return KnownHostsSnapshot(
        revision="revision-1",
        entries=tuple(entries),
    )


def test_load_stores_snapshot_and_clears_pending():
    client = _FakeClient()
    client.snapshot = _snapshot(_entry("a"), _entry("b"))
    controller = KnownHostsController(client)
    controller.stage_remove(KnownHostEntryId("a"))
    controller.stage_remove(KnownHostEntryId("b"))

    snapshot = controller.load()

    assert snapshot is client.snapshot
    assert controller.pending_entry_ids() == ()
    assert client.listed == 1


def test_stage_preserves_order_and_rejects_duplicates():
    client = _FakeClient()
    controller = KnownHostsController(client)
    controller.stage_remove(KnownHostEntryId("b"))
    controller.stage_remove(KnownHostEntryId("a"))
    controller.stage_remove(KnownHostEntryId("b"))

    assert controller.pending_entry_ids() == (
        KnownHostEntryId("b"),
        KnownHostEntryId("a"),
    )


def test_save_requires_loaded_snapshot():
    client = _FakeClient()
    controller = KnownHostsController(client)
    with pytest.raises(RuntimeError):
        controller.save()
    assert client.removed == 0


def test_save_uses_loaded_revision_and_batches():
    client = _FakeClient()
    client.snapshot = _snapshot(_entry("a"), _entry("b"), _entry("c"))
    controller = KnownHostsController(client)
    controller.load()
    controller.stage_remove(KnownHostEntryId("a"))
    controller.stage_remove(KnownHostEntryId("c"))

    result = controller.save()

    assert client.removed == 1
    assert isinstance(client.last_request, RemoveKnownHostEntriesRequest)
    assert client.last_request.revision == "revision-1"
    assert client.last_request.entry_ids == (
        KnownHostEntryId("a"),
        KnownHostEntryId("c"),
    )
    assert result.removed_count == 2
    assert [e.entry_id for e in result.entries] == [KnownHostEntryId("b")]


def test_successful_save_replaces_snapshot_and_clears_pending():
    client = _FakeClient()
    client.snapshot = _snapshot(_entry("a"), _entry("b"))
    controller = KnownHostsController(client)
    controller.load()
    controller.stage_remove(KnownHostEntryId("a"))
    controller.save()

    assert controller.pending_entry_ids() == ()
    assert controller._snapshot.revision == "revision-2"
    assert [e.entry_id for e in controller._snapshot.entries] == [
        KnownHostEntryId("b")
    ]
    # A follow-up save uses the refreshed revision.
    controller.stage_remove(KnownHostEntryId("b"))
    controller.save()
    assert client.last_request.revision == "revision-2"


def test_error_does_not_clear_pending():
    client = _FakeClient()
    client.snapshot = _snapshot(_entry("a"), _entry("b"))
    controller = KnownHostsController(client)
    controller.load()
    controller.stage_remove(KnownHostEntryId("a"))
    client.remove_error = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        controller.save()

    assert controller.pending_entry_ids() == (KnownHostEntryId("a"),)


def test_clear_pending_removes_staged_ids():
    client = _FakeClient()
    controller = KnownHostsController(client)
    controller.stage_remove(KnownHostEntryId("a"))
    controller.clear_pending()
    assert controller.pending_entry_ids() == ()
