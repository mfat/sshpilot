"""The launcher's pre-connection command step.

The command is almost always a port knock (``fwknop``/``knock``) or a VPN
dial-up authorising a short access window. Before this moved into the daemon
it ran in the GTK frontend, for terminal tabs only -- so on a knock-gated host
SFTP browsing, port forwards, SCP transfers, ``ssh-copy-id`` and Host Info
probes all dialled a closed port. These tests pin the four properties that
make the move worth making, plus the one that must *not* change:

* every launch kind runs it (the bug);
* concurrent launches to one connection never overlap (an interleaved knock
  sequence is scored as a *failed* sequence by some ``knockd`` configs);
* a repeat inside the coalescing window reuses the last run;
* the log carries the whole story without ever carrying the command or its
  output above DEBUG;
* and a failing pre-step never, ever fails the launch.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.pre_command import (
    PreCommandLaunchKind,
    PreCommandPhase,
    PreCommandReason,
)
from sshpilot.daemon.pre_connection_command import PreConnectionCommandRunner
from sshpilot.daemon.ssh_launch import (
    CopyIdLaunch,
    ForwardLaunch,
    LaunchKind,
    RemoteCommandLaunch,
    ScpLaunch,
    SftpLaunch,
    SshLauncher,
    TerminalLaunch,
    _POLICIES,
)


SECRET = "s3cr3t-token-8fbd21"


# --- doubles -----------------------------------------------------------------


class RecordingBroker:
    """Minimal broker that records the order of its calls."""

    def __init__(self, order=None):
        self.order = order if order is not None else []

    def prepare_launch(self, spec, builder, *, trailing_args=(), headless=False):
        self.order.append("broker")
        argv, env = builder(spec.connection_id, interaction_policy="normal")
        return tuple(argv) + tuple(trailing_args), dict(env)

    def prepare_operation_launch(self, argv, environment, **kwargs):
        self.order.append("broker")
        return tuple(argv), dict(environment)

    def mark_authenticated(self, scope_id):
        pass

    def cancel_session(self, scope_id):
        pass


class RecordingProvider:
    def _result(self):
        return ("ssh", "-o", "Opt=1", "host"), {"PATH": "/usr/bin"}

    def prepare_daemon_terminal_launch(self, connection_id, **kwargs):
        return self._result()

    def prepare_daemon_sftp_launch(self, connection_id, **kwargs):
        return self._result()

    def prepare_daemon_forward_launch(self, connection_id, **kwargs):
        return self._result()

    def prepare_daemon_scp_launch(self, connection_id, **kwargs):
        return self._result()

    def prepare_remote_command_launch(self, connection_id, *args, **kwargs):
        return self._result()

    def prepare_copy_id_launch(self, connection_id, *args, **kwargs):
        return self._result()


class Spec:
    connection_id = ConnectionId("demo")
    session_id = SessionId("session-1")
    hostname = "example.com"
    username = "alice"
    port = 22
    remote_command = None
    force_tty = False


class RecordingRunner:
    """Stands in for the real runner when only "was it called" matters."""

    def __init__(self, order=None):
        self.calls = []
        self.order = order if order is not None else []

    def run(self, connection_id, *, scope_id, kind):
        self.order.append("pre_command")
        self.calls.append((connection_id, scope_id, kind))


def _settings(timeout=5, coalesce=0):
    return SimpleNamespace(
        pre_command_timeout_seconds=timeout,
        pre_command_coalesce_seconds=coalesce,
    )


def _runner(command="true", *, timeout=5, coalesce=0, runner=None, clock=None):
    """A real runner plus the list its notices land in."""

    notices = []
    instance = PreConnectionCommandRunner(
        settings=_settings(timeout, coalesce),
        lookup=lambda connection_id: command,
        **({"runner": runner} if runner is not None else {}),
        **({"clock": clock} if clock is not None else {}),
    )
    instance.subscribe_events(lambda event: notices.append(event.payload))
    return instance, notices


# --- the policy table --------------------------------------------------------


def test_every_launch_kind_runs_the_pre_connection_command():
    """The bug this move exists to fix: only TERMINAL used to run it."""

    assert {kind for kind, policy in _POLICIES.items() if policy.pre_connection_command} == set(
        LaunchKind
    )


def test_public_launch_kinds_match_the_daemon_table():
    """The API mirrors LaunchKind by value; drift would silence a whole kind."""

    assert {item.value for item in PreCommandLaunchKind} == {
        item.value for item in LaunchKind
    }


# --- the hook ----------------------------------------------------------------


@pytest.mark.parametrize(
    "intent, expected",
    [
        (TerminalLaunch(), PreCommandLaunchKind.TERMINAL),
        (SftpLaunch(), PreCommandLaunchKind.SFTP),
        (
            ForwardLaunch(
                forward_type="local",
                bind_host="127.0.0.1",
                bind_port=9000,
                destination_host="db",
                destination_port=5432,
            ),
            PreCommandLaunchKind.FORWARD,
        ),
    ],
)
def test_session_launches_run_it_once_with_their_own_kind(intent, expected):
    runner = RecordingRunner()
    launcher = SshLauncher(
        RecordingProvider(), RecordingBroker(), pre_command_runner=runner
    )

    launcher.prepare_session(Spec(), intent)

    assert runner.calls == [(Spec.connection_id, str(Spec.session_id), expected)]


@pytest.mark.parametrize(
    "intent, expected",
    [
        (ScpLaunch(), PreCommandLaunchKind.SCP),
        (RemoteCommandLaunch(remote_command="uname -a"), PreCommandLaunchKind.REMOTE_COMMAND),
        (CopyIdLaunch(public_key_path="/tmp/id.pub"), PreCommandLaunchKind.COPY_ID),
    ],
)
def test_operation_launches_run_it_once_with_their_own_kind(intent, expected):
    runner = RecordingRunner()
    launcher = SshLauncher(
        RecordingProvider(), RecordingBroker(), pre_command_runner=runner
    )

    with launcher.open(scope_id=SessionId("op-1"), connection_id=ConnectionId("demo")) as scope:
        scope.prepare(intent)

    assert runner.calls == [(ConnectionId("demo"), "op-1", expected)]


def test_it_runs_after_brokering_not_before():
    """Unlock first, knock second.

    A keyring unlock landing between the knock and the connect can outlast the
    window the knock opened. The frontend ordered these deliberately; the move
    has to keep that order or it silently breaks knock-gated hosts on a locked
    vault.
    """

    order: list[str] = []
    launcher = SshLauncher(
        RecordingProvider(),
        RecordingBroker(order),
        pre_command_runner=RecordingRunner(order),
    )

    launcher.prepare_session(Spec(), TerminalLaunch())

    assert order == ["broker", "pre_command"]


def test_a_launcher_without_a_runner_still_prepares():
    launcher = SshLauncher(RecordingProvider(), RecordingBroker())

    argv, environment = launcher.prepare_session(Spec(), TerminalLaunch())

    assert argv[0] == "ssh"
    assert environment == {"PATH": "/usr/bin"}


# --- execution ---------------------------------------------------------------


def test_no_configured_command_runs_nothing():
    calls = []
    runner, notices = _runner("", runner=lambda *a, **k: calls.append(a))

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)

    assert calls == []
    assert notices == []


def test_a_successful_run_publishes_running_then_finished():
    runner, notices = _runner("exit 0")

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)

    assert [(n.phase, n.reason) for n in notices] == [
        (PreCommandPhase.RUNNING, PreCommandReason.OK),
        (PreCommandPhase.FINISHED, PreCommandReason.OK),
    ]
    assert notices[1].exit_code == 0


def test_a_non_zero_exit_is_reported_with_its_status():
    runner, notices = _runner("exit 7")

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.SFTP)

    finished = notices[-1]
    assert finished.reason is PreCommandReason.NONZERO_EXIT
    assert finished.exit_code == 7


def test_a_timeout_is_reported_as_a_timeout():
    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sh", timeout=1)

    runner, notices = _runner("sleep 600", runner=_timeout)

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.FORWARD)

    assert notices[-1].reason is PreCommandReason.TIMED_OUT
    assert notices[-1].exit_code is None


def test_a_shell_that_cannot_start_is_reported_not_raised():
    def _explode(*args, **kwargs):
        raise OSError("no such file")

    runner, notices = _runner("knock", runner=_explode)

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.SCP)

    assert notices[-1].reason is PreCommandReason.START_FAILED


def test_a_failing_lookup_never_raises():
    def _explode(connection_id):
        raise RuntimeError("store unavailable")

    instance = PreConnectionCommandRunner(settings=_settings(), lookup=_explode)
    notices = []
    instance.subscribe_events(lambda event: notices.append(event.payload))

    instance.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)

    assert notices == []


# --- serialization and coalescing -------------------------------------------


def test_concurrent_launches_to_one_connection_never_overlap():
    """Interleaved knocks are what some knockd configs score as a failure."""

    order: list[str] = []
    gate = threading.Lock()

    def _slow(*args, **kwargs):
        with gate:
            order.append("enter")
            time.sleep(0.05)
            order.append("exit")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner, _ = _runner("knock", runner=_slow)
    threads = [
        threading.Thread(
            target=runner.run,
            args=("c1",),
            kwargs={"scope_id": f"s{index}", "kind": PreCommandLaunchKind.TERMINAL},
        )
        for index in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert order == ["enter", "exit"] * 3


def test_a_repeat_inside_the_window_reuses_the_last_run():
    runs = []

    def _record(*args, **kwargs):
        runs.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner, notices = _runner("knock", coalesce=60, runner=_record)

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)
    runner.run("c1", scope_id="s2", kind=PreCommandLaunchKind.SFTP)

    assert len(runs) == 1
    assert notices[-1].reason is PreCommandReason.COALESCED
    # The second launch still gets a finished notice, so a surface that showed
    # the running state has something to clear it with.
    assert notices[-1].phase is PreCommandPhase.FINISHED


def test_a_repeat_outside_the_window_runs_again():
    runs = []
    now = [1000.0]

    def _record(*args, **kwargs):
        runs.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner, _ = _runner("knock", coalesce=5, runner=_record, clock=lambda: now[0])

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)
    now[0] += 30
    runner.run("c1", scope_id="s2", kind=PreCommandLaunchKind.TERMINAL)

    assert len(runs) == 2


def test_a_zero_window_disables_coalescing():
    runs = []

    def _record(*args, **kwargs):
        runs.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner, _ = _runner("knock", coalesce=0, runner=_record)

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)
    runner.run("c1", scope_id="s2", kind=PreCommandLaunchKind.TERMINAL)

    assert len(runs) == 2


def test_different_connections_do_not_coalesce_with_each_other():
    runs = []

    def _record(*args, **kwargs):
        runs.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner, _ = _runner("knock", coalesce=60, runner=_record)

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)
    runner.run("c2", scope_id="s2", kind=PreCommandLaunchKind.TERMINAL)

    assert len(runs) == 2


def test_a_failed_run_is_not_coalesced_over():
    """A knock that failed opened nothing, so the next launch must retry it."""

    results = [
        SimpleNamespace(returncode=1, stdout="", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ]

    def _record(*args, **kwargs):
        return results.pop(0)

    runner, notices = _runner("knock", coalesce=60, runner=_record)

    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)
    runner.run("c1", scope_id="s2", kind=PreCommandLaunchKind.TERMINAL)

    assert results == []
    assert notices[-1].reason is PreCommandReason.OK


# --- the log -----------------------------------------------------------------


LOGGER = "sshpilot.daemon.pre_connection_command"


def test_lifecycle_is_logged_and_the_command_is_not(caplog):
    """INFO/WARNING carry lifecycle; content stays at DEBUG."""

    def _fail(*args, **kwargs):
        return SimpleNamespace(returncode=9, stdout="", stderr=f"denied {SECRET}")

    runner, _ = _runner(f"knock --token {SECRET}", runner=_fail)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)

    assert "pre-connection command starting kind=terminal" in caplog.text
    assert "pre-connection command failed exit=9" in caplog.text
    assert SECRET not in caplog.text


def test_content_is_available_at_debug(caplog):
    """The trace has to be complete for a bug report, just not by default."""

    def _fail(*args, **kwargs):
        return SimpleNamespace(returncode=9, stdout="", stderr="permission denied")

    runner, _ = _runner("knock", runner=_fail)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)

    assert "pre-connection command text: knock" in caplog.text
    assert "permission denied" in caplog.text


def test_captured_output_is_logged_as_a_byte_count_not_as_output(caplog):
    def _succeed(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=SECRET, stderr="")

    runner, _ = _runner("knock", runner=_succeed)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)

    assert f"stdout_bytes={len(SECRET)}" in caplog.text
    assert SECRET not in caplog.text


def test_a_coalesced_run_says_so_in_the_log(caplog):
    def _succeed(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner, _ = _runner("knock", coalesce=60, runner=_succeed)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)
        runner.run("c1", scope_id="s2", kind=PreCommandLaunchKind.SFTP)

    assert "pre-connection command reused" in caplog.text
    assert "window_s=60" in caplog.text


def test_records_carry_the_connection_and_scope_ids():
    """``log_context`` is what makes the whole sequence greppable per host.

    Formatted through a real handler, because ``SanitizingFormatter`` reads the
    context variable when the record is *emitted* -- which is how production
    works and why a formatter applied afterwards would see nothing.
    """

    import io

    from sshpilot.logging_support import SanitizingFormatter

    def _succeed(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner, _ = _runner("knock", runner=_succeed)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SanitizingFormatter("%(message)s"))
    logger = logging.getLogger(LOGGER)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        runner.run("conn-9", scope_id="sess-4", kind=PreCommandLaunchKind.TERMINAL)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert lines
    assert all("[connection=conn-9 session=sess-4]" in line for line in lines)
