"""Unit tests for frontend-safe public-key fingerprint helpers."""

import base64
import hashlib

from sshpilot import ssh_key_fingerprint as skf
from sshpilot.ssh_key_fingerprint import (
    _fingerprint_for_path,
    _fingerprint_for_pub_line,
    _parse_keygen_line,
)


def _fingerprint(raw: bytes) -> str:
    encoded = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    return f"SHA256:{encoded}"


class TestParsePublicKeyLine:
    def test_ed25519_with_comment(self):
        assert _parse_keygen_line("ssh-ed25519 YWJj user@host") == (
            "ED25519",
            _fingerprint(b"abc"),
            "user@host",
        )

    def test_rsa_with_multiword_comment(self):
        assert _parse_keygen_line("ssh-rsa ZGVm my laptop key") == (
            "RSA",
            _fingerprint(b"def"),
            "my laptop key",
        )

    def test_empty_and_invalid(self):
        assert _parse_keygen_line("") == ("", "", "")
        assert _parse_keygen_line("ssh-ed25519") == ("", "", "")
        assert _parse_keygen_line("ssh-ed25519 not-base64!") == ("", "", "")
        assert _parse_keygen_line(None) == ("", "", "")


class TestFingerprintForPath:
    def setup_method(self):
        skf._FINGERPRINT_CACHE.clear()

    def test_reads_only_public_companion_and_caches(self, tmp_path):
        private = tmp_path / "id_ed25519"
        private.write_text("PRIVATE-MATERIAL-MUST-NOT-BE-PARSED")
        public = tmp_path / "id_ed25519.pub"
        public.write_text("ssh-ed25519 YWJj user@host\n")

        result = _fingerprint_for_path(str(private))
        assert result == ("ED25519", _fingerprint(b"abc"), "user@host")
        public.unlink()
        assert _fingerprint_for_path(str(private)) == result

    def test_missing_public_companion_yields_empty(self, tmp_path):
        private = tmp_path / "id_ed25519"
        private.write_text("PRIVATE-MATERIAL-MUST-NOT-BE-PARSED")
        assert _fingerprint_for_path(str(private)) == ("", "", "")


class TestFingerprintForPubLine:
    def test_success(self):
        assert _fingerprint_for_pub_line("ssh-ed25519 YWJj comment") == (
            "ED25519",
            _fingerprint(b"abc"),
            "comment",
        )

    def test_failure_yields_empty(self):
        assert _fingerprint_for_pub_line("bad") == ("", "", "")
