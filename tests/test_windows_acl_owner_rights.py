from __future__ import annotations

import os
import sys

import pytest

from sshpilot.platform.windows import acl

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")


@windows_only
def test_detects_owner_rights_from_mkdir_0700(tmp_path):
    # pytest's tmp_path comes from mkdtemp, which carries the entry itself.
    assert acl.drop_owner_rights(tmp_path)
    bad = tmp_path / "bad"
    os.mkdir(bad, 0o700)  # Python 3.13+ adds OWNER RIGHTS here
    good = tmp_path / "good"
    os.mkdir(good)
    if sys.version_info >= (3, 13):
        assert acl.has_owner_rights_entry(bad) is True
    assert acl.has_owner_rights_entry(good) is False
    assert acl.has_owner_rights_entry(tmp_path / "missing") is None


@windows_only
def test_repair_removes_owner_rights(tmp_path):
    bad = tmp_path / "bad"
    os.mkdir(bad, 0o700)
    assert acl.repair_owner_rights(bad) is True
    assert acl.has_owner_rights_entry(bad) is False


def test_repair_skips_icacls_when_the_entry_is_absent(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(acl.sys, "platform", "win32")
    monkeypatch.setattr(acl, "has_owner_rights_entry", lambda path: False)
    monkeypatch.setattr(acl, "drop_owner_rights", lambda path: calls.append(path) or True)
    assert acl.repair_owner_rights(tmp_path) is True
    assert calls == []


@pytest.mark.parametrize("detected", [True, None])
def test_repair_runs_when_present_or_unknown(monkeypatch, tmp_path, detected):
    calls = []
    monkeypatch.setattr(acl.sys, "platform", "win32")
    monkeypatch.setattr(acl, "has_owner_rights_entry", lambda path: detected)
    monkeypatch.setattr(acl, "drop_owner_rights", lambda path: calls.append(path) or True)
    assert acl.repair_owner_rights(tmp_path) is True
    assert calls == [tmp_path]


def test_repair_leaves_a_missing_directory_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(acl.sys, "platform", "win32")
    monkeypatch.setattr(acl, "drop_owner_rights", lambda path: pytest.fail("must not run"))
    assert acl.repair_owner_rights(tmp_path / "missing") is True
