"""Tests for the rbw setup status probe (sshpilot.rbw_setup)."""

import json

import sshpilot.rbw_setup as rs


def _mock_rbw(monkeypatch, *, installed=True, email="", unlocked=True):
    monkeypatch.setattr(
        rs, "_rbw_argv",
        lambda *a: (["/usr/bin/rbw", *a] if installed else None),
    )

    class _R:
        def __init__(self, rc=0, out=b""):
            self.returncode = rc
            self.stdout = out
            self.stderr = b""

    def fake_run(argv, input=None, capture_output=None, env=None, check=None, timeout=None):
        rest = argv[1:]
        if rest[:2] == ["config", "show"]:
            return _R(0, json.dumps({"email": email, "base_url": ""}).encode())
        if rest[:1] == ["unlocked"]:
            return _R(0 if unlocked else 1)
        return _R(0)

    monkeypatch.setattr(rs.subprocess, "run", fake_run)


def test_probe_not_installed(monkeypatch):
    _mock_rbw(monkeypatch, installed=False)
    s = rs.probe_rbw_status()
    assert s.cli_installed is False
    assert s.is_ready is False


def test_probe_installed_but_unconfigured(monkeypatch):
    _mock_rbw(monkeypatch, email="", unlocked=True)
    s = rs.probe_rbw_status()
    assert s.cli_installed is True
    assert s.configured is False
    assert s.is_ready is False


def test_probe_configured_but_locked(monkeypatch):
    _mock_rbw(monkeypatch, email="alice@example.com", unlocked=False)
    s = rs.probe_rbw_status()
    assert s.configured is True
    assert s.unlocked is False
    assert s.is_ready is False


def test_probe_ready(monkeypatch):
    _mock_rbw(monkeypatch, email="alice@example.com", unlocked=True)
    s = rs.probe_rbw_status()
    assert s.is_ready is True
    assert s.email == "alice@example.com"
