"""Daemon known-hosts service tests."""

import threading

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.known_hosts import (
    KnownHostEntryId,
    RemoveKnownHostEntriesRequest,
)
from sshpilot.core.known_hosts.document import parse_known_hosts_document
from sshpilot.daemon.known_hosts_service import KnownHostsService

CONTENT = (
    b"# local network\n"
    b"\n"
    b"one.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIone\n"
    b"two.test ssh-rsa AAAAB3NzaC1yc2EAAAAItwo\n"
    b"one.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIone\n"
)


def _service(tmp_path):
    path = tmp_path / "known_hosts"
    service = KnownHostsService(lambda: path)
    return service, path


def _revision(content):
    return parse_known_hosts_document(content).revision


def _remove_request(revision, *entry_ids):
    return RemoveKnownHostEntriesRequest(
        revision=revision,
        entry_ids=tuple(KnownHostEntryId(entry_id) for entry_id in entry_ids),
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def test_list_returns_revision_and_displayed_summaries(tmp_path):
    service, path = _service(tmp_path)
    path.write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    assert snapshot.revision == _revision(CONTENT)
    hostnames = [entry.hostname for entry in snapshot.entries]
    assert hostnames == ["one.test", "two.test", "one.test"]
    # Comments and blank lines are not exposed as entries.
    assert all(entry.hostname != "" for entry in snapshot.entries)


def test_list_missing_file_is_empty_snapshot(tmp_path):
    service, _path = _service(tmp_path)
    snapshot = service.list_known_hosts()
    assert snapshot.revision == _revision(b"")
    assert snapshot.entries == ()


def test_duplicate_identical_lines_keep_distinct_ids(tmp_path):
    service, path = _service(tmp_path)
    path.write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    ids = [entry.entry_id for entry in snapshot.entries]
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------
def test_remove_success_removes_only_selected_physical_line(tmp_path):
    service, path = _service(tmp_path)
    path.write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    target = snapshot.entries[0]  # the first one.test line
    result = service.remove_known_host_entries(
        _remove_request(snapshot.revision, target.entry_id)
    )
    assert result.removed_count == 1
    assert result.revision != snapshot.revision
    assert [e.hostname for e in result.entries] == ["two.test", "one.test"]
    remaining = path.read_bytes()
    assert b"one.test ssh-ed25519" in remaining
    assert remaining.count(b"one.test ssh-ed25519") == 1
    # Comments, blank line and ordering survive.
    assert remaining.startswith(b"# local network\n\n")


def test_remove_duplicate_identical_lines_by_id(tmp_path):
    service, path = _service(tmp_path)
    path.write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    ids = [e.entry_id for e in snapshot.entries]
    first_duplicate = ids[0]
    second_duplicate = ids[2]
    assert first_duplicate != second_duplicate
    # Removing the first duplicate leaves the second identical line behind.
    result = service.remove_known_host_entries(
        _remove_request(snapshot.revision, first_duplicate)
    )
    assert [e.hostname for e in result.entries] == ["two.test", "one.test"]
    assert result.entries[1].display_line == snapshot.entries[2].display_line
    # A fresh snapshot can remove the other duplicate by its new id.
    refreshed = service.list_known_hosts()
    assert [e.hostname for e in refreshed.entries] == ["two.test", "one.test"]
    other_id = refreshed.entries[1].entry_id
    service.remove_known_host_entries(
        _remove_request(refreshed.revision, other_id)
    )
    assert [e.hostname for e in service.list_known_hosts().entries] == ["two.test"]


def test_remove_both_duplicates_in_one_batch(tmp_path):
    service, path = _service(tmp_path)
    path.write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    ids = [e.entry_id for e in snapshot.entries]
    result = service.remove_known_host_entries(
        _remove_request(snapshot.revision, ids[0], ids[2])
    )
    assert result.removed_count == 2
    assert [e.hostname for e in result.entries] == ["two.test"]


# ---------------------------------------------------------------------------
# Stale / external edits
# ---------------------------------------------------------------------------
def test_stale_revision_raises_stale_editor(tmp_path):
    service, path = _service(tmp_path)
    path.write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    stale = _remove_request("not-the-current-revision", snapshot.entries[0].entry_id)
    with pytest.raises(SshPilotError) as excinfo:
        service.remove_known_host_entries(stale)
    error = excinfo.value
    assert error.code is ErrorCode.STALE_EDITOR
    assert error.retryable is True
    assert error.details == {
        "resource": "known_hosts",
        "expected_revision": "not-the-current-revision",
        "current_revision": snapshot.revision,
    }
    # Nothing was removed.
    assert path.read_bytes() == CONTENT


def test_external_edit_between_list_and_remove_raises_stale(tmp_path):
    service, path = _service(tmp_path)
    path.write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    # Another process edits the file after the snapshot was taken.
    path.write_bytes(CONTENT + b"external.test ssh-ed25519 EXTERNAL\n")
    with pytest.raises(SshPilotError) as excinfo:
        service.remove_known_host_entries(
            _remove_request(snapshot.revision, snapshot.entries[0].entry_id)
        )
    assert excinfo.value.code is ErrorCode.STALE_EDITOR
    assert path.read_bytes() == CONTENT + b"external.test ssh-ed25519 EXTERNAL\n"


def test_unknown_entry_id_raises_validation_failed(tmp_path):
    service, path = _service(tmp_path)
    path.write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    with pytest.raises(SshPilotError) as excinfo:
        service.remove_known_host_entries(
            _remove_request(snapshot.revision, "nonexistent-id")
        )
    error = excinfo.value
    assert error.code is ErrorCode.VALIDATION_FAILED
    assert error.retryable is False
    assert path.read_bytes() == CONTENT


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_concurrent_removals_serialize_with_one_winner(tmp_path):
    service, path = _service(tmp_path)
    path.write_bytes(CONTENT)
    snapshot = service.list_known_hosts()
    first_id = snapshot.entries[0].entry_id
    second_id = snapshot.entries[1].entry_id
    results = []
    errors = []
    barrier = threading.Barrier(2)

    def remove(entry_id):
        barrier.wait()
        try:
            service.remove_known_host_entries(
                _remove_request(snapshot.revision, entry_id)
            )
            results.append(entry_id)
        except SshPilotError as error:
            errors.append((entry_id, error.code))

    threads = [
        threading.Thread(target=remove, args=(first_id,)),
        threading.Thread(target=remove, args=(second_id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one removal commits; the other sees a stale revision.
    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0][1] is ErrorCode.STALE_EDITOR
    remaining = path.read_bytes()
    assert remaining.count(b"ssh-ed25519") + remaining.count(b"ssh-rsa") == 2
