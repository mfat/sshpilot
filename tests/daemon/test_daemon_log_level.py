from __future__ import annotations

import logging

from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.daemon import DaemonLogLevel, SetDaemonLogLevelRequest
from sshpilot.daemon.launcher import DaemonLauncher
from sshpilot.logging_support import close_managed_handlers, configure_daemon_logging


def test_real_client_server_daemon_log_level_control(tmp_path, daemon_factory):
    close_managed_handlers()
    configure_daemon_logging(tmp_path, "info")
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        client.set_daemon_log_level(
            SetDaemonLogLevelRequest(level=DaemonLogLevel.DEBUG)
        )
        assert logging.getLogger().level == logging.DEBUG
        client.set_daemon_log_level(
            SetDaemonLogLevelRequest(level=DaemonLogLevel.WARNING)
        )
        assert logging.getLogger().level == logging.WARNING
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped()
        close_managed_handlers()


def test_daemon_cli_level_precedence_reads_config(tmp_path, monkeypatch):
    from sshpilot import platform_utils
    from sshpilot.daemon import bootstrap_settings, cli

    close_managed_handlers()
    monkeypatch.setattr(platform_utils, "get_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        bootstrap_settings.DaemonBootstrapSettings,
        "get_setting",
        lambda _self, _key, default=None: "warning" if _key == "logging.level" else default,
    )
    try:
        cli._configure_logging(False, False)
        assert logging.getLogger().level == logging.WARNING
        cli._configure_logging(True, False)
        assert logging.getLogger().level == logging.DEBUG
        cli._configure_logging(False, True)
        assert logging.getLogger().level == logging.WARNING
    finally:
        close_managed_handlers()


def test_explicit_launcher_level_overrides_reused_daemon(tmp_path, daemon_factory):
    close_managed_handlers()
    configure_daemon_logging(tmp_path, "info")
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)
    try:
        verbose = DaemonLauncher(
            socket_path=server.socket_path,
            verbose=True,
            popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("reused daemon must not spawn")
            ),
        )
        verbose_result = verbose.connect_or_start()
        assert logging.getLogger().level == logging.DEBUG
        verbose_result.client.close()

        quiet = DaemonLauncher(
            socket_path=server.socket_path,
            quiet=True,
            popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("reused daemon must not spawn")
            ),
        )
        quiet_result = quiet.connect_or_start()
        assert logging.getLogger().level == logging.WARNING
        quiet_result.client.close()
    finally:
        server.shutdown()
        assert server.wait_stopped()
        close_managed_handlers()


def test_reused_daemon_level_sync_failure_is_non_fatal(
    tmp_path, daemon_factory, monkeypatch
):
    close_managed_handlers()
    configure_daemon_logging(tmp_path, "info")
    server, _manager = daemon_factory(idle_shutdown_seconds=0.0)

    def fail_sync(_self, _request):
        raise SshPilotError(ErrorCode.TRANSPORT_TIMEOUT, "diagnostic sync timed out")

    monkeypatch.setattr(DaemonClient, "set_daemon_log_level", fail_sync)
    try:
        launcher = DaemonLauncher(
            socket_path=server.socket_path,
            verbose=True,
            popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("reused daemon must not spawn")
            ),
        )
        result = launcher.connect_or_start()
        assert result.process is None
        assert result.client.get_capabilities().supported
        result.client.close()
    finally:
        server.shutdown()
        assert server.wait_stopped()
        close_managed_handlers()
