import os
import subprocess
import time
from dataclasses import replace

import pytest

from sshpilot.api import (
    Capability,
    ErrorCode,
    InProcessClient,
    SshPilotError,
)
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.launcher import (
    DaemonLaunchError,
    DaemonLauncher,
    DaemonStartupFailure,
)


def _stop_owned_daemon(result):
    result.client.close()
    handle = result.process
    if handle is None or handle.process.poll() is not None:
        return
    handle.process.terminate()
    handle.process.wait(timeout=3)
    deadline = time.monotonic() + 2
    while handle.process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)


def test_existing_compatible_daemon_is_reused_without_launch(daemon_factory):
    server, _manager = daemon_factory()

    def _forbidden_popen(*_args, **_kwargs):
        raise AssertionError("an existing daemon must not launch another process")

    launcher = DaemonLauncher(
        socket_path=server.socket_path,
        popen=_forbidden_popen,
    )
    result = launcher.connect_or_start()
    try:
        assert result.process is None
        assert result.client.list_connections()[0].nickname == "demo"
    finally:
        result.client.close()


def test_real_on_demand_process_is_ready_via_handshake_and_owned(tmp_path):
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    launcher = DaemonLauncher(
        socket_path=socket_path,
        startup_timeout=5,
        environment=environment,
    )

    result = launcher.connect_or_start()
    try:
        assert result.process is not None
        assert result.process.started_by_frontend is True
        assert result.process.command[:3] == (
            launcher.executable,
            "-m",
            "sshpilot.daemon",
        )
        assert result.client.get_capabilities().supported == frozenset(
            {
                Capability.CONNECTIONS_READ,
                Capability.CONNECTIONS_EVENTS,
                Capability.CONNECTIONS_WRITE,
                Capability.SESSIONS_READ,
                Capability.SESSIONS_WRITE,
                Capability.SESSIONS_EVENTS,
                Capability.TERMINAL_OUTPUT,
                Capability.TERMINAL_INPUT,
                Capability.TERMINAL_RESIZE,
                Capability.TERMINAL_REPLAY,
                Capability.INTERACTIONS_READ,
                Capability.INTERACTIONS_RESPOND,
                Capability.INTERACTIONS_EVENTS,
                Capability.INTERACTIONS_HOST_KEY,
                Capability.INTERACTIONS_PASSWORD,
                Capability.INTERACTIONS_PASSPHRASE,
                Capability.SFTP_READ,
                Capability.SFTP_WRITE,
                Capability.SFTP_EVENTS,
                Capability.SFTP_METADATA,
                Capability.SFTP_MUTATE,
                Capability.TRANSFERS_READ,
                Capability.TRANSFERS_WRITE,
                Capability.TRANSFERS_EVENTS,
                Capability.TRANSFERS_UPLOAD,
                Capability.TRANSFERS_DOWNLOAD,
                Capability.FORWARDS_READ,
                Capability.FORWARDS_WRITE,
                Capability.FORWARDS_EVENTS,
                Capability.FORWARDS_LOCAL,
                Capability.FORWARDS_REMOTE,
                Capability.FORWARDS_DYNAMIC,
            }
        )
        assert result.client.list_connections() == []

        reused = launcher.connect_or_start()
        try:
            assert reused.process is None
            assert reused.client.list_connections() == []
        finally:
            reused.client.close()
    finally:
        _stop_owned_daemon(result)

    deadline = time.monotonic() + 2
    while socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not socket_path.exists()


def test_process_launch_uses_argv_no_shell_and_sanitized_environment(
    tmp_path,
    monkeypatch,
):
    captured = {}
    client = object()

    class _Process:
        def poll(self):
            return None

    def _popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return _Process()

    launcher = DaemonLauncher(
        socket_path=tmp_path / "runtime" / "sshpilotd.sock",
        environment={
            "PATH": "/usr/bin",
            "BW_SESSION": "must-not-cross",
            "SSHPILOT_KDBX_KEY": "must-not-cross",
        },
        popen=_popen,
    )
    monkeypatch.setattr(launcher, "_wait_until_ready", lambda _process: client)

    result = launcher.connect_or_start()

    assert result.client is client
    assert captured["argv"] == [
        launcher.executable,
        "-m",
        "sshpilot.daemon",
        "--socket",
        str(launcher.socket_path),
    ]
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert captured["env"]["PATH"] == "/usr/bin"
    assert "BW_SESSION" not in captured["env"]
    assert "SSHPILOT_KDBX_KEY" not in captured["env"]
    assert captured["env"]["PYTHONPATH"].endswith("/src")


