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

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.pre_command import (
    PreCommandLaunchKind,
    PreCommandSettings,
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


def _runner(
    command="true", *, timeout=5, coalesce=0, runner=None, clock=None,
    abort=False, per_connection_timeout=0,
):
    """A real runner plus the list its notices land in."""

    notices = []
    instance = PreConnectionCommandRunner(
        settings=_settings(timeout, coalesce),
        lookup=lambda connection_id: PreCommandSettings(
            command=command,
            timeout=per_connection_timeout,
            abort_on_failure=abort,
        ),
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


class ProviderWithRunner(RecordingProvider):
    """A launch provider carrying the daemon's one runner.

    This is how production wires it: the runner hangs off the provider, so a
    launcher built with nothing but ``(provider, broker)`` still finds it.
    """

    def __init__(self, runner):
        self.pre_command_runner = runner


def test_a_launcher_built_without_a_runner_finds_the_provider_s():
    """The wiring that four services depend on.

    ``identity_service``, ``native_scp_backend``, ``broadcast_service`` and
    ``privileged_file_service`` each build ``SshLauncher(provider, broker)``
    with no runner argument, so ssh-copy-id, SCP, Host Info/broadcast and
    privileged file reads all resolve it this way. When the runner was passed
    only to the session launcher, every one of those kinds silently skipped
    the pre-connection command -- half the bug this feature exists to fix,
    shipped looking fixed.
    """

    runner = RecordingRunner()
    launcher = SshLauncher(ProviderWithRunner(runner), RecordingBroker())

    with launcher.open(scope_id=SessionId("op-1"), connection_id=ConnectionId("demo")) as scope:
        scope.prepare(CopyIdLaunch(public_key_path="/tmp/id.pub"))

    assert runner.calls == [
        (ConnectionId("demo"), "op-1", PreCommandLaunchKind.COPY_ID)
    ]


def test_an_explicit_runner_wins_over_the_provider_s():
    """The injection seam stays usable, and unambiguous."""

    explicit = RecordingRunner()
    from_provider = RecordingRunner()
    launcher = SshLauncher(
        ProviderWithRunner(from_provider),
        RecordingBroker(),
        pre_command_runner=explicit,
    )

    launcher.prepare_session(Spec(), TerminalLaunch())

    assert len(explicit.calls) == 1
    assert from_provider.calls == []


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


# --- riding an existing master -----------------------------------------------
#
# Host Info samples every two seconds over a multiplex master it already
# holds. Those launches open no connection, so knocking for them authorised a
# door that was already open -- measured at seven knocks in forty seconds with
# the dashboard open, which is enough to trip the replay protection and rate
# limiting that knock daemons have. The first launch still knocks: it is the
# one that builds the master.


def _fake_ssh(control_path, *, master_alive, calls):
    """Stand in for both probe steps: ``ssh -G`` then ``ssh -O check``."""

    def _run(argv, **kwargs):
        argv = tuple(argv)
        calls.append(argv)
        if "-G" in argv:
            body = f"user alice\ncontrolpath {control_path}\nport 22\n"
            return SimpleNamespace(returncode=0, stdout=body.encode(), stderr=b"")
        if "-O" in argv:
            return SimpleNamespace(
                returncode=0 if master_alive else 1,
                stdout=b"Master running (pid=123)" if master_alive else b"",
                stderr=b"" if master_alive else b"No such file or directory",
            )
        raise AssertionError(f"unexpected probe: {argv}")

    return _run


def _launch_with_master(monkeypatch, *, control_path="/run/cm-abc", master_alive=True):
    import sshpilot.daemon.control_masters as masters

    calls = []
    monkeypatch.setattr(
        masters.subprocess, "run", _fake_ssh(control_path, master_alive=master_alive, calls=calls)
    )
    runner = RecordingRunner()
    launcher = SshLauncher(
        RecordingProvider(), RecordingBroker(), pre_command_runner=runner
    )
    launcher.prepare_session(Spec(), TerminalLaunch())
    return runner, calls


def test_a_launch_riding_a_live_master_does_not_run_the_command(monkeypatch):
    runner, _calls = _launch_with_master(monkeypatch, master_alive=True)

    assert runner.calls == []


def test_a_launch_whose_master_is_gone_still_runs_the_command(monkeypatch):
    runner, _calls = _launch_with_master(monkeypatch, master_alive=False)

    assert len(runner.calls) == 1


def test_the_probe_never_connects(monkeypatch):
    """The first attempt appended ``-O check`` to the launch argv, where ssh
    read it as more remote command: it connected and ran it. ``-G`` resolves
    the config and exits, so the expansion costs nothing and touches no host.
    """

    _runner, calls = _launch_with_master(monkeypatch)

    resolve = calls[0]
    assert resolve[1] == "-G", "the ControlPath must be resolved, not guessed"
    assert resolve[0] == "ssh"
    # Whatever follows is the launch's own argv, unaltered -- the expansion
    # depends on the host, port and user it carries.
    assert resolve[2:] == ("-o", "Opt=1", "host")
    # And the check that follows probes a literal path, never the launch argv.
    check = calls[1]
    assert "-O" in check and "check" in check
    assert "/run/cm-abc" in " ".join(check)


def test_multiplexing_turned_off_runs_the_command(monkeypatch):
    runner, calls = _launch_with_master(monkeypatch, control_path="none")

    assert len(runner.calls) == 1
    assert len(calls) == 1, "there is nothing to probe once ControlPath is none"


def test_an_unanswerable_probe_runs_the_command(monkeypatch):
    """Being wrong is asymmetric: a needless knock is noise, a missing one
    fails the connection."""

    import sshpilot.daemon.control_masters as masters

    def _explode(*args, **kwargs):
        raise OSError("ssh is unavailable")

    monkeypatch.setattr(masters.subprocess, "run", _explode)
    runner = RecordingRunner()
    launcher = SshLauncher(
        RecordingProvider(), RecordingBroker(), pre_command_runner=runner
    )

    launcher.prepare_session(Spec(), TerminalLaunch())

    assert len(runner.calls) == 1


def test_a_non_ssh_argv_is_never_probed(monkeypatch):
    """sftp and scp take neither flag, and a plugin protocol's argv is not an
    ssh command line at all -- which is also how Docker and Mosh keep their
    pre-connection command."""

    import sshpilot.daemon.control_masters as masters

    calls = []
    monkeypatch.setattr(
        masters.subprocess,
        "run",
        lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0),
    )

    assert not masters.launch_rides_live_master(("docker", "exec", "-it", "web"), {})
    assert not masters.launch_rides_live_master(("sftp", "host"), {})
    assert calls == []


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


# --- plugin protocols --------------------------------------------------------
#
# Docker over ``ssh://`` and Mosh open real SSH connections, so a host behind
# port knocking has to be reachable from them too. They already route through
# the same launch provider as SSH, so the launcher hook reaches them for free;
# what was missing was any way to set the value, which the API refused and the
# editor hid.


def test_a_plugin_connection_may_carry_a_pre_connection_command():
    from tests.helpers.fake_connection_repository import make_test_connection_service
    from sshpilot.api.models.connections import CreateConnectionRequest

    service = make_test_connection_service()
    service.create_connection(
        CreateConnectionRequest(
            nickname="dockerbox",
            hostname="localhost",
            username="root",
            port=22,
            protocol="docker",
            plugin_data={"container": "web", "command": "sh", "runtime": "docker"},
        )
    )
    service.update_connection_metadata(
        "dockerbox",
        {"pre_command": "knock host 1000 2000", "pre_command_abort": True},
    )

    settings = service.get_pre_connection_settings("dockerbox")
    assert settings.command == "knock host 1000 2000"
    assert settings.abort_on_failure is True


def test_a_plugin_connection_still_refuses_ssh_directives():
    """Metadata is the only channel: sshPilot builds no ssh command line for
    these connections, so config_patch stays refused outright."""

    from tests.helpers.fake_connection_repository import make_test_connection_service
    from sshpilot.api.errors import SshPilotError
    from sshpilot.api.models.connections import CreateConnectionRequest

    service = make_test_connection_service()

    with pytest.raises(SshPilotError):
        service.create_connection(
            CreateConnectionRequest(
                nickname="dockerbox2",
                hostname="localhost",
                username="root",
                port=22,
                protocol="docker",
                plugin_data={"container": "web"},
                config_patch={"proxy_jump": ["bastion"]},
            )
        )


# --- the hard gate -----------------------------------------------------------
#
# Off by default: SSH's own error tells the user far more than a pre-step veto.
# On, the launch is refused -- Royal TS offers the same choice for its connect
# tasks, and a VPN dial-up that failed genuinely means "do not dial".


def test_a_failing_command_does_not_stop_the_launch_by_default():
    runner, notices = _runner("knock", runner=lambda *a, **k: SimpleNamespace(
        returncode=3, stdout="", stderr=""))

    assert runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL) is True
    assert notices[-1].aborted is False


