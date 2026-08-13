"""Lossless known-hosts document parsing and removal tests."""

import hashlib

import pytest

from sshpilot.core.known_hosts.document import (
    parse_known_hosts_document,
    remove_known_hosts_document_entries,
)

BASIC = (
    b"# comment line\n"
    b"\n"
    b"example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI\n"
    b"\t  # indented comment\r\n"
    b"another.test ssh-rsa AAAAB3NzaC1yc2E\n"
)
BASIC_WITHOUT_TERMINATOR = BASIC[:-1]  # same content, no final newline


def _ids(document):
    return [entry.entry_id for entry in document.entries]


# ---------------------------------------------------------------------------
# Revision and entry identity
# ---------------------------------------------------------------------------
def test_revision_is_sha256_of_exact_bytes():
    document = parse_known_hosts_document(BASIC)
    assert document.revision == hashlib.sha256(BASIC).hexdigest()
    assert len(document.revision) == 64

    altered = BASIC.replace(b"example.test", b"example.org")
    other = parse_known_hosts_document(altered)
    assert other.revision != document.revision


def test_entry_ids_are_deterministic_for_revision_and_line_index():
    first = parse_known_hosts_document(BASIC)
    second = parse_known_hosts_document(BASIC)
    assert _ids(first) == _ids(second)
    for entry in first.entries:
        assert entry.entry_id == f"{first.revision}:{entry.line_index}"


def test_duplicate_identical_lines_have_distinct_ids():
    content = b"host.test ssh-ed25519 AAAA\nhost.test ssh-ed25519 AAAA\n"
    document = parse_known_hosts_document(content)
    ids = _ids(document)
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert ids[0] != ids[1]


# ---------------------------------------------------------------------------
# Display rules
# ---------------------------------------------------------------------------
def test_blank_lines_are_not_displayed():
    document = parse_known_hosts_document(b"a.test ssh-ed25519 AAA\n\n\n  \n")
    assert len(document.entries) == 1
    assert document.entries[0].parsed.hostname == "a.test"


def test_comment_lines_are_not_displayed():
    content = b"# top comment\na.test ssh-ed25519 AAA\n  # indented comment\n"
    document = parse_known_hosts_document(content)
    assert len(document.entries) == 1
    assert document.entries[0].parsed.hostname == "a.test"


def test_malformed_lines_are_displayed():
    document = parse_known_hosts_document(b"just-a-single-token\n")
    assert len(document.entries) == 1
    assert document.entries[0].parsed.line == "just-a-single-token"
    assert document.entries[0].parsed.hostname == ""


def test_malformed_multiword_lines_keep_raw_text():
    document = parse_known_hosts_document(b"not a known hosts line\n")
    assert len(document.entries) == 1
    assert document.entries[0].parsed.line == "not a known hosts line"


def test_invalid_utf8_is_decoded_with_replacement():
    document = parse_known_hosts_document(b"h\xff.test ssh-ed25519 AAA\n")
    assert len(document.entries) == 1
    assert "\ufffd" in document.entries[0].parsed.line


def test_crlf_lines_are_displayed_without_terminator():
    document = parse_known_hosts_document(b"a.test ssh-ed25519 AAA\r\n")
    assert document.entries[0].parsed.line == "a.test ssh-ed25519 AAA"


# ---------------------------------------------------------------------------
# Serialization fidelity
# ---------------------------------------------------------------------------
def test_document_keeps_exact_original_bytes():
    document = parse_known_hosts_document(BASIC)
    assert document.original == BASIC
    # The raw file bytes (comments, blanks, terminators) are repr-hidden;
    # only displayed entry lines appear in the repr.
    assert "# comment line" not in repr(document)
    assert "\r\n" not in repr(document)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"a.test ssh-ed25519 AAA\n",
        b"a.test ssh-ed25519 AAA",  # no final newline
        b"# only comments\n\n",
        b"one\r\ntwo\nthree\r\n",
    ],
)
def test_document_round_trips_all_line_shapes(content):
    document = parse_known_hosts_document(content)
    assert document.original == content


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------
def test_removal_changes_only_selected_physical_lines():
    document = parse_known_hosts_document(BASIC)
    target = document.entries[0]  # example.test line (physical line 2)
    remaining = remove_known_hosts_document_entries(document, (target.entry_id,))
    assert remaining == (
        b"# comment line\n"
        b"\n"
        b"\t  # indented comment\r\n"
        b"another.test ssh-rsa AAAAB3NzaC1yc2E\n"
    )
    # Comments, blank lines, CRLF and the final newline survive exactly.


def test_removal_preserves_no_final_newline():
    content = b"a.test ssh-ed25519 AAA\nb.test ssh-rsa BBB"
    document = parse_known_hosts_document(content)
    target = document.entries[0]
    remaining = remove_known_hosts_document_entries(document, (target.entry_id,))
    assert remaining == b"b.test ssh-rsa BBB"


def test_removal_can_remove_multiple_selected_lines():
    document = parse_known_hosts_document(BASIC)
    ids = tuple(entry.entry_id for entry in document.entries)
    remaining = remove_known_hosts_document_entries(document, ids)
    assert remaining == b"# comment line\n\n\t  # indented comment\r\n"


def test_removal_of_duplicate_identical_lines_selects_physical_lines():
    content = b"host.test ssh-ed25519 AAAA\nhost.test ssh-ed25519 AAAA\n"
    document = parse_known_hosts_document(content)
    first, second = document.entries
    assert remove_known_hosts_document_entries(
        document, (first.entry_id,)
    ) == b"host.test ssh-ed25519 AAAA\n"
    assert remove_known_hosts_document_entries(
        document, (second.entry_id,)
    ) == b"host.test ssh-ed25519 AAAA\n"


def test_removal_rejects_unknown_entry_id():
    document = parse_known_hosts_document(BASIC)
    with pytest.raises(ValueError):
        remove_known_hosts_document_entries(document, ("nope:0",))


def test_removal_rejects_duplicate_requested_ids():
    document = parse_known_hosts_document(BASIC)
    target = document.entries[0].entry_id
    with pytest.raises(ValueError):
        remove_known_hosts_document_entries(document, (target, target))


def test_removed_bytes_reparse_into_fresh_revision():
    document = parse_known_hosts_document(BASIC)
    target = document.entries[0].entry_id
    remaining = remove_known_hosts_document_entries(document, (target,))
    reparsed = parse_known_hosts_document(remaining)
    assert reparsed.revision != document.revision
    assert len(reparsed.entries) == len(document.entries) - 1
