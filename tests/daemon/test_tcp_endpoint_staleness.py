"""Windows loopback-TCP endpoint: a leftover endpoint file must never block startup.

Exercised on every platform by forcing the TCP endpoint mode, since the
regression (a crashed daemon's ``sshpilotd.json`` read as a live daemon) is
pure lifecycle logic.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys

import pytest

from sshpilot.api.transport import endpoint
from sshpilot.daemon import DaemonAlreadyRunningError
from sshpilot.daemon import lifecycle
from sshpilot.daemon.process_registry import ProcessIdentity


@pytest.fixture
def tcp_mode(monkeypatch):
    monkeypatch.setattr(lifecycle, "_TCP", True)


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _unused_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _publish(path, *, port, pid):
    endpoint.publish_tcp_endpoint(
        path, endpoint.TcpEndpoint(port=port, token=endpoint.new_token(), pid=pid)
    )


def _windows_style_refusal(monkeypatch):
    """Make a refused connect time out first, as Windows SYN retries do."""

    calls = []

    def fake_connect(published, *, timeout=None):
        calls.append(timeout)
        if timeout is not None and timeout < 2.0:
            raise socket.timeout("timed out")
        raise ConnectionRefusedError(10061, "refused")

    monkeypatch.setattr(endpoint, "connect_tcp", fake_connect)
    return calls


def test_endpoint_of_dead_daemon_is_stale_without_probing_port(tmp_path, tcp_mode, monkeypatch, caplog):
    path = tmp_path / "sshpilotd.json"
    _publish(path, port=_unused_port(), pid=_dead_pid())

    def must_not_connect(*_args, **_kwargs):
        raise AssertionError("a dead publisher must not need a port probe")

    monkeypatch.setattr(endpoint, "connect_tcp", must_not_connect)
    with caplog.at_level(logging.INFO, logger=lifecycle.logger.name):
        assert lifecycle.probe_socket_owner(path, timeout=0.2) == (False, None)
    assert "owner process has exited" in caplog.text


def test_endpoint_of_dead_daemon_is_stale_even_if_port_was_reused(tmp_path, tcp_mode):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        path = tmp_path / "sshpilotd.json"
        _publish(path, port=listener.getsockname()[1], pid=_dead_pid())
        assert lifecycle.probe_socket_owner(path, timeout=0.2) == (False, None)
    finally:
        listener.close()


def _fake_identity(monkeypatch, pid, create_time):
    identity = ProcessIdentity(pid=pid, create_time=create_time)
    monkeypatch.setattr(
        lifecycle, "identify_process", lambda candidate: identity if candidate == pid else None
    )


def test_recycled_pid_does_not_make_endpoint_live(tmp_path, tcp_mode, monkeypatch):
    path = tmp_path / "sshpilotd.json"
    _publish(path, port=_unused_port(), pid=4242)
    published_at = path.stat().st_mtime
    # Whoever holds pid 4242 now started well after the file was written.
    _fake_identity(monkeypatch, 4242, published_at + 60)

    def must_not_connect(*_args, **_kwargs):
        raise AssertionError("a recycled publisher must not need a port probe")

    monkeypatch.setattr(endpoint, "connect_tcp", must_not_connect)
    assert lifecycle.probe_socket_owner(path, timeout=0.2) == (False, None)


def test_publisher_started_before_publish_is_not_called_recycled(tmp_path, tcp_mode, monkeypatch):
    listener, port = endpoint.create_tcp_listener()
    listener.listen()
    try:
        path = tmp_path / "sshpilotd.json"
        _publish(path, port=port, pid=4242)
        _fake_identity(monkeypatch, 4242, path.stat().st_mtime - 5)
        assert lifecycle.probe_socket_owner(path, timeout=0.2) == (True, 4242)
    finally:
        listener.close()


def test_slow_windows_refusal_is_not_mistaken_for_a_live_daemon(tmp_path, tcp_mode, monkeypatch):
    path = tmp_path / "sshpilotd.json"
    # Unknown publisher: only the port probe can decide.
    _publish(path, port=_unused_port(), pid=None)
    calls = _windows_style_refusal(monkeypatch)

    assert lifecycle.probe_socket_owner(path, timeout=0.2) == (False, None)
    assert calls == [lifecycle._TCP_REFUSAL_SETTLE_TIMEOUT]


def test_poll_loops_keep_short_probes_and_settle_once_at_the_deadline(tmp_path, tcp_mode, monkeypatch):
    path = tmp_path / "sshpilotd.json"
    _publish(path, port=_unused_port(), pid=None)
    calls = _windows_style_refusal(monkeypatch)

    assert lifecycle.wait_until_socket_free(path, timeout=0.05, poll_interval=0.01) is True
    assert calls[-1] == lifecycle._TCP_REFUSAL_SETTLE_TIMEOUT
    assert all(budget == 0.1 for budget in calls[:-1])
    assert calls.count(lifecycle._TCP_REFUSAL_SETTLE_TIMEOUT) == 1


def test_prepare_replaces_stale_endpoint_from_crashed_daemon(tmp_path, tcp_mode, monkeypatch, caplog):
    path = tmp_path / "run" / "sshpilotd.json"
    _publish(path, port=_unused_port(), pid=_dead_pid())
    _windows_style_refusal(monkeypatch)

    with caplog.at_level(logging.INFO, logger=lifecycle.logger.name):
        lifecycle.prepare_socket_path(path)

    assert not path.exists()
    assert "Removed a stale daemon endpoint" in caplog.text


def test_prepare_still_refuses_a_live_daemon(tmp_path, tcp_mode, caplog):
    listener, port = endpoint.create_tcp_listener()
    listener.listen()
    try:
        path = tmp_path / "run" / "sshpilotd.json"
        _publish(path, port=port, pid=os.getpid())
        with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
            with pytest.raises(DaemonAlreadyRunningError):
                lifecycle.prepare_socket_path(path)
        assert path.exists()
        assert "a live peer answers" in caplog.text
    finally:
        listener.close()


def test_remove_dead_socket_clears_crashed_daemon_endpoint(tmp_path, tcp_mode):
    path = tmp_path / "sshpilotd.json"
    _publish(path, port=_unused_port(), pid=_dead_pid())
    assert lifecycle.remove_dead_socket(path) is True
    assert not path.exists()


def test_evict_clears_endpoint_left_by_a_terminated_daemon(tmp_path, tcp_mode):
    # Windows os.kill is TerminateProcess, so an evicted daemon never runs
    # its own unlink; the escalation must clear the file it left behind.
    path = tmp_path / "sshpilotd.json"
    _publish(path, port=_unused_port(), pid=_dead_pid())
    assert lifecycle.evict_socket_owner(path) is True
    assert not path.exists()
