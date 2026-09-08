"""The launch choke point owns what six call sites used to hand-write."""

from __future__ import annotations

import subprocess

import pytest

from sshpilot.api.errors import SshPilotError
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.interactions import ExecutionInteractionMode
from sshpilot.daemon.ssh_launch import (
    _POLICIES,
    IO_CAPTURE,
    CopyIdLaunch,
    ForwardLaunch,
    LaunchKind,
    RemoteCommandLaunch,
    ScpLaunch,
    SftpLaunch,
    SshLauncher,
    TerminalLaunch,
)


class Broker:
    def __init__(self):
        self.calls = []
        self.events = []

    def prepare_operation_launch(self, argv, environment, **kwargs):
        self.calls.append((tuple(argv), dict(environment), kwargs))
        return tuple(argv) + ("-brokered",), {**environment, "BROKERED": "1"}

    def prepare_launch(self, spec, builder, *, trailing_args=(), headless=False):
        argv, env = builder(spec.connection_id, interaction_policy="normal")
        self.calls.append((tuple(argv), dict(env), {"headless": headless}))
        return tuple(argv) + tuple(trailing_args), dict(env)

    def mark_authenticated(self, scope_id):
        self.events.append(("mark", scope_id))

    def cancel_session(self, scope_id):
        self.events.append(("cancel", scope_id))


class Provider:
    def __init__(self):
        self.calls = []

    def _record(self, name, connection_id, args, kwargs):
        self.calls.append((name, connection_id, args, kwargs))
        return ("ssh", "-o", "X=1", "host"), {"PATH": "/usr/bin"}

    def prepare_remote_command_launch(self, connection_id, *args, **kwargs):
        return self._record("remote_command", connection_id, args, kwargs)

    def prepare_scp_launch(self, connection_id, *args, **kwargs):
        return self._record("scp", connection_id, args, kwargs)

    def prepare_daemon_scp_launch(self, connection_id, *args, **kwargs):
        return self._record("daemon_scp", connection_id, args, kwargs)

    def prepare_copy_id_launch(self, connection_id, *args, **kwargs):
        return self._record("copy_id", connection_id, args, kwargs)

    def prepare_terminal_launch(self, connection_id, *args, **kwargs):
        return self._record("terminal", connection_id, args, kwargs)

    def prepare_sftp_launch(self, connection_id, *args, **kwargs):
        return self._record("sftp", connection_id, args, kwargs)

    def prepare_forward_launch(self, connection_id, *args, **kwargs):
        return self._record("forward", connection_id, args, kwargs)


class Spec:
    connection_id = ConnectionId("demo")
    session_id = SessionId("session-1")
    hostname = "example.com"
    username = "alice"
    port = 22
    remote_command = None
    force_tty = False


def _launcher(**kwargs):
    return SshLauncher(Provider(), Broker(), **kwargs)


def test_every_kind_has_a_policy_row():
    assert set(_POLICIES) == set(LaunchKind)


def test_policy_fields_are_all_explicit():
    """A new kind cannot be added without deciding every axis."""

    import dataclasses

    from sshpilot.daemon.ssh_launch import _KindPolicy

    for field in dataclasses.fields(_KindPolicy):
        assert field.default is dataclasses.MISSING, field.name
        assert field.default_factory is dataclasses.MISSING, field.name


def test_scope_is_cancelled_even_when_the_body_raises():
    launcher = _launcher()
    broker = launcher._broker
    with pytest.raises(RuntimeError):
        with launcher.open(scope_id=SessionId("s1"), connection_id=ConnectionId("c")):
            raise RuntimeError("boom")
    assert broker.events == [("cancel", "s1")]


def test_remembered_credentials_are_committed_before_teardown():
    """``cancel_session`` clears pending secrets, so the order is the bug."""

    launcher = _launcher()
    broker = launcher._broker
    with launcher.open(scope_id=SessionId("s1"), connection_id=ConnectionId("c")) as scope:
        scope.authenticated()
    assert broker.events == [("mark", "s1"), ("cancel", "s1")]


def test_unauthenticated_scope_never_marks():
    launcher = _launcher()
    broker = launcher._broker
    with launcher.open(scope_id=SessionId("s1"), connection_id=ConnectionId("c")):
        pass
    assert broker.events == [("cancel", "s1")]


def test_prepare_applies_the_policy_interaction_mode():
    launcher = _launcher()
    provider, broker = launcher._provider, launcher._broker
    with launcher.open(scope_id=SessionId("s1"), connection_id=ConnectionId("c")) as scope:
        scope.prepare(RemoteCommandLaunch(remote_command="uptime"))
    name, connection_id, args, kwargs = provider.calls[0]
    assert name == "remote_command"
    assert args == ("uptime",)
    assert kwargs["interaction_policy"] == "broker"
    assert broker.calls[0][2]["scope_id"] == "s1"


def test_copy_id_does_not_receive_an_interaction_policy():
    """``prepare_copy_id_launch`` composes its own auth and takes no policy."""

    launcher = _launcher()
    provider = launcher._provider
    with launcher.open(scope_id=SessionId("s1"), connection_id=ConnectionId("c")) as scope:
        scope.prepare(CopyIdLaunch(public_key_path="/k.pub", force=True))
    name, _cid, args, kwargs = provider.calls[0]
    assert name == "copy_id"
    assert args == ("/k.pub",)
    assert kwargs == {"force": True}


def test_scp_prefers_the_thread_asserting_provider_method():
    launcher = _launcher()
    provider = launcher._provider
    with launcher.open(scope_id=SessionId("s1"), connection_id=ConnectionId("c")) as scope:
        scope.prepare(ScpLaunch(extra_args=("-r", "a"), target_override="h:/t"))
    assert provider.calls[0][0] == "daemon_scp"


