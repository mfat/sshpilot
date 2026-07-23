"""Headless check for diff_effective_config: a global Host * block that adds/
overrides values must be detected; a clean config must report no diff.

Runs the real `ssh -G`; skips if ssh is unavailable.
"""

import os
import shutil
import tempfile

import pytest

from sshpilot.ssh_config_utils import diff_effective_config

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


if __name__ == "__main__":
    test_global_override_and_accumulation_detected()
    test_clean_config_reports_no_diff()
    print("ok")
