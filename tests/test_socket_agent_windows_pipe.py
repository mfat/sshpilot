"""The 1Password preset on Windows uses the OpenSSH agent named pipe."""

from __future__ import annotations

import importlib

import pytest

from sshpilot.providers import socket_agent


def test_pipe_paths_are_recognised_only_on_windows(monkeypatch):
    monkeypatch.setattr(socket_agent.sys, "platform", "win32")
    assert socket_agent._is_windows_pipe("\\\\.\\pipe\\openssh-ssh-agent")
    assert socket_agent._is_windows_pipe("//./pipe/openssh-ssh-agent")
    assert not socket_agent._is_windows_pipe("C:\\Users\\me\\agent.sock")
    monkeypatch.setattr(socket_agent.sys, "platform", "linux")
    assert not socket_agent._is_windows_pipe("\\\\.\\pipe\\openssh-ssh-agent")


def test_pipe_availability_lists_the_namespace_without_opening(monkeypatch):
    monkeypatch.setattr(socket_agent.sys, "platform", "win32")
    listed = []

    def fake_listdir(path):
        listed.append(path)
        return ["OpenSSH-SSH-Agent", "other"]

    monkeypatch.setattr(socket_agent.os, "listdir", fake_listdir)
    provider = socket_agent.SocketAgentProvider("x", "X", "\\\\.\\pipe\\openssh-ssh-agent")
    assert provider.is_available()
    assert listed == ["\\\\.\\pipe\\"]
    assert not socket_agent.SocketAgentProvider("y", "Y", "\\\\.\\pipe\\missing").is_available()


@pytest.mark.parametrize("platform, expected", [
    ("win32", "\\\\.\\pipe\\openssh-ssh-agent"),
    ("linux", "~/.1password/agent.sock"),
])
def test_onepassword_preset_socket(monkeypatch, platform, expected):
    monkeypatch.setattr("sys.platform", platform)
    module = importlib.reload(socket_agent)
    try:
        assert module.onepassword_provider().socket_path == expected
    finally:
        monkeypatch.undo()
        importlib.reload(socket_agent)


def test_default_agent_pipe_needs_no_identity_agent_line(monkeypatch):
    monkeypatch.setattr(socket_agent.sys, "platform", "win32")
    provider = socket_agent.SocketAgentProvider("x", "X", socket_agent.WINDOWS_AGENT_PIPE)
    assert provider.ssh_config_directives() == []
    other = socket_agent.SocketAgentProvider("y", "Y", "C:/Users/me/agent.sock")
    assert other.ssh_config_directives() == [("IdentityAgent", "C:/Users/me/agent.sock")]
