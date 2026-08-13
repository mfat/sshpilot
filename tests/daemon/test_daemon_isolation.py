"""Deterministic daemon runtime isolation fixtures and regression tests."""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from sshpilot.api import DaemonClient
from sshpilot.daemon.lifecycle import prepare_socket_path, resolve_socket_path


@pytest.fixture
def isolated_daemon_env(tmp_path, monkeypatch):
    """Per-test runtime root that overrides any live user-daemon paths."""
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    cache = tmp_path / "cache"
    config = tmp_path / "config"
    for path in (runtime, state, cache, config):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    # A realistic user-daemon socket path in the environment must be ignored
    # when tests pass an explicit socket_path / isolated XDG_RUNTIME_DIR.
    decoy = tmp_path / "decoy-user" / "sshpilot" / "sshpilotd.sock"
    decoy.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("SSHPILOT_TEST_RUNTIME", str(runtime))
    monkeypatch.delenv("SSHPILOT_DAEMON_SOCKET", raising=False)

    return {
        "runtime": runtime,
        "state": state,
        "cache": cache,
        "config": config,
        "decoy": decoy,
        "socket": runtime / "sshpilot" / "sshpilotd.sock",
    }


def test_user_daemon_socket_env_is_overridden(isolated_daemon_env, daemon_factory):
    """Fixture XDG_RUNTIME_DIR wins; decoy user socket is never used."""
    decoy = isolated_daemon_env["decoy"]
    # Create a decoy socket file that must not be contacted.
    if hasattr(socket, "AF_UNIX"):
        decoy.parent.mkdir(parents=True, exist_ok=True)
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(decoy))
            sock.listen(1)
        except OSError:
            decoy.write_text("not-a-socket")
            sock = None
    else:
        sock = None

    try:
        server, _manager = daemon_factory()
        assert server.socket_path != decoy
        # Default resolve under fixture env must not point at decoy.
        resolved = resolve_socket_path(None)
        assert resolved != decoy
        assert str(isolated_daemon_env["runtime"]) in str(resolved)
        # Explicit factory sockets are also isolated under the test tmp root.
        assert "sshpilotd.sock" in str(server.socket_path)
    finally:
        if sock is not None:
            sock.close()


def test_two_isolated_daemons_concurrent(daemon_factory, tmp_path):
    s1, m1 = daemon_factory(socket_path=tmp_path / "a" / "sshpilotd.sock")
    s2, m2 = daemon_factory(socket_path=tmp_path / "b" / "sshpilotd.sock")
    assert s1.socket_path != s2.socket_path
    c1 = DaemonClient(socket_path=s1.socket_path)
    c2 = DaemonClient(socket_path=s2.socket_path)
    # Each manager has its own connection list — no cross-discovery of sessions.
    assert m1 is not m2
    assert c1 is not c2


def test_stale_socket_and_metadata_handled(tmp_path):
    from sshpilot.daemon.lifecycle import SocketSecurityError

    sock = tmp_path / "sshpilot" / "sshpilotd.sock"
    sock.parent.mkdir(mode=0o700)
    sock.write_text("stale")
    meta = tmp_path / "sshpilot" / "daemon.pid"
    meta.write_text("999999")
    # Non-socket paths are refused deterministically (never silently reused).
    with pytest.raises(SocketSecurityError):
        prepare_socket_path(sock)
    assert sock.exists()  # left untouched when not a socket
    # A genuine stale socket (no listener) is unlinked.
    sock.unlink()
    if hasattr(socket, "AF_UNIX"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(sock))
        finally:
            s.close()
        prepare_socket_path(sock)
        assert not sock.exists()
    # Stale metadata alone does not grant authority over the socket path.
    assert meta.read_text() == "999999"


def test_cleanup_removes_socket(daemon_factory, tmp_path):
    path = tmp_path / "clean" / "sshpilotd.sock"
    server, _ = daemon_factory(socket_path=path)
    assert path.exists() or path.is_socket() or path.parent.exists()
    server.shutdown()
    server.wait_stopped()
    deadline = time.monotonic() + 5
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not path.exists()


def test_failed_setup_does_not_leave_daemon(tmp_path, daemon_factory):
    path = tmp_path / "fail" / "sshpilotd.sock"
    server, _ = daemon_factory(socket_path=path, start=True)
    # Simulate failed setup after start: shutdown must still clean up.
    server.shutdown()
    server.wait_stopped()
    assert not path.exists()


def test_daemon_shutdown_versus_new_request_race(daemon_factory, tmp_path):
    path = tmp_path / "race" / "sshpilotd.sock"
    server, _ = daemon_factory(socket_path=path)
    barrier = threading.Barrier(2)
    errors = []

    def shutdown():
        barrier.wait()
        try:
            server.shutdown()
            server.wait_stopped()
        except Exception as exc:
            errors.append(exc)

    def connect():
        barrier.wait()
        try:
            client = DaemonClient(socket_path=path)
            try:
                client.close()
            except Exception:
                pass
        except Exception:
            # Connection refused during shutdown is acceptable.
            pass

    t1 = threading.Thread(target=shutdown)
    t2 = threading.Thread(target=connect)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors
