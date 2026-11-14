"""When the app is killed, nothing the daemon started may keep running.

A real daemon process runs in a throwaway sandbox. A separate "app" process
(handshake ``frontend_type="gtk"``) opens an SSH terminal session and a local
port forward against a TCP listener that accepts but never sends a banner,
so each ``ssh`` stays alive waiting. The app is then killed outright. The
daemon must exit, every ``ssh`` it started must be gone, and its durable
process registry must list nothing still alive.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

psutil = pytest.importorskip("psutil")

REPO_SRC = Path(__file__).resolve().parents[2] / "src"

pytestmark = pytest.mark.skipif(
    shutil.which("ssh") is None, reason="needs an OpenSSH client"
)

_APP = textwrap.dedent(
    """
    import sys, time
    from sshpilot.api.daemon_client import DaemonClient
    from sshpilot.api.models.operations import ForwardType, OpenForwardRequest
    from sshpilot.api.models.sessions import OpenSessionRequest
    endpoint, forward_port = sys.argv[1], int(sys.argv[2])
    client = DaemonClient(socket_path=endpoint, timeout=20,
                          client_name="sshpilot-gtk", frontend_type="gtk")
    conn = [c for c in client.list_connections() if c.nickname == "probe"][0]
    client.open_session(OpenSessionRequest(connection_id=conn.id))
    client.open_forward(OpenForwardRequest(
        connection_id=conn.id, type=ForwardType.LOCAL,
        bind_host="127.0.0.1", bind_port=forward_port,
        destination_host="127.0.0.1", destination_port=9,
    ))
    print("READY", flush=True)
    time.sleep(600)
    """
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _silent_listener():
    """Accepts TCP and never answers: ssh waits for a banner indefinitely."""

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    held = []

    def accept():
        while True:
            try:
                held.append(listener.accept())
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    return listener, held


def _sandbox(root: Path, port: int) -> dict:
    env = dict(os.environ)
    for key in (
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
        "APPDATA", "LOCALAPPDATA",
    ):
        path = root / key.lower()
        path.mkdir()
        env[key] = str(path)
    if sys.platform != "win32":
        runtime = Path(tempfile.mkdtemp(prefix="sp-rt-"))
        runtime.chmod(0o700)
        env["XDG_RUNTIME_DIR"] = str(runtime)
    ssh_dir = root / "ssh"
    ssh_dir.mkdir(mode=0o700)
    null = "NUL" if sys.platform == "win32" else "/dev/null"
    config = ssh_dir / "config"
    config.write_text(
        f"Host probe\n  HostName 127.0.0.1\n  Port {port}\n  User nobody\n"
        f"  UserKnownHostsFile {null}\n  GlobalKnownHostsFile {null}\n"
    )
    if sys.platform != "win32":
        config.chmod(0o600)
    env["SSHPILOT_SSH_DIR"] = str(ssh_dir)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_SRC), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env.pop("SSH_AUTH_SOCK", None)
    env.pop("SSHPILOT_DAEMON_SERVICE_MODE", None)
    return env


def _owned_ssh(root_pid: int):
    """Long-lived ssh children; short ``ssh -G`` config probes are skipped."""

    try:
        tree = psutil.Process(root_pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []
    found = []
    for proc in tree:
        try:
            if proc.name().lower() not in ("ssh", "ssh.exe"):
                continue
            if "-G" in proc.cmdline():
                continue
        except psutil.Error:
            continue  # exited while we looked
        found.append(proc)
    return found


def _alive(proc) -> bool:
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_killing_the_app_ends_every_daemon_child(tmp_path):
    from sshpilot.daemon.process_registry import (
        OwnedProcessRegistry,
        registry_path_for,
    )

    listener, _held = _silent_listener()
    env = _sandbox(tmp_path, listener.getsockname()[1])
    endpoint = tmp_path / "daemon.sock"
    flags = {}
    if sys.platform == "win32":
        flags["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    # Launched as daemon/launcher.py does: no stdin, detached.
    daemon = subprocess.Popen(
        [sys.executable, "-m", "sshpilot.daemon", "--socket", str(endpoint)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **flags,
    )
    app = None
    owned = []
    try:
        deadline = time.monotonic() + 30
        while not endpoint.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert endpoint.exists(), "daemon did not start"

        app = subprocess.Popen(
            [sys.executable, "-c", _APP, str(endpoint), str(_free_port())],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert app.stdout.readline().strip() == "READY"

        # Both the terminal ssh and the forward ssh are live and registered.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            owned = [p for p in _owned_ssh(daemon.pid) if _alive(p)]
            if len(owned) >= 2:
                break
            time.sleep(0.2)
        assert len(owned) >= 2, f"expected session + forward ssh, got {owned}"
        # Registration follows the spawn; give it a moment to land on disk.
        registry = OwnedProcessRegistry(registry_path_for(endpoint))
        wanted = {p.pid for p in owned}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            registered = {entry.pid for entry in registry.live_entries()}
            if wanted <= registered:
                break
            time.sleep(0.2)
        assert wanted <= registered

        app.kill()  # SIGKILL / TerminateProcess: the app never says goodbye
        app.wait(10)

        daemon.wait(20)
        deadline = time.monotonic() + 10
        while any(_alive(p) for p in owned) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert [p.pid for p in owned if _alive(p)] == []
        assert registry.live_entries() == ()
    finally:
        if app is not None and app.poll() is None:
            app.kill()
        if daemon.poll() is None:
            daemon.kill()
        for proc in owned:  # only our own sandboxed children, by PID
            if _alive(proc):
                proc.kill()
        listener.close()
