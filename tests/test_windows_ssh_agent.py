"""OpenSSH for Windows' agent: a fixed named pipe, and an environment ssh-add needs."""

from __future__ import annotations

import pytest

from sshpilot.daemon import identity_service as daemon_identity
from sshpilot.providers import system_agent


def test_ssh_auth_sock_wins_everywhere(monkeypatch):
    monkeypatch.setattr(system_agent.sys, "platform", "win32")
    monkeypatch.setattr(system_agent, "_windows_agent_pipe_exists", lambda: True)
    assert system_agent.system_agent_socket({"SSH_AUTH_SOCK": "/tmp/agent"}) == "/tmp/agent"


@pytest.mark.parametrize("pipe_exists, expected", [(True, system_agent.WINDOWS_AGENT_PIPE), (False, "")])
def test_windows_agent_service_pipe_counts_without_ssh_auth_sock(monkeypatch, pipe_exists, expected):
    monkeypatch.setattr(system_agent.sys, "platform", "win32")
    monkeypatch.setattr(system_agent, "_windows_agent_pipe_exists", lambda: pipe_exists)
    assert system_agent.system_agent_socket({}) == expected


def test_pipe_is_not_probed_off_windows(monkeypatch):
    monkeypatch.setattr(system_agent.sys, "platform", "linux")

    def _forbidden():
        raise AssertionError("the Windows pipe must not be probed on POSIX")

    monkeypatch.setattr(system_agent, "_windows_agent_pipe_exists", _forbidden)
    assert system_agent.system_agent_socket({}) == ""


def test_pipe_path_spelling():
    assert system_agent.WINDOWS_AGENT_PIPE == "\\\\.\\pipe\\openssh-ssh-agent"


def _base_env(environ):
    service = daemon_identity.DaemonIdentityService.__new__(daemon_identity.DaemonIdentityService)
    service._environ = environ
    return service._base_env()


def test_windows_base_env_keeps_what_ssh_add_needs(monkeypatch):
    monkeypatch.setattr(daemon_identity.sys, "platform", "win32")
    env = _base_env({"ProgramData": "C:\\ProgramData", "SystemRoot": "C:\\Windows", "BW_SESSION": "x"})
    assert env["ProgramData"] == "C:\\ProgramData"
    assert env["SystemRoot"] == "C:\\Windows"
    assert "BW_SESSION" not in env


def test_posix_base_env_is_unchanged(monkeypatch):
    monkeypatch.setattr(daemon_identity.sys, "platform", "linux")
    env = _base_env({"PATH": "/usr/bin", "PROGRAMDATA": "x", "SYSTEMROOT": "y"})
    assert "PROGRAMDATA" not in env and "SYSTEMROOT" not in env
    assert env["PATH"] == "/usr/bin"
