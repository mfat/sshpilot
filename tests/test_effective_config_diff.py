"""Headless check for diff_effective_config: a global Host * block that adds/
overrides values must be detected; a clean config must report no diff.

Runs the real `ssh -G`; skips if ssh is unavailable.
"""

import os
import shutil
import tempfile

import pytest

from sshpilot.ssh_config_utils import (
    _effective_config_lines,
    diff_effective_config,
    get_effective_ssh_config,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ssh") is None, reason="ssh binary not available"
)

OWN_BLOCK = (
    "Host foo\n"
    "    HostName 10.0.0.1\n"
    "    User hostuser\n"
    "    IdentityFile /keys/host_key\n"
)


def _write(text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="sshpilot-test-", suffix=".conf")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def test_global_override_and_accumulation_detected():
    # Host * on top: overrides User (single-valued) and adds a key (accumulate).
    full = _write(
        "Host *\n"
        "    User globaluser\n"
        "    IdentityFile /keys/global_key\n\n"
        + OWN_BLOCK
    )
    try:
        result = diff_effective_config("foo", full, OWN_BLOCK)
    finally:
        os.unlink(full)

    assert result is not None, "ssh -G should have produced a comparison"
    assert result["has_diff"] is True
    changes = {c["key"]: c for c in result["changes"]}

    # User: global (Host * on top) overrode the host's value (first-match wins).
    assert "user" in changes
    assert changes["user"]["kind"] == "overridden"
    assert changes["user"]["own"] == ["hostuser"]
    assert changes["user"]["effective"] == ["globaluser"]

    # IdentityFile: global key accumulated alongside the host key (not replaced).
    assert "identityfile" in changes
    assert changes["identityfile"]["kind"] == "added"
    assert "/keys/global_key" in changes["identityfile"]["added"]
    assert "/keys/host_key" in changes["identityfile"]["own"]


def test_clean_config_reports_no_diff():
    full = _write(OWN_BLOCK)  # no globals at all
    try:
        result = diff_effective_config("foo", full, OWN_BLOCK)
    finally:
        os.unlink(full)

    assert result is not None
    assert result["has_diff"] is False
    assert result["changes"] == []


def _use_home(tmp_path, monkeypatch, config_text=None) -> None:
    """Point HOME at a scratch dir, optionally writing ~/.ssh/config."""
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(parents=True)
    if config_text is not None:
        (ssh_dir / "config").write_text(config_text)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_default_config_path_excludes_system_defaults(tmp_path, monkeypatch):
    """config_file=None must resolve the full side with -F ~/.ssh/config (#1138).

    Plain ``ssh -G`` also reads /etc/ssh/ssh_config, which -F suppresses on the
    own-block side — that asymmetry leaked distro defaults (SendEnv,
    HashKnownHosts, GSSAPIAuthentication) into the diff as phantom "added"
    entries. With -F on both sides the block matches itself.
    """
    _use_home(tmp_path, monkeypatch, OWN_BLOCK)

    result = diff_effective_config("foo", None, OWN_BLOCK)

    assert result is not None
    assert result["has_diff"] is False
    assert result["changes"] == []
    # The DISPLAYED full side stays what a real ssh invocation resolves (the
    # app launches ssh without -F in default mode, so /etc/ssh/ssh_config
    # applies there) — only change detection excludes the system-wide config.
    # (diff_effective_config ~-expands path directives for display; undo that.)
    home = str(tmp_path / "home")
    normalized = [line.replace(home + "/.ssh", "~/.ssh") for line in result["full"]]
    assert normalized == _effective_config_lines(get_effective_ssh_config("foo"))


def test_user_level_global_additions_still_reported(tmp_path, monkeypatch):
    """System defaults are excluded, but a Host * in the USER's own config that
    adds a directive must still surface — as an addition, without pretending
    the block authored ssh's compiled default value."""
    _use_home(
        tmp_path, monkeypatch,
        "Host *\n    ServerAliveInterval 30\n\n" + OWN_BLOCK,
    )

    result = diff_effective_config("foo", None, OWN_BLOCK)

    assert result is not None and result["has_diff"] is True
    changes = {c["key"]: c for c in result["changes"]}
    assert set(changes) == {"serveraliveinterval"}
    assert changes["serveraliveinterval"]["kind"] == "added"
    assert changes["serveraliveinterval"]["own"] == []
    assert changes["serveraliveinterval"]["effective"] == ["30"]


def test_missing_user_config_reports_no_diff(tmp_path, monkeypatch):
    """No ~/.ssh/config at all: nothing can override the block."""
    _use_home(tmp_path, monkeypatch, config_text=None)

    result = diff_effective_config("foo", None, OWN_BLOCK)

    assert result is not None
    assert result["has_diff"] is False
    assert result["changes"] == []


def test_global_value_change_on_unauthored_key_reports_added():
    """A Host * changing a compiled default (a key the block never authored) is
    an 'added' row, not a misleading 'overridden: default → value' row."""
    full = _write("Host *\n    Compression yes\n\n" + OWN_BLOCK)
    try:
        result = diff_effective_config("foo", full, OWN_BLOCK)
    finally:
        os.unlink(full)

    assert result is not None and result["has_diff"] is True
    changes = {c["key"]: c for c in result["changes"]}
    assert set(changes) == {"compression"}
    assert changes["compression"]["kind"] == "added"
    assert changes["compression"]["own"] == []
    assert changes["compression"]["effective"] == ["yes"]


if __name__ == "__main__":
    test_global_override_and_accumulation_detected()
    test_clean_config_reports_no_diff()
    print("ok")
