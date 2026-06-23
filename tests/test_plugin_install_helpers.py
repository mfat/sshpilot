"""Unit tests for the plugin-install integrity helpers (SHA-256 verification)."""

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from sshpilot.preferences import PreferencesWindow as P
except Exception:  # pragma: no cover - GTK import issues in some environments
    P = None

pytestmark = pytest.mark.skipif(P is None, reason="preferences module unimportable")


def test_sha256_file_matches_hashlib(tmp_path):
    f = tmp_path / "pkg.zip"
    f.write_bytes(b"sshpilot plugin payload")
    assert P._sha256_file(str(f)) == hashlib.sha256(b"sshpilot plugin payload").hexdigest()


def test_verify_sha256_none_when_no_expected(tmp_path):
    f = tmp_path / "pkg.zip"
    f.write_bytes(b"x")
    assert P._verify_sha256(str(f), None) is None
    assert P._verify_sha256(str(f), "   ") is None


def test_verify_sha256_match_is_case_and_space_insensitive(tmp_path):
    f = tmp_path / "pkg.zip"
    f.write_bytes(b"x")
    digest = hashlib.sha256(b"x").hexdigest()
    assert P._verify_sha256(str(f), "  " + digest.upper() + " ") is True
    assert P._verify_sha256(str(f), "deadbeef") is False
