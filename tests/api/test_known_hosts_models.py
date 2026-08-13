"""Known-hosts protocol model construction and validation tests."""

import pytest

from sshpilot.api.models.known_hosts import (
    KnownHostEntryId,
    KnownHostEntrySummary,
    KnownHostsMutationResult,
    KnownHostsSnapshot,
    RemoveKnownHostEntriesRequest,
)


def _entry(
    entry_id="kh-1",
    hostname="example.test",
    key_type="ssh-ed25519",
    display_line=None,
):
    return KnownHostEntrySummary(
        entry_id=KnownHostEntryId(entry_id),
        hostname=hostname,
        key_type=key_type,
        display_line=(
            display_line
            if display_line is not None
            else f"{hostname} {key_type} AAAAC3NzaC1lZDI1NTE5"
        ),
    )


def _snapshot(revision="rev-1", *entries):
    return KnownHostsSnapshot(
        revision=revision,
        entries=tuple(entries) or (_entry(),),
    )


# ---------------------------------------------------------------------------
# Valid construction
# ---------------------------------------------------------------------------
def test_entry_summary_constructs():
    entry = _entry()
    assert entry.entry_id == "kh-1"
    assert entry.hostname == "example.test"
    assert entry.key_type == "ssh-ed25519"
    assert entry.display_line.startswith("example.test")


def test_snapshot_constructs_with_tuple_entries():
    snapshot = _snapshot("rev-1", _entry("a"), _entry("b"))
    assert snapshot.revision == "rev-1"
    assert len(snapshot.entries) == 2


def test_remove_request_constructs_with_tuple_ids():
    request = RemoveKnownHostEntriesRequest(
        revision="rev-1",
        entry_ids=(KnownHostEntryId("a"), KnownHostEntryId("b")),
    )
    assert request.revision == "rev-1"
    assert request.entry_ids == ("a", "b")


def test_mutation_result_constructs():
    result = KnownHostsMutationResult(
        revision="rev-2",
        removed_count=1,
        entries=(_entry("b"),),
    )
    assert result.removed_count == 1
    assert result.revision == "rev-2"


# ---------------------------------------------------------------------------
# IDs and revisions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"entry_id": ""},
        {"entry_id": "   "},
    ],
)
def test_entry_summary_rejects_empty_entry_id(kwargs):
    with pytest.raises(ValueError):
        KnownHostEntrySummary(
            entry_id=KnownHostEntryId(kwargs["entry_id"]),
            hostname="example.test",
            key_type="ssh-ed25519",
            display_line="example.test ssh-ed25519 x",
        )


def test_entry_summary_rejects_non_string_entry_id():
    with pytest.raises(ValueError):
        KnownHostEntrySummary(
            entry_id=123,  # type: ignore[arg-type]
            hostname="example.test",
            key_type="ssh-ed25519",
            display_line="example.test ssh-ed25519 x",
        )


@pytest.mark.parametrize("revision", ["", "  "])
def test_snapshot_rejects_empty_revision(revision):
    with pytest.raises(ValueError):
        KnownHostsSnapshot(revision=revision, entries=(_entry(),))


@pytest.mark.parametrize("revision", ["", "  "])
def test_remove_request_rejects_empty_revision(revision):
    with pytest.raises(ValueError):
        RemoveKnownHostEntriesRequest(
            revision=revision,
            entry_ids=(KnownHostEntryId("a"),),
        )


@pytest.mark.parametrize("revision", ["", "  "])
def test_mutation_result_rejects_empty_revision(revision):
    with pytest.raises(ValueError):
        KnownHostsMutationResult(
            revision=revision,
            removed_count=0,
            entries=(_entry(),),
        )


# ---------------------------------------------------------------------------
# Text field typing / content
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field_name", ["hostname", "key_type", "display_line"])
def test_entry_summary_rejects_non_string_text_fields(field_name):
    kwargs = {
        "entry_id": KnownHostEntryId("kh-1"),
        "hostname": "example.test",
        "key_type": "ssh-ed25519",
        "display_line": "example.test ssh-ed25519 x",
    }
    kwargs[field_name] = 123  # type: ignore[assignment]
    with pytest.raises(TypeError):
        KnownHostEntrySummary(**kwargs)


