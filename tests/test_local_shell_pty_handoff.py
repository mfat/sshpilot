"""Headless coverage for the local-shell PTY handoff.

The handoff itself needs a Flatpak sandbox to exercise end to end, but its
three seams are plain data and can be pinned here: the argv the agent is
launched with, the SCM_RIGHTS message parsing, and the capability gate that
decides whether a handoff is attempted at all.
"""

import array
import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# --- the argv -------------------------------------------------------------

def _flatpak_agent_command(tmp_path, monkeypatch, **kwargs):
    from sshpilot import agent_client

    agent = tmp_path / "sshpilot_agent.py"
    agent.write_text("# agent\n")

    client = agent_client.AgentClient()
    monkeypatch.setattr(agent_client, "is_flatpak", lambda: True)
    monkeypatch.setattr(agent_client.shutil, "which", lambda _n: "/usr/bin/flatpak-spawn")
    monkeypatch.setattr(client, "find_agent", lambda: ("/usr/bin/python3", str(agent)))
    return client.build_agent_command(rows=24, cols=80, cwd="/home/u", **kwargs)


def test_handoff_command_forwards_the_socket(tmp_path, monkeypatch):
    cmd = _flatpak_agent_command(tmp_path, monkeypatch, pty_socket_fd=7)

    # The fd must be forwarded by flatpak-spawn *and* named for the agent;
    # either one alone leaves the agent unable to hand the PTY back.
    assert "--forward-fd=7" in cmd
    assert "--pty-socket 7" in cmd[-1]
    assert cmd.index("--forward-fd=7") < cmd.index("bash")


def test_relay_command_has_no_socket(tmp_path, monkeypatch):
    cmd = _flatpak_agent_command(tmp_path, monkeypatch)

    assert not any(a.startswith("--forward-fd") for a in cmd)
    assert "--pty-socket" not in cmd[-1]


# --- the handover message -------------------------------------------------

def _send(sock, payload, fds):
    sock.sendmsg(
        [payload],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))],
    )


@pytest.fixture
def receive():
    from sshpilot.terminal import TerminalWidget

    return TerminalWidget._receive_pty_master


def test_receives_the_descriptor(receive):
    a, b = socket.socketpair()
    r, w = os.pipe()
    try:
        _send(b, b"P", [r])
        got = receive(a)
        assert got != r          # a *new* descriptor for the same object
        os.write(w, b"x")
        assert os.read(got, 1) == b"x"
        os.close(got)
    finally:
        a.close(); b.close(); os.close(r); os.close(w)


def test_rejects_a_message_without_a_descriptor(receive):
    a, b = socket.socketpair()
    try:
        b.send(b"P")
        with pytest.raises(RuntimeError, match="no PTY descriptor"):
            receive(a)
    finally:
        a.close(); b.close()


def test_rejects_an_unexpected_payload_and_closes_the_fd(receive):
    """A stray sender must not get its descriptor adopted."""
    a, b = socket.socketpair()
    r, w = os.pipe()
    try:
        _send(b, b"X", [r])
        with pytest.raises(RuntimeError, match="unexpected handover message"):
            receive(a)
    finally:
        a.close(); b.close(); os.close(r); os.close(w)


# --- the capability gate --------------------------------------------------

def test_backends_declare_whether_they_can_adopt():
    from sshpilot import terminal_backends as tb

    # Checked before spawning: a backend that cannot adopt must never cause
    # an agent to be started and then abandoned holding a PTY.
    assert tb.VTETerminalBackend.supports_pty_adoption is True
    assert tb.BaseTerminalBackend.supports_pty_adoption is False
    assert tb.PyXtermTerminalBackend.supports_pty_adoption is False


def test_handoff_is_skipped_when_the_backend_cannot_adopt(monkeypatch):
    from sshpilot.terminal import TerminalWidget

    class Backend:
        supports_pty_adoption = False

        def adopt_pty(self, master_fd, watch_pid):  # pragma: no cover
            raise AssertionError("must not be reached")

    widget = TerminalWidget.__new__(TerminalWidget)
    widget.backend = Backend()

    def fail(*_a, **_k):  # pragma: no cover
        raise AssertionError("no agent may be spawned for a refused handoff")

    monkeypatch.setattr("sshpilot.terminal.GLib.spawn_async", fail)

    assert TerminalWidget._spawn_agent_shell_with_pty_handoff(
        widget, client=None, cwd="/home/u", rows=24, cols=80, verbose=False
    ) is False
