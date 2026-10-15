"""Early daemon start (``sshpilot.daemon_prelaunch``) and its hand-over."""

from __future__ import annotations

import threading

import pytest

from sshpilot import daemon_prelaunch
from sshpilot.api.client_factory import select_client


class _Result:
    def __init__(self, name: str) -> None:
        self.client = _Client(name)
        self.process = None


class _Client:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeLauncher:
    """Stands in for ``DaemonLauncher`` inside the prelaunch worker."""

    early_outcome: object = None
    instances: list = []

    def __init__(self, *, verbose=False, quiet=False) -> None:
        self.verbose = verbose
        self.quiet = quiet
        self.calls: list[str] = []
        _FakeLauncher.instances.append(self)

    def connect_or_start_without_eviction(self):
        self.calls.append("early")
        outcome = type(self).early_outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def connect_or_start(self):
        self.calls.append("full")
        return _Result("full")


@pytest.fixture(autouse=True)
def _fresh_prelaunch(monkeypatch):
    monkeypatch.setattr(daemon_prelaunch, "_state", None)
    monkeypatch.delenv(daemon_prelaunch.DISABLE_ENV, raising=False)
    monkeypatch.delenv("SSHPILOT_DAEMON_VERBOSE", raising=False)
    monkeypatch.delenv("SSHPILOT_DAEMON_QUIET", raising=False)
    monkeypatch.setattr("sshpilot.daemon.launcher.DaemonLauncher", _FakeLauncher)
    monkeypatch.setattr(daemon_prelaunch, "_export_language", lambda: None)
    _FakeLauncher.instances = []
    _FakeLauncher.early_outcome = _Result("early")
    yield
    daemon_prelaunch._state = None


def _started_launcher():
    assert daemon_prelaunch._state is not None
    daemon_prelaunch._state.done.wait(5)
    return daemon_prelaunch.launcher()


def test_first_selection_takes_the_early_client_once():
    assert daemon_prelaunch.start(verbose=True)
    launcher = _started_launcher()

    first = select_client()
    assert first.client.name == "early"
    # Later selections (retry, reconnect) launch normally with the same launcher.
    second = select_client(launcher=launcher)
    assert second.client.name == "full"
    assert launcher.calls == ["early", "full"]
    assert launcher.verbose is True


def test_start_is_idempotent():
    assert daemon_prelaunch.start()
    assert daemon_prelaunch.start(verbose=True)
    _started_launcher()
    assert len(_FakeLauncher.instances) == 1


def test_early_failure_is_raised_by_the_selection_that_takes_it():
    error = RuntimeError("startup_timeout")
    _FakeLauncher.early_outcome = error
    daemon_prelaunch.start()
    launcher = _started_launcher()

    with pytest.raises(RuntimeError) as raised:
        select_client()
    assert raised.value is error
    # A retry is an ordinary attempt, not a replay of the early failure.
    assert select_client(launcher=launcher).client.name == "full"


def test_declined_eviction_falls_back_to_a_full_connect_or_start():
    _FakeLauncher.early_outcome = None
    daemon_prelaunch.start()
    launcher = _started_launcher()

    assert select_client().client.name == "full"
    assert launcher.calls == ["early", "full"]


def test_a_different_launcher_does_not_take_the_early_result():
    daemon_prelaunch.start()
    _started_launcher()
    other = _FakeLauncher()

    assert select_client(launcher=other).client.name == "full"
    assert daemon_prelaunch.take() is not None


def test_take_waits_for_the_worker():
    gate = threading.Event()

    class _Slow(_FakeLauncher):
        def connect_or_start_without_eviction(self):
            gate.wait(5)
            return _Result("slow")

    import sshpilot.daemon.launcher as launcher_module

    launcher_module.DaemonLauncher = _Slow
    daemon_prelaunch.start()
    assert daemon_prelaunch.launcher() is None  # never blocks
    threading.Timer(0.05, gate.set).start()
    assert daemon_prelaunch.take().client.name == "slow"


def test_disable_environment_skips_the_early_start(monkeypatch):
    monkeypatch.setenv(daemon_prelaunch.DISABLE_ENV, "1")
    assert daemon_prelaunch.start() is False
    assert daemon_prelaunch.take() is None


def test_verbosity_reaches_the_daemon_environment(monkeypatch):
    import os

    assert daemon_prelaunch.start_from_argv(["--verbose"])
    _started_launcher()
    assert os.environ.get("SSHPILOT_DAEMON_VERBOSE") == "1"
    assert "SSHPILOT_DAEMON_QUIET" not in os.environ


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["--verbose", "--quiet"]])
def test_non_gui_command_lines_start_nothing(argv, capsys):
    assert daemon_prelaunch.start_from_argv(argv) is False
    assert daemon_prelaunch._state is None
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_discard_closes_an_untaken_client():
    daemon_prelaunch.start()
    _started_launcher()
    result = daemon_prelaunch._state.result
    daemon_prelaunch.discard()
    assert result.client.closed is True
    # Nothing is handed over afterwards.
    assert daemon_prelaunch.take() is None


def test_discard_after_take_leaves_the_client_alone():
    daemon_prelaunch.start()
    _started_launcher()
    taken = daemon_prelaunch.take()
    daemon_prelaunch.discard()
    assert taken.client.closed is False


def test_discard_never_blocks_and_the_worker_closes_its_late_client():
    gate = threading.Event()
    late = _Result("late")

    class _Slow(_FakeLauncher):
        def connect_or_start_without_eviction(self):
            gate.wait(5)
            return late

    import sshpilot.daemon.launcher as launcher_module

    launcher_module.DaemonLauncher = _Slow
    daemon_prelaunch.start()
    daemon_prelaunch.discard()  # a secondary instance exiting mid-attempt
    gate.set()
    assert daemon_prelaunch._state.done.wait(5)
    assert late.client.closed is True
    assert daemon_prelaunch.take() is None


def test_language_is_exported_before_the_worker_starts(monkeypatch):
    """The daemon inherits the environment; it is complete before any spawn."""
    order = []

    class _Recording(_FakeLauncher):
        def connect_or_start_without_eviction(self):
            order.append("worker")
            return _Result("spawned")

    import sshpilot.daemon.launcher as launcher_module

    monkeypatch.setattr(
        daemon_prelaunch, "_export_language", lambda: order.append("language")
    )
    launcher_module.DaemonLauncher = _Recording
    daemon_prelaunch.start()
    assert daemon_prelaunch.take().client.name == "spawned"
    assert order == ["language", "worker"]
