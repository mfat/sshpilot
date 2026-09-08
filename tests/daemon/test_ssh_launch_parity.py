"""Every SSH feature must launch exactly as it did before the choke point.

Moving six call sites onto ``sshpilot.daemon.ssh_launch`` turned their
hand-written constants into one policy table. A wrong cell there is silent:
it does not fail a unit test, it changes how a real OpenSSH child is started
and only shows up against a live server. One cell *was* wrong -- port
forwarding lost ``SSH_ASKPASS_REQUIRE=force`` and would have failed a
password-authenticated forward instead of prompting -- and the whole existing
suite still passed.

So these tests pin the values that were in force before the migration:
per-kind askpass level, trailing arguments, diagnostics, interaction policy,
prompt identity, and the ``Popen`` flags each spawn kind used. Their job is to
break loudly when a table cell moves.
"""

from __future__ import annotations

import subprocess

import pytest

from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.daemon.ssh_launch import (
    IO_CAPTURE,
    IO_CAPTURE_WITH_STDIN,
    IO_MERGED_TEXT,
    IO_STDERR_ONLY,
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


class RecordingBroker:
    def __init__(self):
        self.session_calls = []
        self.operation_calls = []

    def prepare_launch(self, spec, builder, *, trailing_args=(), headless=False):
        argv, env = builder(spec.connection_id, interaction_policy="normal")
        self.session_calls.append(
            {"trailing_args": tuple(trailing_args), "headless": headless}
        )
        return tuple(argv) + tuple(trailing_args), dict(env)

    def prepare_operation_launch(self, argv, environment, **kwargs):
        self.operation_calls.append(kwargs)
        return tuple(argv), dict(environment)

    def mark_authenticated(self, scope_id):
        pass

    def cancel_session(self, scope_id):
        pass


class RecordingProvider:
    def __init__(self):
        self.calls = []

    def _record(self, name, connection_id, args, kwargs):
        self.calls.append({"method": name, "args": args, "kwargs": kwargs})
        return ("ssh", "-o", "Opt=1", "host"), {"PATH": "/usr/bin"}

    def prepare_daemon_terminal_launch(self, connection_id, **kwargs):
        return self._record("terminal", connection_id, (), kwargs)

    def prepare_daemon_sftp_launch(self, connection_id, **kwargs):
        return self._record("sftp", connection_id, (), kwargs)

    def prepare_daemon_forward_launch(self, connection_id, **kwargs):
        return self._record("forward", connection_id, (), kwargs)

    def prepare_daemon_scp_launch(self, connection_id, **kwargs):
        return self._record("scp", connection_id, (), kwargs)

    def prepare_remote_command_launch(self, connection_id, *args, **kwargs):
        return self._record("remote_command", connection_id, args, kwargs)

    def prepare_copy_id_launch(self, connection_id, *args, **kwargs):
        return self._record("copy_id", connection_id, args, kwargs)


class Spec:
    connection_id = ConnectionId("demo")
    session_id = SessionId("session-1")
    hostname = "example.com"
    username = "alice"
    port = 22
    remote_command = None
    force_tty = False


def _launcher(readiness=None):
    return SshLauncher(
        RecordingProvider(), RecordingBroker(), readiness_manager=readiness
    )


# --- Session launches --------------------------------------------------------
#
# Values taken from the pre-migration adapters in daemon/server.py:
#   terminal  broker.prepare_launch(spec, builder)                  -> prefer
#   sftp      ... trailing_args=("sftp",), headless=True            -> force
#   forward   ... headless=True                                     -> force


@pytest.mark.parametrize(
    "intent, headless, trailing",
    [
        (TerminalLaunch(), False, ()),
        (SftpLaunch(), True, ("sftp",)),
        (
            ForwardLaunch(
                forward_type="local",
                bind_host="127.0.0.1",
                bind_port=8080,
                destination_host="db",
                destination_port=5432,
            ),
            True,
            (),
        ),
    ],
    ids=["terminal", "sftp", "forward"],
)
def test_session_askpass_level_and_trailing_args_are_unchanged(
    intent, headless, trailing
):
    launcher = _launcher()
    argv, _env = launcher.prepare_session(Spec(), intent)
    call = launcher._broker.session_calls[0]
    assert call["headless"] is headless
    assert call["trailing_args"] == trailing
    assert argv[len(argv) - len(trailing):] == trailing if trailing else True


def test_a_headless_session_never_falls_back_to_a_missing_terminal():
    """SFTP and forward children are spawned without a TTY.

    ``prefer`` lets OpenSSH try a terminal first; there is none, so a
    password, passphrase or host-key prompt is simply lost and the operation
    fails rather than asking. Only an interactive terminal may use ``prefer``.
    """

    for kind in (LaunchKind.SFTP, LaunchKind.FORWARD):
        assert _POLICIES[kind].headless is True, kind
    assert _POLICIES[LaunchKind.TERMINAL].headless is False


def test_only_terminal_sessions_get_diagnostics():
    class Readiness:
        def __init__(self):
            self.sessions = []

        def prepare_launch(self, session_id, argv, environment):
            self.sessions.append(session_id)
            return None

    for intent, expected in (
        (TerminalLaunch(), 1),
        (SftpLaunch(), 0),
        (
            ForwardLaunch(
                forward_type="dynamic", bind_host="", bind_port=1080
            ),
            0,
        ),
    ):
        readiness = Readiness()
        _launcher(readiness).prepare_session(Spec(), intent)
        assert len(readiness.sessions) == expected, intent


def test_forward_parameters_reach_the_provider_intact():
    launcher = _launcher()
    launcher.prepare_session(
        Spec(),
        ForwardLaunch(
            forward_type="remote",
            bind_host="0.0.0.0",
            bind_port=9000,
            destination_host="internal",
            destination_port=443,
        ),
    )
    kwargs = launcher._provider.calls[0]["kwargs"]
    assert kwargs["forward_type"] == "remote"
    assert kwargs["bind_host"] == "0.0.0.0"
    assert kwargs["bind_port"] == 9000
    assert kwargs["destination_host"] == "internal"
    assert kwargs["destination_port"] == 443


def test_terminal_remote_command_and_force_tty_reach_the_provider():
    class RemoteSpec(Spec):
        remote_command = "tmux attach"
        force_tty = True

    launcher = _launcher()
    launcher.prepare_session(
        RemoteSpec(), TerminalLaunch(remote_command="tmux attach", force_tty=True)
    )
    kwargs = launcher._provider.calls[0]["kwargs"]
    assert kwargs["remote_command"] == "tmux attach"
    assert kwargs["force_tty"] is True


# --- Operation launches ------------------------------------------------------


def test_scp_composes_as_before():
    """Pre-migration: prepare_daemon_scp_launch(extra_args, interaction_policy
    ="broker", target_override) then prepare_operation_launch(hostname=target).
    """

    launcher = _launcher()
    with launcher.open(
        scope_id=SessionId("transfer-7"), connection_id=ConnectionId("demo")
    ) as scope:
        scope.prepare(
            ScpLaunch(extra_args=("-r", "/src"), target_override="host:/dst"),
            connection_id=ConnectionId("demo"),
            hostname="host",
        )
    call = launcher._provider.calls[0]
    assert call["method"] == "scp"
    assert call["kwargs"]["extra_args"] == ["-r", "/src"]
    assert call["kwargs"]["target_override"] == "host:/dst"
    assert call["kwargs"]["interaction_policy"] == "broker"
    broker_call = launcher._broker.operation_calls[0]
    assert broker_call["scope_id"] == "transfer-7"
    assert broker_call["hostname"] == "host"


def test_broadcast_identity_still_reaches_the_prompt():
    """An empty username makes a prompt read "unknown@host" and makes the
    stored-secret lookup miss, so the user is asked for a password the keyring
    already holds."""

    launcher = _launcher()
    with launcher.open(scope_id=SessionId("op-3")) as scope:
        scope.prepare(
            RemoteCommandLaunch(remote_command="uptime"),
            connection_id=ConnectionId("demo"),
            hostname="demo.example.test",
            username="alice",
            port=2222,
        )
    call = launcher._broker.operation_calls[0]
    assert call["hostname"] == "demo.example.test"
    assert call["username"] == "alice"
    assert call["port"] == 2222


def test_privileged_commands_pass_no_identity_as_before():
    """The privileged path never supplied one; the broker derives it from the
    launch target."""

    launcher = _launcher()
    with launcher.open(
        scope_id=SessionId("sftp:demo"),
        connection_id=ConnectionId("demo"),
        owns_scope=False,
    ) as scope:
        scope.prepare(
            RemoteCommandLaunch(remote_command="sudo -n -- cat -- /etc/hosts"),
            connection_id=ConnectionId("demo"),
        )
    call = launcher._broker.operation_calls[0]
    assert call["hostname"] == ""
    assert call["username"] == ""
    assert call["port"] == 22


def test_copy_id_composes_as_before():
    launcher = _launcher()
    with launcher.open(
        scope_id=SessionId("operation-9"), connection_id=ConnectionId("demo")
    ) as scope:
        scope.prepare(
            CopyIdLaunch(public_key_path="/home/a/.ssh/id.pub", force=True),
            connection_id=ConnectionId("demo"),
        )
    call = launcher._provider.calls[0]
    assert call["method"] == "copy_id"
    assert call["args"] == ("/home/a/.ssh/id.pub",)
    assert call["kwargs"] == {"force": True}
    assert launcher._broker.operation_calls[0]["scope_id"] == "operation-9"


def test_remote_commands_are_brokered_not_normal():
    launcher = _launcher()
    with launcher.open(scope_id=SessionId("s")) as scope:
        scope.prepare(
            RemoteCommandLaunch(remote_command="id"),
            connection_id=ConnectionId("demo"),
        )
    assert launcher._provider.calls[0]["kwargs"]["interaction_policy"] == "broker"


def test_require_master_reaches_the_provider_when_set():
    """The flag is conditional (default call sites keep byte-identical
    kwargs), so bind the set path explicitly: a renamed provider parameter
    must fail here, not against a live host."""

    import inspect

    from sshpilot.daemon.connection_launch_provider import (
        DaemonConnectionLaunchProvider as ConnectionLaunchProvider,
    )

    launcher = _launcher()
    with launcher.open(scope_id=SessionId("s")) as scope:
        scope.prepare(
            RemoteCommandLaunch(remote_command="id", require_master=True),
            connection_id=ConnectionId("demo"),
        )
    call = launcher._provider.calls[0]
    assert call["kwargs"]["require_master"] is True
    inspect.signature(
        ConnectionLaunchProvider.prepare_remote_command_launch
    ).bind(None, ConnectionId("demo"), "id", **call["kwargs"])


def test_require_master_travels_in_provider_kwargs():
    """The intent carries its own provider parameters.

    ``_compose`` and ``_session_builder`` both build the provider call from
    ``provider_kwargs()``; a flag special-cased in one of them would be
    silently dropped by the other.
    """

    assert RemoteCommandLaunch(remote_command="id").provider_kwargs() == {}
    assert RemoteCommandLaunch(
        remote_command="id", require_master=True
    ).provider_kwargs() == {"require_master": True}


# --- Spawn flags -------------------------------------------------------------


@pytest.mark.parametrize(
    "io, expected",
    [
        (
            IO_STDERR_ONLY,
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
            },
        ),
        (
            IO_CAPTURE,
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
            },
        ),
        (
            IO_CAPTURE_WITH_STDIN,
            {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
            },
        ),
        (
            IO_MERGED_TEXT,
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "errors": "replace",
            },
        ),
    ],
    ids=["scp", "privileged-read", "privileged-write", "copy-id"],
)
def test_spawn_flags_match_the_pre_migration_call(io, expected, monkeypatch):
    monkeypatch.setattr(
        "sshpilot.daemon.ssh_launch.record_owned_process_or_abandon",
        lambda process, **kwargs: None,
    )
    captured = {}

    class FakeProcess:
        pid = 7

        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    launcher = SshLauncher(RecordingProvider(), RecordingBroker(), popen=fake_popen)
    with launcher.open(
        scope_id=SessionId("s"), connection_id=ConnectionId("demo")
    ) as scope:
        scope.prepare(
            RemoteCommandLaunch(remote_command="id"),
            connection_id=ConnectionId("demo"),
        ).spawn(io)

    for key, value in expected.items():
        assert captured[key] == value, key
    # Every daemon-owned child is detached and never runs through a shell.
    assert captured["shell"] is False
    assert captured["start_new_session"] is True
    # text/errors must be absent unless the policy asks for them.
    if "text" not in expected:
        assert "text" not in captured
        assert "errors" not in captured


def test_every_kind_records_its_children_under_a_stable_registry_kind(monkeypatch):
    """Registry kinds are written to disk by one build and read by another.

    ``open`` no longer takes a kind: prepare binds the policy row, and spawn
    records under that kind. A wrong open() default was how helpers briefly
    became sessions.
    """

    assert {kind: policy.registry_kind for kind, policy in _POLICIES.items()} == {
        LaunchKind.TERMINAL: "session",
        LaunchKind.SFTP: "sftp",
        LaunchKind.FORWARD: "forward",
        LaunchKind.SCP: "transfer",
        LaunchKind.REMOTE_COMMAND: "helper",
        LaunchKind.COPY_ID: "helper",
    }

    recorded = []
    monkeypatch.setattr(
        "sshpilot.daemon.ssh_launch.record_owned_process_or_abandon",
        lambda process, **kwargs: recorded.append(kwargs["kind"]),
    )

    class FakeProcess:
        pid = 1

        def poll(self):
            return 0

    launcher = SshLauncher(
        RecordingProvider(),
        RecordingBroker(),
        popen=lambda *a, **k: FakeProcess(),
    )
    cases = [
        (ScpLaunch(), "transfer"),
        (RemoteCommandLaunch(remote_command="id"), "helper"),
        (CopyIdLaunch(public_key_path="/k.pub"), "helper"),
    ]
    for intent, expected in cases:
        recorded.clear()
        with launcher.open(scope_id=SessionId("s")) as scope:
            scope.prepare(intent, connection_id=ConnectionId("demo")).spawn(IO_STDERR_ONLY)
        assert recorded == [expected], intent



# --- Intent/provider signature compatibility ---------------------------------


def test_every_intent_binds_against_the_real_provider_signature():
    """Test doubles take ``**kwargs`` and so cannot catch drift here.

    An intent field renamed without renaming the provider parameter (or a
    provider parameter dropped) would pass every unit test and fail only
    against a live connection service. Bind the real signatures instead.
    """

    import inspect

    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )
    from sshpilot.daemon.connection_launch_provider import (
        DaemonConnectionLaunchProvider as ConnectionLaunchProvider,
    )

    cases = [
        (TerminalLaunch(remote_command="uptime", force_tty=True), True),
        (SftpLaunch(), True),
        (
            ForwardLaunch(
                forward_type="local",
                bind_host="127.0.0.1",
                bind_port=1,
                destination_host="h",
                destination_port=2,
            ),
            True,
        ),
        (ScpLaunch(extra_args=("-r",), target_override="h:/d"), True),
        (RemoteCommandLaunch(remote_command="id"), True),
        (CopyIdLaunch(public_key_path="/k.pub", force=True), False),
    ]
    for intent, takes_policy in cases:
        policy = _POLICIES[intent.kind]
        kwargs = dict(intent.provider_kwargs())
        if takes_policy:
            kwargs["interaction_policy"] = policy.interaction_policy
        args = getattr(intent, "provider_args", lambda: ())()
        bound = False
        for owner in (ConnectionApplicationService, ConnectionLaunchProvider):
            for name in policy.provider_methods:
                method = getattr(owner, name, None)
                if method is None:
                    continue
                # Raises TypeError if the intent and the provider have drifted.
                inspect.signature(method).bind(
                    None, ConnectionId("demo"), *args, **kwargs
                )
                bound = True
        assert bound, f"no provider method found for {intent.kind}"


