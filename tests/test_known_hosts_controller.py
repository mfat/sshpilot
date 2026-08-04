"""KnownHostsController tests (GTK-free)."""
from __future__ import annotations

import threading

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


class _BlockingClient(_FakeClient):
    """Blocks inside the mutation call until released, to hold a Save open."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def remove_known_host_entries(self, request):
        self.removed += 1
        self.last_request = request
        self.started.set()
        self.release.wait(timeout=5)
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


class _BlockingResult:
    """A mutation result whose ``revision`` blocks, holding the save open.

    ``save()`` reads ``result.revision`` while building the next snapshot
    inside its protected section; blocking here proves ``_saving`` stays held
    until the returned state is fully committed.
    """

    def __init__(self, entries):
        self.entries = entries
        self.entered = threading.Event()
        self.release = threading.Event()
        self.removed_count = 1

    @property
    def revision(self):
        self.entered.set()
        self.release.wait(timeout=5)
        return "revision-2"


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


def test_save_with_no_pending_ids_makes_zero_mutation_calls():
    client = _FakeClient()
    client.snapshot = _snapshot(_entry("a"), _entry("b"))
    controller = KnownHostsController(client)
    controller.load()

    result = controller.save()

    assert client.removed == 0
    assert client.listed == 1  # load only; no mutation RPC
    assert result.removed_count == 0
    assert result.revision == "revision-1"
    assert [e.entry_id for e in result.entries] == [
        KnownHostEntryId("a"),
        KnownHostEntryId("b"),
    ]


def test_noop_save_returns_zero_removed_count():
    client = _FakeClient()
    client.snapshot = _snapshot(_entry("a"))
    controller = KnownHostsController(client)
    controller.load()

    result = controller.save()

    assert isinstance(result, KnownHostsMutationResult)
    assert result.removed_count == 0
    assert result.revision == "revision-1"


def test_second_save_while_first_is_blocked_is_rejected():
    client = _BlockingClient()
    client.snapshot = _snapshot(_entry("a"))
    controller = KnownHostsController(client)
    controller.load()
    controller.stage_remove(KnownHostEntryId("a"))

    outcome = {}

    def _save():
        try:
            outcome["result"] = controller.save()
        except Exception as exc:  # pragma: no cover - guard for test cleanup
            outcome["error"] = exc

    thread = threading.Thread(target=_save)
    thread.start()
    assert client.started.wait(timeout=2)

    with pytest.raises(RuntimeError):
        controller.save()

    client.release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert client.removed == 1
    assert "error" not in outcome


def test_stage_while_save_is_blocked_is_rejected():
    client = _BlockingClient()
    client.snapshot = _snapshot(_entry("a"), _entry("b"))
    controller = KnownHostsController(client)
    controller.load()
    controller.stage_remove(KnownHostEntryId("a"))

    thread = threading.Thread(target=controller.save)
    thread.start()
    assert client.started.wait(timeout=2)

    with pytest.raises(RuntimeError):
        controller.stage_remove(KnownHostEntryId("b"))

    client.release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    # "b" was rejected during the blocked save, so it was never staged; the
    # successful save cleared the pre-block ID "a".
    assert KnownHostEntryId("b") not in controller.pending_entry_ids()


def test_load_while_save_is_blocked_is_rejected():
    client = _BlockingClient()
    client.snapshot = _snapshot(_entry("a"))
    controller = KnownHostsController(client)
    controller.load()
    controller.stage_remove(KnownHostEntryId("a"))

    thread = threading.Thread(target=controller.save)
    thread.start()
    assert client.started.wait(timeout=2)

    with pytest.raises(RuntimeError):
        controller.load()

    client.release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_failed_save_resets_busy_flag():
    client = _FakeClient()
    client.snapshot = _snapshot(_entry("a"))
    controller = KnownHostsController(client)
    controller.load()
    controller.stage_remove(KnownHostEntryId("a"))
    client.remove_error = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        controller.save()

    assert controller._saving is False
    assert controller._loading is False
    # The controller accepts a subsequent save after the failure.
    client.remove_error = None
    controller.save()
    assert client.removed == 2


def test_failed_save_retains_pending_ids():
    client = _FakeClient()
    client.snapshot = _snapshot(_entry("a"), _entry("b"))
    controller = KnownHostsController(client)
    controller.load()
    controller.stage_remove(KnownHostEntryId("a"))
    controller.stage_remove(KnownHostEntryId("b"))
    client.remove_error = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        controller.save()

    assert controller.pending_entry_ids() == (
        KnownHostEntryId("a"),
        KnownHostEntryId("b"),
    )


def test_save_holds_busy_flag_until_result_commit():
    """A save blocked reading its result must reject overlapping operations.

    ``_saving`` stays held until the returned revision/entries have been
    committed to the stored snapshot; only then does it clear.
    """
    client = _FakeClient()
    client.snapshot = _snapshot(_entry("a"))
    controller = KnownHostsController(client)
    controller.load()
    controller.stage_remove(KnownHostEntryId("a"))

    blocking = _BlockingResult(entries=())
    client.remove_known_host_entries = lambda request: blocking

    outcome = {}

    def _save():
        try:
            outcome["result"] = controller.save()
        except Exception as exc:  # pragma: no cover - cleanup guard
            outcome["error"] = exc

    thread = threading.Thread(target=_save)
    thread.start()
    assert blocking.entered.wait(timeout=2)

    # Save is blocked inside the result commit; every overlapping operation
    # must be rejected while the busy flag is still held.
    with pytest.raises(RuntimeError):
        controller.stage_remove(KnownHostEntryId("b"))
    with pytest.raises(RuntimeError):
        controller.save()
    with pytest.raises(RuntimeError):
        controller.load()

    blocking.release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert "error" not in outcome
    # The committed snapshot carries the released result's revision.
    assert controller._snapshot.revision == "revision-2"
    assert controller._saving is False
    assert controller._loading is False
    assert controller.pending_entry_ids() == ()

