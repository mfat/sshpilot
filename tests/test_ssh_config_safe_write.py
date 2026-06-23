"""Safety helpers shared by the SSH config editors: atomic_write_text (crash-safe
write + backup + perms) and validate_ssh_config_text (ssh -G dry run)."""

import os
import shutil
import stat
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.ssh_config_utils import atomic_write_text, validate_ssh_config_text


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_atomic_write_creates_file_with_requested_mode(tmp_path):
    target = tmp_path / "config"
    atomic_write_text(str(target), "Host a\n    HostName a.example\n", mode=0o600)
    assert target.read_text() == "Host a\n    HostName a.example\n"
    assert _mode(str(target)) == 0o600


def test_atomic_write_makes_backup_and_keeps_perms(tmp_path):
    target = tmp_path / "config"
    target.write_text("old contents\n")
    os.chmod(target, 0o600)
    atomic_write_text(str(target), "new contents\n", mode=0o600, backup=True)
    assert target.read_text() == "new contents\n"
    bak = tmp_path / "config.bak"
    assert bak.exists() and bak.read_text() == "old contents\n"
    assert _mode(str(bak)) == 0o600


def test_atomic_write_preserves_existing_mode_when_mode_none(tmp_path):
    target = tmp_path / "config"
    target.write_text("x\n")
    os.chmod(target, 0o600)
    atomic_write_text(str(target), "y\n")  # mode=None
    assert _mode(str(target)) == 0o600  # not widened to umask default


def test_atomic_write_no_partial_file_on_replace(tmp_path):
    # The temp file lives in the same dir and is renamed in; after a successful
    # write there should be no leftover .sshpilot-*.tmp files.
    target = tmp_path / "config"
    atomic_write_text(str(target), "data\n")
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith('.sshpilot-')]
    assert leftovers == []


@pytest.mark.skipif(shutil.which('ssh') is None, reason="ssh not installed")
def test_validate_accepts_good_config():
    assert validate_ssh_config_text(
        "Host good\n    HostName good.example\n    Port 22\n") is None


@pytest.mark.skipif(shutil.which('ssh') is None, reason="ssh not installed")
def test_validate_rejects_bad_directive():
    err = validate_ssh_config_text("Host bad\n    ThisIsNotARealOption yes\n")
    assert err is not None
    assert "bad" in err.lower() or "option" in err.lower() or "line" in err.lower()
