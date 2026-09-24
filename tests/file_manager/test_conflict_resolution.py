"""Unit tests for pre-transfer conflict resolution bookkeeping."""

from __future__ import annotations

from sshpilot.file_manager.conflict_resolution import (
    ConflictAction,
    ConflictItem,
    ConflictResolutionSession,
    ConflictResponse,
    ConflictSideInfo,
    _replace_basename,
)


def _item(src: str, dst: str, *, merge: bool = False) -> ConflictItem:
    return ConflictItem(
        source=ConflictSideInfo(path=src, is_directory=merge),
        destination=ConflictSideInfo(path=dst, is_directory=merge),
        destination_directory_name="dest",
    )


def test_session_keeps_non_conflicting_pairs():
    files = [("a", "/r/a"), ("b", "/r/b"), ("c", "/r/c")]
    conflicts = [_item("b", "/r/b")]
    session = ConflictResolutionSession(files, conflicts)
    assert session.next_conflict() is conflicts[0]
    assert session.apply(ConflictResponse(ConflictAction.SKIP))
    assert session.next_conflict() is None
    assert session.resolved_pairs == [("a", "/r/a"), ("c", "/r/c")]


def test_apply_to_all_skip_and_replace():
    files = [("a", "/r/a"), ("b", "/r/b"), ("c", "/r/c")]
    conflicts = [_item("a", "/r/a"), _item("b", "/r/b"), _item("c", "/r/c")]
    session = ConflictResolutionSession(files, conflicts)
    assert session.apply(
        ConflictResponse(ConflictAction.SKIP, apply_to_all=True)
    )
    assert session.next_conflict() is None
    assert session.resolved_pairs == []

    session = ConflictResolutionSession(files, conflicts)
    assert session.apply(
        ConflictResponse(ConflictAction.REPLACE, apply_to_all=True)
    )
    assert session.next_conflict() is None
    assert session.resolved_pairs == files


def test_rename_rewrites_destination_basename():
    files = [("a", "/r/a.txt")]
    conflicts = [_item("a", "/r/a.txt")]
    session = ConflictResolutionSession(files, conflicts)
    assert session.next_conflict() is not None
    assert session.apply(
        ConflictResponse(ConflictAction.RENAME, new_name="a (1).txt")
    )
    assert session.resolved_pairs == [("a", "/r/a (1).txt")]


def test_cancel_aborts_session():
    files = [("a", "/r/a"), ("b", "/r/b")]
    conflicts = [_item("a", "/r/a")]
    session = ConflictResolutionSession(files, conflicts)
    session.next_conflict()
    assert not session.apply(ConflictResponse(ConflictAction.CANCEL))
    assert session.cancelled
    assert session.resolved_pairs == []


def test_replace_basename_remote_and_local():
    assert _replace_basename("/home/user/file.txt", "other.txt") == "/home/user/other.txt"
    assert _replace_basename("/file.txt", "other.txt") == "/other.txt"
