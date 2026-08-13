"""Tests for remote path validation helpers."""

import pytest

from sshpilot.api.remote_path import (
    RemotePathError,
    is_absolute_remote_path,
    remote_path_basename,
    remote_path_dirname,
    remote_path_join,
    validate_remote_path,
)


def test_validate_accepts_spaces_and_unicode():
    assert validate_remote_path("/tmp/سلام world 😀.txt") == "/tmp/سلام world 😀.txt"
    assert validate_remote_path("/tmp/--leading-dash") == "/tmp/--leading-dash"
    assert validate_remote_path("/tmp/a;b|c&d$(x)`y`") == "/tmp/a;b|c&d$(x)`y`"


def test_validate_rejects_nul_and_empty():
    with pytest.raises(RemotePathError):
        validate_remote_path("")
    with pytest.raises(RemotePathError):
        validate_remote_path("a\x00b")
    with pytest.raises(RemotePathError):
        validate_remote_path(None)


def test_join_rejects_nested_separators_in_name():
    assert remote_path_join("/tmp", "file name") == "/tmp/file name"
    with pytest.raises(RemotePathError):
        remote_path_join("/tmp", "a/b")
    assert remote_path_join("/tmp", "..") == "/tmp/.."
    assert is_absolute_remote_path("/a")
    assert not is_absolute_remote_path("a")
    assert remote_path_basename("/a/b/c") == "c"
    assert remote_path_dirname("/a/b/c") == "/a/b"
    assert remote_path_dirname("/") == "/"