def test_a_failing_command_stops_the_launch_when_asked():
    runner, notices = _runner("knock", abort=True, runner=lambda *a, **k: SimpleNamespace(
        returncode=3, stdout="", stderr=""))

    assert runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL) is False
    assert notices[-1].aborted is True
    assert notices[-1].reason is PreCommandReason.NONZERO_EXIT


def test_a_timeout_stops_the_launch_when_asked():
    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sh", timeout=1)

    runner, notices = _runner("sleep 600", abort=True, runner=_timeout)

    assert runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL) is False
    assert notices[-1].aborted is True


def test_a_successful_command_never_reports_an_abort():
    runner, notices = _runner("true", abort=True, runner=lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="", stderr=""))

    assert runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL) is True
    assert notices[-1].aborted is False


def test_a_connection_with_no_command_is_never_gated():
    """An empty command is not a failed one, whatever the flag says."""

    runner, _ = _runner("", abort=True)

    assert runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL) is True


def test_an_internal_fault_never_locks_the_user_out():
    """The gate must not be reachable by accident."""

    runner, _ = _runner("knock", abort=True)
    runner._run_guarded = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug"))

    assert runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL) is True


def test_the_launcher_refuses_a_gated_launch():
    class _Gate:
        def run(self, connection_id, *, scope_id, kind):
            return False

    launcher = SshLauncher(
        RecordingProvider(), RecordingBroker(), pre_command_runner=_Gate()
    )

    with pytest.raises(SshPilotError) as caught:
        launcher.prepare_session(Spec(), TerminalLaunch())

    assert caught.value.code is ErrorCode.SESSION_STARTUP_FAILED


# --- per-connection timeout --------------------------------------------------


def test_a_connection_timeout_overrides_the_app_default():
    seen = {}

    def _record(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner, _ = _runner("knock", timeout=30, per_connection_timeout=5, runner=_record)
    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)

    assert seen["timeout"] == 5


def test_no_connection_timeout_follows_the_app_default():
    """Zero means "move with the preference", not "no timeout"."""

    seen = {}

    def _record(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner, _ = _runner("knock", timeout=30, per_connection_timeout=0, runner=_record)
    runner.run("c1", scope_id="s1", kind=PreCommandLaunchKind.TERMINAL)

    assert seen["timeout"] == 30
