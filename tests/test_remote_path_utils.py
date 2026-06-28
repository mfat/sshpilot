"""Unit tests for the pure remote-path / SSH-target helpers.

These functions were extracted verbatim from window.py into
sshpilot/remote_path_utils.py (a leaf module with no GTK/I/O deps), which makes
them importable and testable in isolation. They had no direct coverage before.
"""

from sshpilot.remote_path_utils import (
    _format_ssh_target,
    _normalize_remote_path,
    _remote_parent,
    _remote_join,
    _quote_remote_path_for_shell,
)


class TestFormatSshTarget:
    def test_user_and_host(self):
        assert _format_ssh_target("host", "user") == "user@host"

    def test_no_user(self):
        assert _format_ssh_target("host", "") == "host"

    def test_ipv6_is_bracketed(self):
        assert _format_ssh_target("fe80::1", "user") == "user@[fe80::1]"

    def test_already_bracketed_ipv6_not_double_wrapped(self):
        assert _format_ssh_target("[fe80::1]", "user") == "user@[fe80::1]"


class TestNormalizeRemotePath:
    def test_empty_becomes_dot(self):
        assert _normalize_remote_path("") == "."
        assert _normalize_remote_path("   ") == "."

    def test_roots_preserved(self):
        assert _normalize_remote_path("/") == "/"
        assert _normalize_remote_path("~") == "~"
        assert _normalize_remote_path(".") == "."

    def test_home_relative_trailing_slash_stripped(self):
        assert _normalize_remote_path("~/foo/") == "~/foo"

    def test_absolute_dotdot_collapsed(self):
        assert _normalize_remote_path("/a/../b") == "/b"

    def test_relative_preserved(self):
        assert _normalize_remote_path("a/b") == "a/b"


class TestRemoteParent:
    def test_root_and_dot_have_no_parent(self):
        assert _remote_parent("/") is None
        assert _remote_parent(".") is None

    def test_home_parent(self):
        assert _remote_parent("~") == "/"
        assert _remote_parent("~/foo") == "~"

    def test_absolute_parent(self):
        assert _remote_parent("/a/b") == "/a"
        assert _remote_parent("/a") == "/"


class TestRemoteJoin:
    def test_absolute_join(self):
        assert _remote_join("/a", "b") == "/a/b"

    def test_home_join(self):
        assert _remote_join("~", "x") == "~/x"

    def test_dot_child_returns_base(self):
        assert _remote_join("/a", ".") == "/a"
        assert _remote_join("/a", "") == "/a"

    def test_dotdot_child_goes_to_parent(self):
        assert _remote_join("/a/b", "..") == "/a"


class TestQuoteRemotePathForShell:
    def test_specials(self):
        assert _quote_remote_path_for_shell(".") == "."
        assert _quote_remote_path_for_shell("/") == "/"
        assert _quote_remote_path_for_shell("~") == "$HOME"

    def test_home_relative_quotes_each_segment(self):
        assert _quote_remote_path_for_shell("~/a b") == "$HOME/'a b'"

    def test_absolute_with_space_is_quoted(self):
        assert _quote_remote_path_for_shell("/a b") == "'/a b'"