@pytest.mark.parametrize("field_name", ["hostname", "key_type", "display_line"])
def test_entry_summary_rejects_nul_in_text_fields(field_name):
    kwargs = {
        "entry_id": KnownHostEntryId("kh-1"),
        "hostname": "example.test",
        "key_type": "ssh-ed25519",
        "display_line": "example.test ssh-ed25519 x",
    }
    kwargs[field_name] = "bad\x00text"
    with pytest.raises(ValueError):
        KnownHostEntrySummary(**kwargs)


@pytest.mark.parametrize("display_line", ["", "   "])
def test_entry_summary_rejects_empty_display_line(display_line):
    with pytest.raises(ValueError):
        KnownHostEntrySummary(
            entry_id=KnownHostEntryId("kh-1"),
            hostname="example.test",
            key_type="ssh-ed25519",
            display_line=display_line,
        )


# ---------------------------------------------------------------------------
# Collection strictness (no list normalization)
# ---------------------------------------------------------------------------
def test_snapshot_rejects_list_entries():
    with pytest.raises(TypeError):
        KnownHostsSnapshot(revision="rev-1", entries=[_entry()])  # type: ignore[arg-type]


def test_snapshot_rejects_wrong_member_type():
    with pytest.raises(TypeError):
        KnownHostsSnapshot(revision="rev-1", entries=(object(),))  # type: ignore[arg-type]


def test_remove_request_rejects_list_ids():
    with pytest.raises(TypeError):
        RemoveKnownHostEntriesRequest(  # type: ignore[arg-type]
            revision="rev-1",
            entry_ids=["a"],
        )


def test_remove_request_rejects_empty_ids():
    with pytest.raises(ValueError):
        RemoveKnownHostEntriesRequest(revision="rev-1", entry_ids=())


def test_remove_request_rejects_non_string_id_members():
    with pytest.raises(ValueError):
        RemoveKnownHostEntriesRequest(
            revision="rev-1",
            entry_ids=(KnownHostEntryId("a"), 123),  # type: ignore[arg-type]
        )


def test_remove_request_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        RemoveKnownHostEntriesRequest(
            revision="rev-1",
            entry_ids=(KnownHostEntryId("a"), KnownHostEntryId("a")),
        )


def test_mutation_result_rejects_list_entries():
    with pytest.raises(TypeError):
        KnownHostsMutationResult(  # type: ignore[arg-type]
            revision="rev-2",
            removed_count=0,
            entries=[_entry()],
        )


def test_mutation_result_rejects_wrong_member_type():
    with pytest.raises(TypeError):
        KnownHostsMutationResult(
            revision="rev-2",
            removed_count=0,
            entries=(object(),),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# removed_count
# ---------------------------------------------------------------------------
def test_mutation_result_rejects_negative_removed_count():
    with pytest.raises(ValueError):
        KnownHostsMutationResult(
            revision="rev-2",
            removed_count=-1,
            entries=(),
        )


def test_mutation_result_rejects_non_integer_removed_count():
    with pytest.raises(ValueError):
        KnownHostsMutationResult(
            revision="rev-2",
            removed_count="1",  # type: ignore[arg-type]
            entries=(),
        )


# ---------------------------------------------------------------------------
# repr safety
# ---------------------------------------------------------------------------
def test_repr_does_not_expose_display_line():
    entry = _entry(display_line="secret-host ssh-ed25519 AAAA-SECRET")
    snapshot = _snapshot("rev-1", entry)
    assert "secret-host" not in repr(snapshot)
    assert "AAAA-SECRET" not in repr(snapshot)
    assert "secret-host" not in repr(entry)
    assert "AAAA-SECRET" not in repr(entry)