def test_policy_provider_methods_exist_on_a_real_provider():
    """A typo in a ``provider_methods`` cell would fall through to the next
    name, or to a launch failure at runtime."""

    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )
    from sshpilot.daemon.connection_launch_provider import (
        DaemonConnectionLaunchProvider as ConnectionLaunchProvider,
    )

    for kind, policy in _POLICIES.items():
        found = [
            name
            for name in policy.provider_methods
            if hasattr(ConnectionApplicationService, name)
            or hasattr(ConnectionLaunchProvider, name)
        ]
        assert found == list(policy.provider_methods), (
            f"{kind}: unknown provider method(s) "
            f"{set(policy.provider_methods) - set(found)}"
        )


# --- Server adapters ---------------------------------------------------------
#
# Nothing reached these three before: they are only callable through a live
# daemon with a registered session, so the forward regression above passed the
# whole suite. They translate a runtime's spec into a launch intent, and that
# translation is where per-kind values are chosen.


def _bare_server(forward_runtime=None):
    """A DaemonServer with only the attributes the adapters read.

    Starting a real daemon to check a spec-to-intent mapping would test the
    socket, not the mapping.
    """

    from sshpilot.daemon.server import DaemonServer

    server = object.__new__(DaemonServer)
    server._connection_service = RecordingProvider()
    server._interaction_broker = RecordingBroker()
    server._readiness_manager = None
    server._forward_runtime = forward_runtime
    return server


