"""Startup failure paths: cleanup, hook chaining, and logging fallbacks."""

from __future__ import annotations

import logging
import sys
import threading
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")

from sshpilot import main as sshpilot_main  # noqa: E402
from sshpilot.logging_support import close_managed_handlers  # noqa: E402


# -- main(): the early daemon client is discarded however run() ends ---------


def _stub_main_startup(monkeypatch, discarded):
    from sshpilot import daemon_prelaunch, startup_preload

    monkeypatch.setattr(sys, "argv", ["sshpilot"])
    monkeypatch.setattr(daemon_prelaunch, "start", lambda **_kwargs: None)
    monkeypatch.setattr(daemon_prelaunch, "discard", lambda: discarded.append(True))
    monkeypatch.setattr(startup_preload, "start", lambda: None)


def test_main_discards_early_client_when_application_construction_fails(monkeypatch):
    discarded = []
    _stub_main_startup(monkeypatch, discarded)

    def _broken_application(**_kwargs):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(sshpilot_main, "SshPilotApplication", _broken_application)
    with pytest.raises(RuntimeError):
        sshpilot_main.main()
    assert discarded == [True]


def test_main_discards_early_client_when_run_raises(monkeypatch):
    discarded = []
    _stub_main_startup(monkeypatch, discarded)

    class _App:
        def __init__(self, **_kwargs):
            pass

        def run(self, _argv):
            raise RuntimeError("run failed")

    monkeypatch.setattr(sshpilot_main, "SshPilotApplication", _App)
    with pytest.raises(RuntimeError):
        sshpilot_main.main()
    assert discarded == [True]


def test_main_discards_early_client_and_returns_status(monkeypatch):
    discarded = []
    _stub_main_startup(monkeypatch, discarded)

    class _App:
        def __init__(self, **_kwargs):
            pass

        def run(self, _argv):
            return 3

    monkeypatch.setattr(sshpilot_main, "SshPilotApplication", _App)
    assert sshpilot_main.main() == 3
    assert discarded == [True]


# -- worker exception hook chains to the hook it replaced --------------------


@pytest.fixture
def fresh_exception_hooks(monkeypatch):
    monkeypatch.setattr(sshpilot_main, "_exception_hooks_installed", False)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(sys, "unraisablehook", sys.unraisablehook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)


def _thread_hook_args(exc_type):
    exc = exc_type("boom")
    return SimpleNamespace(
        exc_type=exc_type,
        exc_value=exc,
        exc_traceback=None,
        thread=SimpleNamespace(name="worker"),
    )


def test_thread_excepthook_logs_and_chains_previous_hook(fresh_exception_hooks, caplog):
    seen = []
    threading.excepthook = seen.append
    sshpilot_main._install_exception_hooks()

    args = _thread_hook_args(ValueError)
    with caplog.at_level(logging.CRITICAL, logger="sshpilot"):
        threading.excepthook(args)

    assert seen == [args]
    assert any("Unhandled exception in thread" in r.getMessage() for r in caplog.records)


def test_thread_excepthook_chains_system_exit_without_logging(fresh_exception_hooks, caplog):
    seen = []
    threading.excepthook = seen.append
    sshpilot_main._install_exception_hooks()

    args = _thread_hook_args(SystemExit)
    with caplog.at_level(logging.CRITICAL, logger="sshpilot"):
        threading.excepthook(args)

    # The previous hook decides what SystemExit means (the default ignores it).
    assert seen == [args]
    assert not any("Unhandled exception" in r.getMessage() for r in caplog.records)


def test_thread_excepthook_survives_failing_previous_hook(fresh_exception_hooks):
    def _broken(_args):
        raise RuntimeError("previous hook failed")

    threading.excepthook = _broken
    sshpilot_main._install_exception_hooks()
    threading.excepthook(_thread_hook_args(ValueError))  # must not raise


# -- daemon logging bootstrap falls back to stderr ---------------------------


@pytest.fixture
def clean_root_logging():
    root = logging.getLogger()
    level = root.level
    close_managed_handlers()
    try:
        yield root
    finally:
        close_managed_handlers()
        root.setLevel(level)


def _managed_roles(root):
    return sorted(
        getattr(h, "_sshpilot_log_role", "")
        for h in root.handlers
        if getattr(h, "_sshpilot_managed", False)
    )


def test_daemon_logging_falls_back_to_stderr_when_state_dir_fails(
    monkeypatch, clean_root_logging, caplog
):
    from sshpilot import platform_utils
    from sshpilot.daemon import cli

    def _no_state_dir():
        raise OSError("state dir unavailable")

    monkeypatch.setattr(platform_utils, "get_state_dir", _no_state_dir)
    with caplog.at_level(logging.WARNING):
        cli._configure_logging(False, False)

    assert _managed_roles(clean_root_logging) == ["daemon.console"]
    assert any(
        "file logging unavailable" in r.getMessage() for r in caplog.records
    )


def test_daemon_logging_falls_back_to_stderr_when_file_handler_fails(
    monkeypatch, tmp_path, clean_root_logging
):
    from sshpilot import logging_support, platform_utils
    from sshpilot.daemon import cli

    monkeypatch.setattr(platform_utils, "get_state_dir", lambda: str(tmp_path))

    def _broken_file_handler(*_args, **_kwargs):
        raise PermissionError("read-only log dir")

    monkeypatch.setattr(logging_support, "_add_file_handler", _broken_file_handler)
    cli._configure_logging(True, False)

    assert _managed_roles(clean_root_logging) == ["daemon.console"]
    assert clean_root_logging.level == logging.DEBUG


def test_daemon_logging_keeps_file_logging_when_settings_fail(
    monkeypatch, tmp_path, clean_root_logging, caplog
):
    from sshpilot import platform_utils
    from sshpilot.daemon import bootstrap_settings, cli

    monkeypatch.setattr(platform_utils, "get_state_dir", lambda: str(tmp_path))

    def _broken_settings(*_args, **_kwargs):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(bootstrap_settings, "DaemonBootstrapSettings", _broken_settings)
    caplog.set_level(logging.WARNING, logger="sshpilot.daemon.cli")
    cli._configure_logging(False, False)

    assert _managed_roles(clean_root_logging) == ["daemon.console", "daemon.file"]
    assert clean_root_logging.level == logging.INFO
    assert any("log level setting unavailable" in r.getMessage() for r in caplog.records)
