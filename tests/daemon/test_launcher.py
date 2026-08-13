import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api import (
    Capability,
    ErrorCode,
    SshPilotError,
)
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.launcher import (
    DaemonLaunchError,
    DaemonLauncher,
    DaemonStartupFailure,
)


def _stop_owned_daemon(result, *, socket_path=None):
    result.client.close()
    handle = result.process
    if handle is not None and handle.process.poll() is None:
        handle.process.terminate()
        try:
            handle.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            handle.process.wait(timeout=3)
        deadline = time.monotonic() + 2
        while handle.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
    if socket_path is not None:
        deadline = time.monotonic() + 5
        while Path(socket_path).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        # Last resort: unlink a leftover socket after the process is gone.
        if Path(socket_path).exists() and (handle is None or handle.process.poll() is not None):
            try:
                Path(socket_path).unlink()
            except OSError:
                pass


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


def test_launcher_keeps_request_timeout_separate_from_probe(daemon_factory):
    """Probe budget is for connect only; RPCs must keep DEFAULT_REQUEST_TIMEOUT.

    Regression: wiring probe_timeout (0.25s) into DaemonClient.timeout made
    sftp.list (and other slow methods) report false transport_timeout.
    """
    from sshpilot.api.daemon_client import DEFAULT_REQUEST_TIMEOUT

    server, _manager = daemon_factory()

    launcher = DaemonLauncher(
        socket_path=server.socket_path,
        probe_timeout=0.25,
        popen=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must reuse existing daemon")
        ),
    )
    result = launcher.connect_or_start()
    try:
        assert result.client._timeout == DEFAULT_REQUEST_TIMEOUT
        assert result.client._connect_timeout == 0.25
        assert result.client.list_connections()[0].nickname == "demo"
    finally:
        result.client.close()


