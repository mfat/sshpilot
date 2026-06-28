"""Unit tests for the SSH key fingerprint helpers.

These were extracted verbatim from connection_dialog.py into the leaf module
sshpilot/ssh_key_fingerprint.py so they can be imported and tested without the
GTK dialog. They had no direct coverage before. The subprocess-backed lookups
are tested with a monkeypatched ssh-keygen so the tests are hermetic.
"""

import subprocess

from sshpilot import ssh_key_fingerprint as skf
from sshpilot.ssh_key_fingerprint import (
    _parse_keygen_line,
    _fingerprint_for_path,
    _fingerprint_for_pub_line,
)


class TestParseKeygenLine:
    def test_full_line_with_type_and_comment(self):
        line = "256 SHA256:abc123 user@host (ED25519)"
        assert _parse_keygen_line(line) == ("ED25519", "SHA256:abc123", "user@host")

    def test_multiword_comment(self):
        line = "3072 SHA256:deadbeef my laptop key (RSA)"
        assert _parse_keygen_line(line) == ("RSA", "SHA256:deadbeef", "my laptop key")

    def test_no_type_parenthesis(self):
        # parts[-1] does not start with "(" -> no type label
        line = "256 SHA256:xyz user@host"
        assert _parse_keygen_line(line) == ("", "SHA256:xyz", "user@host")

    def test_empty_and_too_short(self):
        assert _parse_keygen_line("") == ("", "", "")
        assert _parse_keygen_line("256") == ("", "", "")
        assert _parse_keygen_line(None) == ("", "", "")


class TestFingerprintForPath:
    def setup_method(self):
        skf._FINGERPRINT_CACHE.clear()

    def test_success_parses_and_caches(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout="256 SHA256:abc user@host (ED25519)", stderr="")

        monkeypatch.setattr(skf.subprocess, "run", fake_run)
        result = _fingerprint_for_path("~/key")
        assert result == ("ED25519", "SHA256:abc", "user@host")
        # Second call is served from cache (ssh-keygen not invoked again).
        assert _fingerprint_for_path("~/key") == result
        assert len(calls) == 1

    def test_nonzero_returncode_yields_empty(self, monkeypatch):
        monkeypatch.setattr(
            skf.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="nope"))
        assert _fingerprint_for_path("/missing") == ("", "", "")

    def test_exception_is_swallowed(self, monkeypatch):
        def boom(cmd, **kw):
            raise OSError("ssh-keygen not found")

        monkeypatch.setattr(skf.subprocess, "run", boom)
        assert _fingerprint_for_path("/x") == ("", "", "")


class TestFingerprintForPubLine:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            skf.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 0, stdout="256 SHA256:pub comment (ED25519)", stderr=""))
        assert _fingerprint_for_pub_line("ssh-ed25519 AAAA...") == (
            "ED25519", "SHA256:pub", "comment")

    def test_failure_yields_empty(self, monkeypatch):
        def boom(cmd, **kw):
            raise OSError()

        monkeypatch.setattr(skf.subprocess, "run", boom)
        assert _fingerprint_for_pub_line("bad") == ("", "", "")