def test_scp_falls_back_when_the_provider_lacks_the_daemon_variant():
    class Bare(Provider):
        prepare_daemon_scp_launch = None

    launcher = SshLauncher(Bare(), Broker())
    with launcher.open(scope_id=SessionId("s1"), connection_id=ConnectionId("c")) as scope:
        scope.prepare(ScpLaunch())
    assert launcher._provider.calls[0][0] == "scp"


def test_one_scope_can_hold_many_launches():
    """A broadcast is one scope across many hosts."""

    launcher = _launcher()
    broker = launcher._broker
    with launcher.open(scope_id=SessionId("op-1")) as scope:
        for host in ("a", "b", "c"):
            scope.prepare(
                RemoteCommandLaunch(remote_command="id"),
                connection_id=ConnectionId(host),
                hostname=host,
            )
        scope.authenticated()
    assert len(broker.calls) == 3
    assert {call[2]["scope_id"] for call in broker.calls} == {"op-1"}
    assert broker.events == [("mark", "op-1"), ("cancel", "op-1")]


def test_interaction_mode_is_per_call_not_per_kind():
    launcher = _launcher()
    broker = launcher._broker
    with launcher.open(scope_id=SessionId("s1")) as scope:
        scope.prepare(
            RemoteCommandLaunch(remote_command="id"),
            connection_id=ConnectionId("c"),
            interaction_mode=ExecutionInteractionMode.AUTOFILL_ONLY,
        )
    assert broker.calls[0][2]["interaction_mode"] is ExecutionInteractionMode.AUTOFILL_ONLY


def test_spawn_records_the_child_in_the_process_registry(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "sshpilot.daemon.ssh_launch.record_owned_process_or_abandon",
        lambda process, **kwargs: recorded.append((process, kwargs)),
    )

    class FakeProcess:
        pid = 4321

        def poll(self):
            return 0

    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    launcher = SshLauncher(Provider(), Broker(), popen=fake_popen)
    with launcher.open(
        scope_id=SessionId("s1"), connection_id=ConnectionId("c"), registry_kind="transfer"
    ) as scope:
        prepared = scope.prepare(RemoteCommandLaunch(remote_command="id"))
        prepared.spawn(IO_CAPTURE)

    assert recorded and recorded[0][1]["kind"] == "transfer"
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["close_fds"] is True
    assert captured["kwargs"]["stdout"] is subprocess.PIPE


def test_adopted_children_reach_the_registry_too(monkeypatch):
    """Broadcast's runner owns the Popen; ownership still belongs to the scope."""

    recorded = []
    monkeypatch.setattr(
        "sshpilot.daemon.ssh_launch.record_owned_process_or_abandon",
        lambda process, **kwargs: recorded.append((process, kwargs)),
    )

    class FakeProcess:
        pid = 99

        def poll(self):
            return None

    launcher = _launcher()
    with launcher.open(scope_id=SessionId("s1"), registry_kind="session") as scope:
        scope.adopt(FakeProcess())
    assert recorded and recorded[0][1]["kind"] == "session"


def test_retry_argv_stays_inside_the_scope():
    launcher = _launcher()
    with launcher.open(scope_id=SessionId("s1"), connection_id=ConnectionId("c")) as scope:
        prepared = scope.prepare(ScpLaunch())
        retry = prepared.with_argv(("scp", "-O", "host"))
    assert retry.environment == prepared.environment
    assert retry.argv == ("scp", "-O", "host")


def test_prepare_after_close_is_refused():
    launcher = _launcher()
    with launcher.open(scope_id=SessionId("s1")) as scope:
        pass
    with pytest.raises(RuntimeError):
        scope.prepare(RemoteCommandLaunch(remote_command="id"))


def test_session_preparation_uses_the_table_not_the_caller():
    launcher = _launcher()
    broker = launcher._broker
    argv, _env = launcher.prepare_session(Spec(), SftpLaunch())
    assert argv[-1] == "sftp", "trailing args come from the policy row"
    assert broker.calls[0][2]["headless"] is True


def test_terminal_session_is_not_headless():
    launcher = _launcher()
    broker = launcher._broker
    launcher.prepare_session(Spec(), TerminalLaunch())
    assert broker.calls[0][2]["headless"] is False


def test_forward_intent_carries_its_own_parameters():
    launcher = _launcher()
    provider = launcher._provider
    launcher.prepare_session(
        Spec(),
        ForwardLaunch(
            forward_type="local", bind_host="127.0.0.1", bind_port=8080,
            destination_host="db", destination_port=5432,
        ),
    )
    name, _cid, _args, kwargs = provider.calls[0]
    assert name == "forward"
    assert kwargs["forward_type"] == "local"
    assert kwargs["bind_port"] == 8080
    assert kwargs["destination_host"] == "db"


def test_diagnostics_are_inserted_only_for_kinds_that_ask_for_them():
    class Readiness:
        def __init__(self):
            self.calls = 0

        def prepare_launch(self, session_id, argv, environment):
            self.calls += 1
            return None

    readiness = Readiness()
    launcher = SshLauncher(Provider(), Broker(), readiness_manager=readiness)
    launcher.prepare_session(Spec(), SftpLaunch())
    assert readiness.calls == 0
    launcher.prepare_session(Spec(), TerminalLaunch())
    assert readiness.calls == 1


def test_a_missing_broker_is_refused_rather_than_launched():
    launcher = SshLauncher(Provider(), None)
    with pytest.raises(SshPilotError):
        with launcher.open(scope_id=SessionId("s1")):
            pass