def test_real_on_demand_process_is_ready_via_handshake_and_owned(tmp_path):
    probe = subprocess.run(
        [sys.executable, "-c", "import gi"],
        capture_output=True,
        check=False,
    )
    if probe.returncode:
        pytest.skip("production daemon dependencies unavailable to subprocess")
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
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
    for key in ("XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "HOME"):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
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
                Capability.CONNECTIONS_CONFIG_READ,
                Capability.CONNECTIONS_CONFIG_WRITE,
                Capability.CONNECTIONS_SECRETS_WRITE,
                Capability.CONNECTIONS_SECRETS_STATUS_READ,
                Capability.CONNECTIONS_SECRETS_REVEAL,
                Capability.CONNECTIONS_METADATA_WRITE,
                Capability.CONNECTIONS_GROUPS,
                Capability.CONNECTIONS_SPLIT,
                Capability.SESSIONS_READ,
                Capability.SESSIONS_WRITE,
                Capability.SESSIONS_COMMAND,
                Capability.SESSIONS_EVENTS,
                Capability.TERMINAL_OUTPUT,
                Capability.TERMINAL_INPUT,
                Capability.TERMINAL_RESIZE,
                Capability.TERMINAL_REPLAY,
                    Capability.BROADCAST_READ,
                    Capability.BROADCAST_WRITE,
                    Capability.BROADCAST_EVENTS,
                    Capability.PLUGIN_SETTINGS_READ,
                    Capability.PLUGIN_SETTINGS_WRITE,
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
                Capability.SFTP_PRIVILEGED_FILE,
                Capability.OPERATIONS_READ,
                Capability.OPERATIONS_CONTROL,
                Capability.TRANSFERS_READ,
                Capability.TRANSFERS_WRITE,
                Capability.TRANSFERS_EVENTS,
                Capability.TRANSFERS_UPLOAD,
                Capability.TRANSFERS_DOWNLOAD,
                Capability.TRANSFERS_SCP,
                Capability.FORWARDS_READ,
                Capability.FORWARDS_WRITE,
                Capability.FORWARDS_EVENTS,
                Capability.FORWARDS_LOCAL,
                Capability.FORWARDS_REMOTE,
                Capability.FORWARDS_DYNAMIC,
                Capability.KNOWN_HOSTS_READ,
                Capability.KNOWN_HOSTS_WRITE,
                Capability.KEYS_READ,
                Capability.KEYS_WRITE,
                Capability.SSH_OVERRIDES_READ,
                Capability.SSH_OVERRIDES_WRITE,
                Capability.SECRETS_READ,
                Capability.SECRETS_WRITE,
                Capability.SECRETS_OPERATE,
                Capability.SECRETS_TRANSFER,
                Capability.IDENTITY_READ,
                Capability.IDENTITY_WRITE,
                Capability.IDENTITY_OPERATE,
                Capability.DAEMON_STATUS,
                Capability.DAEMON_CONTROL,
                Capability.DAEMON_EVENTS,
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
        _stop_owned_daemon(result, socket_path=socket_path)

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


def test_frozen_process_launch_uses_bundle_dispatch_and_resets_bootloader(
    tmp_path,
    monkeypatch,
):
    captured = {}
    client = object()
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    class _Process:
        def poll(self):
            return None

    def _popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return _Process()

    launcher = DaemonLauncher(
        socket_path=tmp_path / "runtime" / "sshpilotd.sock",
        environment={"PATH": "/usr/bin"},
        popen=_popen,
    )
    monkeypatch.setattr(launcher, "_wait_until_ready", lambda _process: client)

    result = launcher.connect_or_start()

    assert result.client is client
    assert captured["argv"] == [
        launcher.executable,
        "--daemon",
        "--socket",
        str(launcher.socket_path),
    ]
    assert captured["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_verbose_launch_passes_flag_and_starts_askpass_forwarder(tmp_path, monkeypatch):
    class _Process:
        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            return 0

    client = object()
    captured = {}
    forwarder_calls = []

    def _popen(argv, **kwargs):
        captured["argv"] = argv
        return _Process()

    monkeypatch.setattr(
        "sshpilot.askpass_utils.ensure_askpass_log_forwarder",
        lambda: forwarder_calls.append(True),
    )
    monkeypatch.setattr(
        "sshpilot.logging_support.ensure_daemon_log_forwarder",
        lambda *_args, **_kwargs: None,
    )
    launcher = DaemonLauncher(
        socket_path=tmp_path / "runtime" / "sshpilotd.sock",
        environment={"PATH": "/usr/bin"},
        popen=_popen,
        verbose=True,
    )
    monkeypatch.setattr(launcher, "_wait_until_ready", lambda _process: client)

    result = launcher.connect_or_start()

    assert result.client is client
    assert captured["argv"][-1] == "--verbose"
    assert forwarder_calls == [True]


def test_verbose_failed_start_forwards_startup_daemon_log(tmp_path, monkeypatch):
    from sshpilot import platform_utils
    from sshpilot.logging_support import (
        close_managed_handlers,
        configure_frontend_logging,
        stop_daemon_log_forwarder,
    )

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(platform_utils, "get_state_dir", lambda: str(state))
    close_managed_handlers()
    configure_frontend_logging(state, "debug")

    class _Process:
        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            return 1

    def _popen(_argv, **_kwargs):
        (state / "daemon.log").write_text(
            "2026-08-09 12:00:04 - sshpilot.daemon.startup - ERROR - handshake failed\n"
        )
        return _Process()

    launcher = DaemonLauncher(
        socket_path=tmp_path / "runtime" / "sshpilotd.sock",
        startup_timeout=0.2,
        poll_interval=0.02,
        verbose=True,
        popen=_popen,
    )
    monkeypatch.setattr(
        launcher,
        "_wait_until_ready",
        lambda _process: (_ for _ in ()).throw(
            DaemonLaunchError(DaemonStartupFailure.HANDSHAKE_FAILED)
        ),
    )
    try:
        with pytest.raises(DaemonLaunchError):
            launcher.connect_or_start()
        master = state / "sshpilot.log"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and "handshake failed" not in master.read_text():
            time.sleep(0.02)
        assert "handshake failed" in master.read_text()
    finally:
        stop_daemon_log_forwarder()
        close_managed_handlers()


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


def test_verbose_launcher_uses_shared_daemon_log_forwarder(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "sshpilot.platform_utils.get_state_dir",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(
        "sshpilot.logging_support.ensure_daemon_log_forwarder",
        lambda path, *, enabled: calls.append((Path(path), enabled)),
    )
    launcher = DaemonLauncher(verbose=True)
    launcher._ensure_daemon_log_forwarder()
    assert calls == [(tmp_path / "daemon.log", True)]


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
            from tests.helpers.fake_connection_repository import make_test_repository

            self._base = ConnectionApplicationService(make_test_repository())

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
