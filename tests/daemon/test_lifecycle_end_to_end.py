"""End-to-end: a real daemon, a real teardown, a real next startup.

Everything here uses real subprocesses. The two product contracts are only
meaningful as observable facts about processes on the machine, so mocks would
prove the wrong thing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sshpilot.daemon.launcher import DaemonLauncher
from sshpilot.daemon.lifecycle import probe_socket_owner
from sshpilot.daemon.process_registry import (
    KIND_SESSION,
    OwnedProcessRegistry,
    registry_path_for,
)
from sshpilot.daemon.runtime_verification import (
    terminate_owned_runtime,
    verify_sshpilot_runtime_terminated,
)

pytestmark = pytest.mark.integration


def _require_daemon_subprocess() -> None:
    probe = subprocess.run(
        [sys.executable, "-c", "import gi"], capture_output=True, check=False
    )
    if probe.returncode:
        pytest.skip("production daemon dependencies unavailable to subprocess")


def _isolated_environment(tmp_path: Path) -> dict:
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_RUNTIME_DIR": str(tmp_path / "xdg-runtime"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    for key in (
        "HOME",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
    return environment


def _wait_gone(process: subprocess.Popen, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.05)
    return False


def _sleeper() -> subprocess.Popen:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(120)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def test_quit_leaves_no_sshpilot_process_and_startup_works_again(tmp_path):
    """The whole lifecycle, on real processes.

    Start a daemon, give it a registered child, tear everything down, prove
    nothing owned survives — then prove the next startup still works on the
    same socket.
    """
    _require_daemon_subprocess()
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    environment = _isolated_environment(tmp_path)

    launcher = DaemonLauncher(
        socket_path=socket_path, startup_timeout=20, environment=environment
    )
    launched = launcher.connect_or_start()
    assert launched.process is not None
    daemon_process = launched.process.process

    # A child the daemon owns. Registered exactly as the runtimes do, so the
    # teardown path sees a genuine orphan rather than a fixture.
    child = _sleeper()
    registry = OwnedProcessRegistry(registry_path_for(socket_path))
    registry.register(child.pid, kind=KIND_SESSION, label="integration")

    try:
        assert probe_socket_owner(socket_path)[0] is True
        # Before teardown, verification must refuse.
        before = verify_sshpilot_runtime_terminated(
            socket_path=socket_path, check_control_masters=False
        )
        assert before.terminated is False
    finally:
        launched.client.close()

    # Quit: stop the daemon, then collect whatever it owned.
    daemon_process.terminate()
    assert _wait_gone(daemon_process), "the daemon must exit on SIGTERM"

    result = terminate_owned_runtime(
        socket_path=socket_path, sweep_control_masters=False
    )

    assert result.terminated is True, result.describe()
    assert _wait_gone(child), "the registered child must be reaped"
    assert probe_socket_owner(socket_path) == (False, None)
    assert not socket_path.exists()

    # Required: startup works again immediately after a clean quit.
    relaunched = DaemonLauncher(
        socket_path=socket_path, startup_timeout=20, environment=environment
    ).connect_or_start()
    try:
        assert relaunched.process is not None
        assert relaunched.client.list_connections() == []
    finally:
        relaunched.client.close()
        handle = relaunched.process
        if handle is not None and handle.process.poll() is None:
            handle.process.terminate()
            _wait_gone(handle.process)


def test_startup_recovers_from_previous_build_residue(tmp_path):
    """Required: a squatter and a stale registry must not block the next start.

    Stands in for the state a killed previous build leaves behind: something
    holding the socket that cannot serve this build, plus a registry naming
    a child that outlived its daemon.
    """
    _require_daemon_subprocess()
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    environment = _isolated_environment(tmp_path)

    squatter = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,socket,sys,time\n"
            "l = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "l.bind(sys.argv[1]); os.chmod(sys.argv[1], 0o600); l.listen(8)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "while True:\n"
            "    try:\n"
            "        peer, _a = l.accept(); peer.close()\n"
            "    except OSError:\n"
            "        time.sleep(0.05)\n",
            str(socket_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert squatter.stdout is not None
    assert squatter.stdout.readline().strip() == "ready"

    orphan = _sleeper()
    OwnedProcessRegistry(registry_path_for(socket_path)).register(
        orphan.pid, kind=KIND_SESSION, label="previous-build"
    )

    launched = None
    try:
        launched = DaemonLauncher(
            socket_path=socket_path, startup_timeout=20, environment=environment
        ).connect_or_start()

        assert launched.process is not None, "a fresh daemon must have started"
        assert launched.client.list_connections() == []
        assert _wait_gone(squatter), "the squatter must have been evicted"

        # The previous build's orphan is still tracked and reapable.
        remaining = terminate_owned_runtime(
            socket_path=socket_path, sweep_control_masters=False
        )
        assert _wait_gone(orphan), "the previous build's orphan must be reaped"
        assert any(
            item.kind == "daemon_socket" for item in remaining.survivors
        ), "the daemon we just started is legitimately still running"
    finally:
        if launched is not None:
            launched.client.close()
            handle = launched.process
            if handle is not None and handle.process.poll() is None:
                handle.process.terminate()
                _wait_gone(handle.process)
        if squatter.poll() is None:
            squatter.kill()
        squatter.wait(timeout=5)
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait(timeout=5)