def test_server_terminal_adapter_requests_a_terminal_launch():
    server = _bare_server()
    server._prepare_session_launch(Spec())
    assert server._connection_service.calls[0]["method"] == "terminal"
    assert server._interaction_broker.session_calls[0]["headless"] is False


def test_server_sftp_adapter_still_asks_for_the_sftp_subsystem():
    server = _bare_server()
    argv, _env = server._prepare_sftp_launch(Spec())
    call = server._interaction_broker.session_calls[0]
    assert call["trailing_args"] == ("sftp",)
    assert call["headless"] is True
    assert argv[-1] == "sftp"


def test_server_forward_adapter_maps_the_summary_and_stays_headless():
    """The forward's shape lives in its runtime summary, not in the spec."""

    class Summary:
        type = type("T", (), {"value": "remote"})()
        bind_host = "0.0.0.0"
        bind_port = 9000
        destination_host = "internal"
        destination_port = 443

    class ForwardRuntime:
        def __init__(self):
            self.asked = []

        def get_forward(self, forward_id):
            self.asked.append(str(forward_id))
            return Summary()

    runtime = ForwardRuntime()
    server = _bare_server(runtime)
    server._prepare_forward_launch(Spec())

    assert runtime.asked == [str(Spec.session_id)]
    kwargs = server._connection_service.calls[0]["kwargs"]
    assert kwargs["forward_type"] == "remote"
    assert kwargs["bind_host"] == "0.0.0.0"
    assert kwargs["bind_port"] == 9000
    assert kwargs["destination_host"] == "internal"
    assert kwargs["destination_port"] == 443
    # The regression this file exists for.
    assert server._interaction_broker.session_calls[0]["headless"] is True


def test_forward_without_a_runtime_is_refused_not_launched():
    from sshpilot.api.errors import SshPilotError

    server = _bare_server(forward_runtime=None)
    with pytest.raises(SshPilotError):
        server._prepare_forward_launch(Spec())


def test_sessions_without_a_broker_are_refused_not_launched():
    """The daemon has no terminal: an unbrokered child's prompt is lost."""

    from sshpilot.api.errors import SshPilotError

    server = _bare_server()
    server._interaction_broker = None
    for adapter in (
        server._prepare_session_launch,
        server._prepare_sftp_launch,
    ):
        with pytest.raises(SshPilotError):
            adapter(Spec())
