"""The daemon stops when the app that uses it crashes or is killed.

Quit stops the daemon explicitly; these pin the crash path: only the app's
process ending (not a dropped connection, not a CLI client leaving) shuts
the daemon down, and it does so through the same forced stop quit uses.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from sshpilot.daemon.app_watch import (
    AppProcessWatcher,
    ProcessIdentity,
    identify_peer_process,
    is_app_frontend,
    process_alive,
)

REPO_SRC = Path(__file__).resolve().parents[2] / "src"


class _Fake:
    def __init__(self):
        self.alive = {}
        self.fired = []

    def watcher(self):
        return AppProcessWatcher(
            self.fired.append,
            poll_seconds=60,
            alive=lambda identity: self.alive.get(identity, True),
        )


def test_only_app_frontends_are_watched():
    assert is_app_frontend("gtk")
    for other in ("cli", "diagnostics", "mcp", None, ""):
        assert not is_app_frontend(other)


def test_fires_once_every_app_process_is_gone():
    fake = _Fake()
    watcher = fake.watcher()
    first, second = ProcessIdentity(100, 1.0), ProcessIdentity(200, 2.0)
    watcher.register(first)
    watcher.register(second)
    try:
        fake.alive[first] = False
        assert watcher.check() is False
        assert fake.fired == []
        fake.alive[second] = False
        assert watcher.check() is True
        assert fake.fired == [(second,)]
        assert watcher.check() is False  # fires once
    finally:
        watcher.stop()


def test_nothing_registered_never_fires():
    fake = _Fake()
    watcher = fake.watcher()
    assert watcher.check() is False
    assert fake.fired == []


def test_a_recycled_pid_is_a_different_process():
    import psutil

    me = psutil.Process()
    assert process_alive(ProcessIdentity(me.pid, me.create_time()))
    assert not process_alive(ProcessIdentity(me.pid, me.create_time() - 100))


def test_in_process_clients_are_not_watched():
    # AF_UNIX on POSIX, loopback TCP on Windows: the daemon's own transport.
    left, right = socket.socketpair()
    try:
        assert identify_peer_process(left) is None
    finally:
        left.close()
        right.close()


_TRANSPORTS = ["tcp"] + (["unix"] if hasattr(socket, "AF_UNIX") else [])


@pytest.mark.parametrize("transport", _TRANSPORTS)
def test_peer_process_is_identified_across_processes(tmp_path, transport):
    if transport == "unix":
        address = str(tmp_path / "s.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(address)
        connect = f"s=socket.socket(socket.AF_UNIX);s.connect({address!r})"
    else:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        connect = f"s=socket.create_connection(('127.0.0.1', {port}))"
    listener.listen(1)
    child = subprocess.Popen(
        [sys.executable, "-c", f"import socket,time;{connect};time.sleep(30)"]
    )
    try:
        import psutil

        conn, _ = listener.accept()
        identity = identify_peer_process(conn)
        # A Windows venv python.exe is a launcher; the socket's owner is
        # then its child, which dies with it.
        launched = {child.pid} | {
            p.pid for p in psutil.Process(child.pid).children(recursive=True)
        }
        assert identity is not None and identity.pid in launched
        assert process_alive(identity)
        child.kill()
        child.wait()
        deadline = time.monotonic() + 5
        while process_alive(identity) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process_alive(identity)
        conn.close()
    finally:
        if child.poll() is None:
            child.kill()
        listener.close()


# -- real daemon --------------------------------------------------------------

_CLIENT = textwrap.dedent(
    """
    import sys, time
    from sshpilot.api.daemon_client import DaemonClient
    socket_path, frontend, mode = sys.argv[1:4]
    def connect():
        return DaemonClient(socket_path=socket_path, timeout=5,
                            client_name="probe-" + frontend, frontend_type=frontend)
    client = connect()
    client.list_sessions()
    print("READY", flush=True)
    if mode == "reconnect":
        client.close()
        time.sleep(1.5)
        client = connect()
        client.list_sessions()
        print("RECONNECTED", flush=True)
    if mode == "leave":
        client.close()
        sys.exit(0)
    time.sleep(60)
    """
)


@pytest.fixture
def daemon(tmp_path):
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )
    from sshpilot.daemon import DaemonServer
    from tests.daemon.conftest import TestConnectionManager

    manager = TestConnectionManager()
    service = ConnectionApplicationService(
        manager, secret_provider=manager, client_name="test-daemon"
    )
    server = DaemonServer(
        lambda: service, socket_path=tmp_path / "daemon" / "sshpilotd.sock"
    )
    server.start_in_thread()
    yield server
    server.shutdown()
    server.wait_stopped(timeout=10)


def _spawn(server, frontend, mode):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_SRC), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    child = subprocess.Popen(
        [sys.executable, "-c", _CLIENT, str(server.socket_path), frontend, mode],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout.readline().strip() == "READY"
    return child


def test_killing_the_app_stops_the_daemon(daemon):
    app = _spawn(daemon, "gtk", "stay")
    try:
        time.sleep(0.5)
        assert not daemon.stopped
        app.kill()  # SIGKILL / TerminateProcess: no clean shutdown
        app.wait()
        assert daemon.wait_stopped(timeout=10)
    finally:
        if app.poll() is None:
            app.kill()


def test_app_reconnecting_does_not_stop_the_daemon(daemon):
    app = _spawn(daemon, "gtk", "reconnect")
    try:
        assert app.stdout.readline().strip() == "RECONNECTED"
        time.sleep(2.5)
        assert not daemon.stopped
    finally:
        app.kill()
        app.wait()
    assert daemon.wait_stopped(timeout=10)


def test_a_cli_client_leaving_does_not_stop_the_daemon(daemon):
    cli = _spawn(daemon, "cli", "leave")
    cli.wait(timeout=10)
    time.sleep(2.5)
    assert not daemon.stopped


def test_disabled_watcher_never_fires():
    fake = _Fake()
    watcher = fake.watcher()
    only = ProcessIdentity(100, 1.0)
    watcher.register(only)
    watcher.disable("an app client's process could not be identified")
    fake.alive[only] = False
    try:
        assert watcher.check() is False
        assert fake.fired == []
        watcher.register(ProcessIdentity(300, 3.0))  # ignored once disabled
        assert watcher.check() is False
    finally:
        watcher.stop()


def test_unidentified_app_falls_back_to_idle_lifetime(daemon, monkeypatch):
    """App A is identified, app B is not; A exiting must not stop the daemon.

    The daemon cannot see B's process, so "every watched app exited" no
    longer means the app is gone: it falls back to its idle lifetime.
    """
    from sshpilot.daemon import app_watch

    real = app_watch.identify_peer_process
    calls = []

    def first_only(sock):
        calls.append(sock)
        return real(sock) if len(calls) == 1 else None

    monkeypatch.setattr(app_watch, "identify_peer_process", first_only)
    app_a = _spawn(daemon, "gtk", "stay")
    app_b = _spawn(daemon, "gtk", "stay")
    try:
        time.sleep(0.5)
        app_a.kill()
        app_a.wait()
        time.sleep(3)
        assert not daemon.stopped
    finally:
        for app in (app_a, app_b):
            if app.poll() is None:
                app.kill()
                app.wait()
