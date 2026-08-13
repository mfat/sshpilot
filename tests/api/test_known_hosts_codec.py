"""Known-hosts transport codec round-trip and strictness tests."""

import pytest

from sshpilot.api.models.known_hosts import (
    KnownHostEntryId,
    KnownHostEntrySummary,
    KnownHostsMutationResult,
    KnownHostsSnapshot,
    RemoveKnownHostEntriesRequest,
)
from sshpilot.api.transport.codec import (
    known_host_entry_summary_from_wire,
    known_host_entry_summary_to_wire,
    known_hosts_mutation_result_from_wire,
    known_hosts_mutation_result_to_wire,
    known_hosts_snapshot_from_wire,
    known_hosts_snapshot_to_wire,
    remove_known_host_entries_request_from_wire,
    remove_known_host_entries_request_to_wire,
)


def _entry(entry_id="kh-1", hostname="example.test"):
    return KnownHostEntrySummary(
        entry_id=KnownHostEntryId(entry_id),
        hostname=hostname,
        key_type="ssh-ed25519",
        display_line=f"{hostname} ssh-ed25519 AAAAC3NzaC1lZDI1NTE5",
    )


def _snapshot():
    return KnownHostsSnapshot(revision="rev-1", entries=(_entry(), _entry("kh-2")))


def _remove_request():
    return RemoveKnownHostEntriesRequest(
        revision="rev-1",
        entry_ids=(KnownHostEntryId("kh-1"), KnownHostEntryId("kh-2")),
    )


def _mutation_result():
    return KnownHostsMutationResult(
        revision="rev-2",
        removed_count=1,
        entries=(_entry("kh-2"),),
    )


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------
def test_entry_summary_round_trip():
    entry = _entry()
    assert known_host_entry_summary_from_wire(
        known_host_entry_summary_to_wire(entry)
    ) == entry


def test_snapshot_round_trip():
    snapshot = _snapshot()
    assert known_hosts_snapshot_from_wire(
        known_hosts_snapshot_to_wire(snapshot)
    ) == snapshot


def test_remove_request_round_trip():
    request = _remove_request()
    assert remove_known_host_entries_request_from_wire(
        remove_known_host_entries_request_to_wire(request)
    ) == request


def test_mutation_result_round_trip():
    result = _mutation_result()
    assert known_hosts_mutation_result_from_wire(
        known_hosts_mutation_result_to_wire(result)
    ) == result


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------
def test_entry_summary_wire_shape():
    encoded = known_host_entry_summary_to_wire(_entry())
    assert set(encoded) == {"entry_id", "hostname", "key_type", "display_line"}


def test_snapshot_wire_shape():
    encoded = known_hosts_snapshot_to_wire(_snapshot())
    assert set(encoded) == {"revision", "entries"}
    assert type(encoded["entries"]) is list


def test_remove_request_wire_shape():
    encoded = remove_known_host_entries_request_to_wire(_remove_request())
    assert set(encoded) == {"revision", "entry_ids"}
    assert type(encoded["entry_ids"]) is list


def test_mutation_result_wire_shape():
    encoded = known_hosts_mutation_result_to_wire(_mutation_result())
    assert set(encoded) == {"revision", "removed_count", "entries"}
    assert type(encoded["removed_count"]) is int
    assert type(encoded["entries"]) is list


# ---------------------------------------------------------------------------
# Strict rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "decoder,payload",
    [
        (known_host_entry_summary_from_wire, {}),
        (known_host_entry_summary_from_wire, {"entry_id": "a", "hostname": "h", "key_type": "k", "display_line": "d", "extra": 1}),
        (known_host_entry_summary_from_wire, {"entry_id": "", "hostname": "h", "key_type": "k", "display_line": "d"}),
        (known_host_entry_summary_from_wire, {"entry_id": "a", "hostname": "h", "key_type": "k", "display_line": "  "}),
        (known_hosts_snapshot_from_wire, {"revision": "r"}),
        (known_hosts_snapshot_from_wire, {"revision": "r", "entries": {}}),
        (remove_known_host_entries_request_from_wire, {"revision": "r"}),
        (remove_known_host_entries_request_from_wire, {"revision": "r", "entry_ids": {}}),
        (remove_known_host_entries_request_from_wire, {"revision": "r", "entry_ids": ["a", "a"]}),
        (known_hosts_mutation_result_from_wire, {"revision": "r", "removed_count": 1}),
        (known_hosts_mutation_result_from_wire, {"revision": "r", "removed_count": -1, "entries": []}),
        (known_hosts_mutation_result_from_wire, {"revision": "r", "removed_count": "1", "entries": []}),
    ],
)
def test_known_hosts_codecs_reject_malformed_wire(decoder, payload):
    with pytest.raises(ValueError):
        decoder(payload)


def test_snapshot_decoder_rejects_non_summary_members():
    with pytest.raises(ValueError):
        known_hosts_snapshot_from_wire(
            {"revision": "r", "entries": [{"entry_id": "a"}]}
        )


def test_mutation_result_decoder_rejects_non_summary_members():
    with pytest.raises(ValueError):
        known_hosts_mutation_result_from_wire(
            {"revision": "r", "removed_count": 0, "entries": [{"nope": 1}]}
        )


def test_entry_summary_decoder_rejects_malformed_member():
    encoded = known_hosts_snapshot_to_wire(_snapshot())
    encoded["entries"][0]["entry_id"] = ""
    with pytest.raises(ValueError):
        known_hosts_snapshot_from_wire(encoded)


# ---------------------------------------------------------------------------
# Type guards on encoding
# ---------------------------------------------------------------------------
def test_encoders_reject_wrong_object_types():
    with pytest.raises(TypeError):
        known_host_entry_summary_to_wire(object())
    with pytest.raises(TypeError):
        known_hosts_snapshot_to_wire(object())
    with pytest.raises(TypeError):
        remove_known_host_entries_request_to_wire(object())
    with pytest.raises(TypeError):
        known_hosts_mutation_result_to_wire(object())
