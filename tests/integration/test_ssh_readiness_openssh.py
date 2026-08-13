"""End-to-end OpenSSH readiness: real ssh diagnostics through the manager.

These tests run a real local OpenSSH client against the disposable container
sshd and prove the daemon's readiness instrumentation (private ``-v -E`` file,
inotify delivery, parser verdicts) works against real OpenSSH output:

* a successful plain-key login yields AUTHENTICATED;
* an unreachable target yields a trusted FAILED detail;
* a ControlMaster client yields MUX_SESSION_OPENED.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from sshpilot.core.ssh_diagnostics import SshDiagnosticState
from sshpilot.daemon.ssh_readiness import (
    SshReadinessManager,
    insert_ssh_diagnostics_options,
)
from tests.fixtures.temporary_openssh import require_temporary_openssh


pytestmark = pytest.mark.integration


@pytest.fixture
def openssh(tmp_path):
    env = require_temporary_openssh(tmp_path)
    yield env
    env.destroy()


@pytest.fixture
def manager(tmp_path):
    readiness = SshReadinessManager(
        instance_id="daemon-test",
        runtime_dir=str(tmp_path),
    )
    if not readiness.available:
        pytest.skip("readiness diagnostics unavailable in this environment")
    yield readiness
    readiness.close()


def _ssh_env(home: str) -> dict:
    env = os.environ.copy()
    env["HOME"] = home
    env["SSH_AUTH_SOCK"] = ""
    return env


def _launch(
    manager: SshReadinessManager,
    session_id: str,
    argv: tuple[str, ...],
    env: dict,
) -> tuple[str, ...]:
    diagnostics_path = manager.prepare_launch(session_id, argv, env)
    assert diagnostics_path is not None, "readiness did not arm the launch"
    return tuple(insert_ssh_diagnostics_options(argv, diagnostics_path))


def test_direct_plain_key_login_is_authenticated(openssh, manager):
    openssh.write_client_config("key_plain")
    results: list = []
    manager.subscribe("session-1", lambda sid, result: results.append(result))

    argv = (
        "ssh",
        "-F",
        str(openssh.ssh_config),
        openssh.host_alias_key_plain,
    )
    argv = _launch(manager, "session-1", argv, _ssh_env(str(openssh.client_home)))
    proc = subprocess.run(
        (*argv, "true"),
        capture_output=True,
        text=True,
        timeout=30,
        env=_ssh_env(str(openssh.client_home)),
    )
    assert proc.returncode == 0, proc.stderr
    deadline = time.monotonic() + 10
    while not results and time.monotonic() < deadline:
        time.sleep(0.05)
    assert results, "no decisive diagnostics verdict delivered"
    assert results[0].state is SshDiagnosticState.AUTHENTICATED
    assert "Authenticated to" in results[0].detail


def test_unreachable_target_is_trusted_failure(openssh, manager):
    results: list = []
    manager.subscribe("session-fail", lambda sid, result: results.append(result))

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    dead_port = listener.getsockname()[1]
    listener.close()

    config = openssh.client_home / "dead-host-config"
    config.write_text(
        "\n".join(
            (
                "Host dead",
                "    HostName 127.0.0.1",
                f"    Port {dead_port}",
                f"    User {openssh.username}",
                "    BatchMode yes",
                "    ConnectTimeout 5",
                f"    UserKnownHostsFile {openssh.known_hosts}",
                "    GlobalKnownHostsFile /dev/null",
                "    StrictHostKeyChecking no",
                "    LogLevel ERROR",
            )
        )
        + "\n"
    )
    config.chmod(0o600)
    argv = ("ssh", "-F", str(config), "dead")
    argv = _launch(manager, "session-fail", argv, _ssh_env(str(openssh.client_home)))
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        env=_ssh_env(str(openssh.client_home)),
    )
    assert proc.returncode != 0
    deadline = time.monotonic() + 10
    while not results and time.monotonic() < deadline:
        time.sleep(0.05)
    assert results, "no failure verdict delivered"
    assert results[0].state is SshDiagnosticState.FAILED
    assert "refused" in results[0].detail.lower()


def test_control_master_client_reports_mux_session(openssh, manager):
    openssh.write_client_config("key_plain")
    ctl = Path(f"/tmp/sshpilot-ctl-{os.getpid()}.sock")
    ctl.unlink(missing_ok=True)
    results: list = []
    manager.subscribe("session-mux", lambda sid, result: results.append(result))

    master = subprocess.Popen(
        (
            "ssh",
            "-F",
            str(openssh.ssh_config),
            "-M",
            "-S",
            str(ctl),
            "-N",
            openssh.host_alias_key_plain,
        ),
        env=_ssh_env(str(openssh.client_home)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ctl.exists() and time.monotonic() < deadline:
            if master.poll() is not None:
                break
            time.sleep(0.1)
        if not ctl.exists():
            stderr = master.stderr.read() if master.poll() is not None else ""
            pytest.fail(f"control socket not created: {stderr!r}")

        argv = (
            "ssh",
            "-F",
            str(openssh.ssh_config),
            "-S",
            str(ctl),
            openssh.host_alias_key_plain,
            "true",
        )
        argv = _launch(manager, "session-mux", argv, _ssh_env(str(openssh.client_home)))
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            env=_ssh_env(str(openssh.client_home)),
        )
        assert proc.returncode == 0, proc.stderr
        deadline = time.monotonic() + 10
        while not results and time.monotonic() < deadline:
            time.sleep(0.05)
        assert results, "no mux verdict delivered"
        assert results[0].state is SshDiagnosticState.MUX_SESSION_OPENED
        assert "master session id" in results[0].detail
    finally:
        master.terminate()
        try:
            master.wait(timeout=5)
        except subprocess.TimeoutExpired:
            master.kill()
        ctl.unlink(missing_ok=True)