def test_early_process_exit_is_bounded_and_classified(tmp_path):
    class _ExitedProcess:
        def poll(self):
            return 7

    launcher = DaemonLauncher(
        socket_path=tmp_path / "runtime" / "sshpilotd.sock",
        startup_timeout=0.2,
        popen=lambda *_args, **_kwargs: _ExitedProcess(),
    )

    with pytest.raises(DaemonLaunchError) as caught:
        launcher.connect_or_start()

    assert caught.value.reason is DaemonStartupFailure.PROCESS_EXITED


def test_socket_readiness_timeout_is_bounded_and_stops_exact_child(tmp_path):
    class _HangingProcess:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            del timeout
            return 0

    process = _HangingProcess()
    launcher = DaemonLauncher(
        socket_path=tmp_path / "runtime" / "sshpilotd.sock",
        startup_timeout=0.05,
        poll_interval=0.01,
        popen=lambda *_args, **_kwargs: process,
    )

    with pytest.raises(DaemonLaunchError) as caught:
        launcher.connect_or_start()

    assert caught.value.reason is DaemonStartupFailure.STARTUP_TIMEOUT
    assert process.terminated is True


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (
            ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
            DaemonStartupFailure.INCOMPATIBLE_PROTOCOL,
        ),
        (ErrorCode.PROTOCOL_ERROR, DaemonStartupFailure.HANDSHAKE_FAILED),
    ],
)
def test_existing_incompatible_or_malformed_daemon_is_not_restarted(
    tmp_path,
    monkeypatch,
    code,
    reason,
):
    launches = []
    launcher = DaemonLauncher(
        socket_path=tmp_path / "runtime" / "sshpilotd.sock",
        popen=lambda *_args, **_kwargs: launches.append(True),
    )
    monkeypatch.setattr(
        launcher,
        "_connect",
        lambda _timeout: (_ for _ in ()).throw(
            SshPilotError(code, "safe protocol failure")
        ),
    )

    with pytest.raises(DaemonLaunchError) as caught:
        launcher.connect_or_start()

    assert caught.value.reason is reason
    assert launches == []


@pytest.mark.parametrize(
    "supported",
    [
        frozenset(),
        frozenset({Capability.CONNECTIONS_READ}),
        frozenset({Capability.CONNECTIONS_EVENTS}),
        frozenset(
            {
                Capability.CONNECTIONS_READ,
                Capability.CONNECTIONS_EVENTS,
            }
        ),
    ],
)
def test_daemon_without_required_gtk_connection_capabilities_is_rejected(
    tmp_path,
    supported,
):
    class _NoCapabilityCore:
        def __init__(self):
            manager = type(
                "_Manager",
                (),
                {"get_connections": lambda self: []},
            )()
            self._base = InProcessClient(manager)

        def get_capabilities(self):
            return replace(
                self._base.get_capabilities(),
                supported=supported,
            )

        def close(self):
            self._base.close()

    socket_dir = tmp_path / "no-capability"
    socket_dir.mkdir(mode=0o700)
    socket_path = socket_dir / "sshpilotd.sock"
    server = DaemonServer(_NoCapabilityCore, socket_path=socket_path)
    server.start_in_thread()
    try:
        launcher = DaemonLauncher(socket_path=socket_path)
        with pytest.raises(DaemonLaunchError) as caught:
            launcher.connect_or_start()
        assert caught.value.reason is DaemonStartupFailure.MISSING_CAPABILITY
    finally:
        server.shutdown()
        assert server.wait_stopped()


def test_unsafe_socket_target_is_refused_before_process_launch(tmp_path):
    directory = tmp_path / "runtime"
    directory.mkdir(mode=0o700)
    socket_path = directory / "sshpilotd.sock"
    socket_path.write_text("not a socket", encoding="utf-8")
    launches = []
    launcher = DaemonLauncher(
        socket_path=socket_path,
        popen=lambda *_args, **_kwargs: launches.append(True),
    )

    with pytest.raises(DaemonLaunchError) as caught:
        launcher.connect_or_start()

    assert caught.value.reason is DaemonStartupFailure.UNSAFE_SOCKET
    assert launches == []
    assert socket_path.read_text(encoding="utf-8") == "not a socket"
