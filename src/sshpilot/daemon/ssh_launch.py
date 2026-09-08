"""The one place a daemon-owned OpenSSH child is constructed and run.

Before this module the launch stack was a set of layers -- argv composition
(:mod:`sshpilot.core.ssh.launch`), auth resolution
(:mod:`sshpilot.ssh_connection_builder`), the per-kind provider
(:mod:`sshpilot.daemon.connection_launch_provider`) and the askpass broker
(:mod:`sshpilot.daemon.interaction_broker`) -- with no entry point over them.
Each feature picked its own combination and then hand-wrote the same six
follow-up steps: the interaction policy, the askpass ``REQUIRE`` level, the
scope id convention, the ``Popen`` flags, the process-registry record, and the
``mark_authenticated()``-before-``cancel_session()`` ordering.

Nothing enforced any of it, and the cost was real: three call sites re-derived
the credential ordering in near-identical comments, one omitted it entirely
(so a remembered sudo password was discarded), and two spawned children that
never reached the on-disk process registry -- taking every Host Info probe and
every exec-mode backup transfer with them, because those delegate here.

This module is the choke point. It does not replace the layers below it; it is
the only sanctioned way to reach them. Two entry points:

``SshLauncher.open()``
    One-shot operations (scp, remote commands, ``ssh-copy-id``, broadcast,
    privileged file access). Owns composition, brokering, spawning, the
    registry record and the credential lifecycle. Callers describe intent and
    consume a process; they never receive argv/env, because that tuple is
    exactly what let every previous caller invent its own wiring.

``SshLauncher.prepare_session()``
    Long-lived sessions (terminal, SFTP, forward). These are spawned by
    session runtimes that already own their own process lifecycle and registry
    records correctly, so the launcher owns construction only -- but it owns
    it through the same policy table, so a session cannot drift from an
    operation on the axes they share.

The per-kind constants that used to live at six separate call sites are now
:data:`_POLICIES`, one reviewable table. Adding a feature means adding a row,
and no field has a default: you cannot add a row without deciding every axis.

:mod:`tests.architecture.test_ssh_launch_boundary` is what makes "must" real.
Convention without a test is what produced the drift this module removes.
"""

from __future__ import annotations

import logging
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.interactions import ExecutionInteractionMode

from .process_registry import (
    KIND_HELPER,
    KIND_FORWARD,
    KIND_SESSION,
    KIND_SFTP,
    KIND_TRANSFER,
    forget_owned_process,
    record_owned_process_or_abandon,
)

logger = logging.getLogger(__name__)


#: A launch scope is the public identity a frontend binds its interaction
#: presenter to, so it is never a private random string. Callers pass the
#: resource id the frontend already knows (TransferId, OperationId, service
#: id) and this alias records that they are all the same kind of thing.
ScopeId = SessionId


class LaunchKind(str, Enum):
    """What is being launched. Selects a row in :data:`_POLICIES`."""

    TERMINAL = "terminal"
    SFTP = "sftp"
    FORWARD = "forward"
    SCP = "scp"
    REMOTE_COMMAND = "remote_command"
    COPY_ID = "copy_id"


@dataclass(frozen=True)
class _KindPolicy:
    """Every axis a caller used to decide for itself.

    Three of these fields were previously conflated by callers into a vague
    notion of "interaction mode". They are independent and they do not
    collapse -- SFTP is ``normal`` policy *and* headless, SCP is ``broker``
    policy *and* headless, a terminal is ``normal`` and not headless:

    ``interaction_policy``
        ``normal``/``broker``/``none``; reaches ``resolve_native_auth`` and
        decides how the auth layer resolves credentials.
    ``headless``
        Sets ``SSH_ASKPASS_REQUIRE`` to ``force`` rather than ``prefer``. A
        child with no TTY must never be allowed to fall back to one.
    ``ExecutionInteractionMode``
        Whether prompts may be published at all. Per-call, not per-kind, so it
        is an argument to :meth:`SshLauncher.open` rather than a column here.

    No field has a default: a new kind must state its position on all of them.
    """

    command_type: str
    interaction_policy: str
    headless: bool
    trailing_args: Tuple[str, ...]
    registry_kind: str
    diagnostics: bool
    #: Provider methods to try in order. The ``prepare_daemon_*`` variants add
    #: a command-thread assertion and are preferred when the provider offers
    #: them; the bare names are the same call without it.
    provider_methods: Tuple[str, ...]


#: The six wirings that used to live at six call sites, as data.
_POLICIES: Mapping[LaunchKind, _KindPolicy] = {
    LaunchKind.TERMINAL: _KindPolicy(
        command_type="ssh",
        interaction_policy="normal",
        headless=False,
        trailing_args=(),
        registry_kind=KIND_SESSION,
        diagnostics=True,
        provider_methods=("prepare_daemon_terminal_launch", "prepare_terminal_launch"),
    ),
    LaunchKind.SFTP: _KindPolicy(
        command_type="sftp",
        interaction_policy="normal",
        headless=True,
        trailing_args=("sftp",),
        registry_kind=KIND_SFTP,
        diagnostics=False,
        provider_methods=("prepare_daemon_sftp_launch", "prepare_sftp_launch"),
    ),
    LaunchKind.FORWARD: _KindPolicy(
        command_type="ssh",
        interaction_policy="normal",
        # ``ssh -N -T -L ...`` is spawned with no TTY, so every prompt must
        # reach the broker; ``prefer`` would let OpenSSH try a terminal that
        # is not there and the forward would fail instead of asking.
        headless=True,
        trailing_args=(),
        registry_kind=KIND_FORWARD,
        diagnostics=False,
        provider_methods=("prepare_daemon_forward_launch", "prepare_forward_launch"),
    ),
    LaunchKind.SCP: _KindPolicy(
        command_type="scp",
        interaction_policy="broker",
        headless=True,
        trailing_args=(),
        registry_kind=KIND_TRANSFER,
        diagnostics=False,
        provider_methods=("prepare_daemon_scp_launch", "prepare_scp_launch"),
    ),
    LaunchKind.REMOTE_COMMAND: _KindPolicy(
        command_type="ssh",
        interaction_policy="broker",
        headless=True,
        trailing_args=(),
        # One-shot commands (broadcast, Host Info probes, sudo file reads,
        # authorized-key reads) are helpers, not sessions. The kind is shown
        # to the user when a child outlives the daemon, so it must describe
        # what the child actually is.
        registry_kind=KIND_HELPER,
        diagnostics=False,
        provider_methods=("prepare_remote_command_launch",),
    ),
    LaunchKind.COPY_ID: _KindPolicy(
        command_type="ssh",
        interaction_policy="broker",
        headless=True,
        trailing_args=(),
        registry_kind=KIND_HELPER,
        diagnostics=False,
        provider_methods=("prepare_copy_id_launch",),
    ),
}


# --- Intents -----------------------------------------------------------------
#
# A tagged union rather than one struct with an ``extra_args`` field. Untyped
# extra arguments are precisely the channel the old conventions travelled
# through -- forward smuggled ``-N -T <flag> <rule>``, SFTP smuggled ``-s`` --
# so a shared escape hatch would let the next feature invent its own wiring
# through the front door and still satisfy the boundary test. Flags belong to
# the provider; intents carry only what the caller genuinely chooses.


@dataclass(frozen=True)
class TerminalLaunch:
    remote_command: Optional[str] = None
    force_tty: bool = False

    kind = LaunchKind.TERMINAL

    def provider_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if self.remote_command is not None:
            kwargs["remote_command"] = self.remote_command
        if self.force_tty:
            kwargs["force_tty"] = True
        return kwargs


@dataclass(frozen=True)
class SftpLaunch:
    kind = LaunchKind.SFTP

    def provider_kwargs(self) -> Dict[str, Any]:
        return {}


@dataclass(frozen=True)
class ForwardLaunch:
    forward_type: str
    bind_host: str
    bind_port: int
    destination_host: Optional[str] = None
    destination_port: Optional[int] = None

    kind = LaunchKind.FORWARD

    def provider_kwargs(self) -> Dict[str, Any]:
        return {
            "forward_type": self.forward_type,
            "bind_host": self.bind_host,
            "bind_port": self.bind_port,
            "destination_host": self.destination_host,
            "destination_port": self.destination_port,
        }


@dataclass(frozen=True)
class ScpLaunch:
    """SCP is the one intent with caller-supplied operands.

    ``extra_args`` here is not an escape hatch for ssh flags: it is the
    transfer's own source list (plus ``-r``), assembled by the SCP backend
    from a validated request.
    """

    extra_args: Tuple[str, ...] = ()
    target_override: Optional[str] = None

    kind = LaunchKind.SCP

    def provider_kwargs(self) -> Dict[str, Any]:
        return {
            "extra_args": list(self.extra_args),
            "target_override": self.target_override,
        }


@dataclass(frozen=True)
class RemoteCommandLaunch:
    remote_command: str
    #: Hold the multiplex master for this connection, even when the
    #: ``ssh.controlmaster`` preference is off. The first command
    #: authenticates normally and becomes the master; later commands to the
    #: same host ride it instead of re-authenticating. This is how Host Info's
    #: autofill-only live samples keep working on connections whose password
    #: was typed but not stored. It reaches the provider as a parameter (not
    #: as appended argv) so the launch builder keeps owning option order:
    #: OpenSSH takes everything after the destination for the remote command,
    #: so multiplex options appended there would run on the remote host.
    #: It overrides the preference default, not an explicitly authored
    #: per-host ``ControlMaster`` directive, which stays authoritative.
    require_master: bool = False

    kind = LaunchKind.REMOTE_COMMAND

    def provider_args(self) -> Tuple[Any, ...]:
        return (self.remote_command,)

    def provider_kwargs(self) -> Dict[str, Any]:
        return {}


@dataclass(frozen=True)
class CopyIdLaunch:
    public_key_path: str
    force: bool = False

    kind = LaunchKind.COPY_ID

    def provider_args(self) -> Tuple[Any, ...]:
        return (self.public_key_path,)

    def provider_kwargs(self) -> Dict[str, Any]:
        return {"force": self.force}


LaunchIntent = Any  # one of the frozen dataclasses above


# --- Spawning ----------------------------------------------------------------


@dataclass(frozen=True)
class IoPolicy:
    """How a spawned child's standard streams are wired.

    Callers pick one of the module-level policies rather than writing
    ``Popen`` flags, which is how ``shell``/``start_new_session``/``close_fds``
    came to differ across otherwise identical call sites.
    """

    stdin: int = subprocess.DEVNULL
    stdout: int = subprocess.DEVNULL
    stderr: int = subprocess.DEVNULL
    bufsize: int = -1
    #: POSIX only; a child in its own process group can be signalled as a
    #: group when it spawns helpers of its own.
    new_process_group: bool = True
    #: Decode the child's pipes. ``ssh-copy-id`` output is shown to the user
    #: verbatim, so it is read as text with replacement rather than bytes.
    text: bool = False
    errors: Optional[str] = None


#: Capture stderr only -- SCP reports failures there and writes nothing useful
#: to stdout.
IO_STDERR_ONLY = IoPolicy(stderr=subprocess.PIPE)
#: Capture both streams, with stdin open for a payload.
IO_CAPTURE_WITH_STDIN = IoPolicy(
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
)
#: Capture both streams, no stdin.
IO_CAPTURE = IoPolicy(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
#: One interleaved text stream -- ``ssh-copy-id`` narrates to both pipes and
#: the user is shown the transcript in order.
IO_MERGED_TEXT = IoPolicy(
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    errors="replace",
)


class LaunchStartError(SshPilotError):
    """The child could not be started.

    A start failure means different things to different callers -- a transfer
    reports ``PROCESS_START_FAILED`` with the OS diagnostic, a session reports
    a startup failure -- so the scope raises this and lets the caller restate
    it in its own domain terms. :attr:`os_error` is the original cause.
    """

    def __init__(self, message: str, *, os_error: OSError, connection_id=None) -> None:
        super().__init__(
            ErrorCode.SESSION_STARTUP_FAILED, message, connection_id=connection_id
        )
        self.os_error = os_error


class PreparedLaunch:
    """One brokered argv/env pair, bound to the scope that owns its lifecycle.

    Two ways to start it, because the daemon genuinely has two kinds of
    caller:

    :meth:`spawn`
        The launcher starts the child and records it. Used by callers with no
        I/O machinery of their own.

    :attr:`argv` / :attr:`environment` + :meth:`Scope.adopt`
        For a caller whose runner owns a select loop with bounded output
        retention (broadcast, and therefore Host Info and exec-mode backup).
        Handing those two values out is safe here in a way it was not before:
        the brokering, the credential lifecycle and the ownership record all
        still belong to the scope. Only the ``Popen`` flags stay with the
        runner, and the boundary test records which files still hold them.
    """

    def __init__(self, argv: Sequence[str], environment: Mapping[str, str], scope: "LaunchScope") -> None:
        self._argv = tuple(argv)
        self._environment = dict(environment)
        self._scope = scope

    @property
    def argv(self) -> Tuple[str, ...]:
        return self._argv

    @property
    def environment(self) -> Dict[str, str]:
        return dict(self._environment)

    @property
    def target(self) -> str:
        """The OpenSSH target (``argv[-1]``), for prompts and diagnostics."""

        return self._argv[-1] if self._argv else ""

    def with_argv(self, argv: Sequence[str]) -> "PreparedLaunch":
        """A retry that rewrites flags, on the same scope and environment.

        The legacy-SCP fallback needs this. Keeping it on the scope means a
        retry can never escape the brokered credentials of the first attempt.
        """

        return PreparedLaunch(argv, self._environment, self._scope)

    def spawn(self, io: IoPolicy = IO_STDERR_ONLY) -> Any:
        return self._scope._spawn(self._argv, self._environment, io)


class LaunchScope:
    """One interaction scope, its launches, and the lifecycle around them.

    A scope may hold several launches: a broadcast runs one command across
    many hosts under a single operation id, and the frontend binds one
    interaction presenter to that id.

    The credential ordering is the point of this class. ``cancel_session``
    destroys the askpass context and clears its pending remembered secrets, so
    ``mark_authenticated`` must run first or a credential the user explicitly
    asked to save is discarded. Three call sites re-derived that in comments
    and a fourth omitted it. Here it lives in :meth:`close`, which the context
    manager always runs, and callers only report whether authentication
    succeeded.
    """

    def __init__(
        self,
        *,
        launcher: "SshLauncher",
        broker: Any,
        scope_id: ScopeId,
        connection_id: Optional[ConnectionId],
        popen: Callable[..., Any],
        registry_kind: str,
        owns_scope: bool,
    ) -> None:
        self._launcher = launcher
        self._broker = broker
        self._scope_id = scope_id
        self._connection_id = connection_id
        self._popen = popen
        self._registry_kind = registry_kind
        self._owns_scope = owns_scope
        self._authenticated = False
        self._processes: list[Any] = []
        self._closed = False

    @property
    def scope_id(self) -> ScopeId:
        return self._scope_id

    # -- preparing -----------------------------------------------------------

    def prepare(
        self,
        intent: "LaunchIntent",
        *,
        connection_id: Optional[ConnectionId] = None,
        hostname: str = "",
        username: str = "",
        port: int = 22,
        allow_stored_secrets: bool = True,
        confirm_passphrase: bool = False,
        interaction_mode: ExecutionInteractionMode = (
            ExecutionInteractionMode.INTERACTIVE
        ),
    ) -> PreparedLaunch:
        """Compose and broker one launch inside this scope."""

        if self._closed:
            raise RuntimeError("launch scope is closed")
        target_connection = connection_id or self._connection_id
        policy = _policy_for(intent)
        base_argv, base_env = self._launcher._compose(intent, target_connection, policy)
        # Only forward the credential options a caller actually asked for.
        # Sending the broker's own defaults back to it would widen the call
        # surface every collaborator has to implement, for no behaviour.
        options = {}
        if not allow_stored_secrets:
            options["allow_stored_secrets"] = False
        if confirm_passphrase:
            options["confirm_passphrase"] = True
        argv, environment = self._broker.prepare_operation_launch(
            tuple(base_argv),
            dict(base_env),
            scope_id=self._scope_id,
            connection_id=target_connection,
            hostname=hostname,
            username=username,
            port=port,
            interaction_mode=interaction_mode,
            **options,
        )
        return PreparedLaunch(argv, environment, self)

    # -- owning children -----------------------------------------------------

    def adopt(self, process: Any, *, process_group: bool = False) -> None:
        """Record *process* as daemon-owned.

        The on-disk registry is the only record that survives a daemon that
        had to be killed; it is what lets the app later prove which orphans
        are its own rather than matching on process names. Two services used
        to skip it, and everything layered on them skipped it too.
        """

        if process is None:
            return
        record_owned_process_or_abandon(
            process,
            kind=self._registry_kind,
            process_group=process_group,
        )
        self._processes.append(process)

    def _spawn(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        io: IoPolicy,
    ) -> Any:
        if self._closed:
            raise RuntimeError("launch scope is closed")
        if not argv:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH launch command is invalid",
                connection_id=self._connection_id,
            )
        process_group = bool(io.new_process_group) and os.name != "nt"
        try:
            process = self._popen(
                list(argv),
                env=dict(environment),
                shell=False,
                stdin=io.stdin,
                stdout=io.stdout,
                stderr=io.stderr,
                bufsize=io.bufsize,
                close_fds=True,
                **({"text": True} if io.text else {}),
                **({"errors": io.errors} if io.errors else {}),
                **({"start_new_session": True} if process_group else {}),
            )
        except OSError as exc:
            raise LaunchStartError(
                "The SSH command could not be started",
                os_error=exc,
                connection_id=self._connection_id,
            ) from exc
        setattr(process, "_sshpilot_process_group", process_group)
        self.adopt(process, process_group=process_group)
        return process

    # -- lifecycle -----------------------------------------------------------

    def authenticated(self) -> None:
        """Report that authentication succeeded somewhere in this scope.

        Call it after a successful run; the ordering against teardown is
        handled in :meth:`close`, not by the caller.
        """

        self._authenticated = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for process in self._processes:
            # Only drop the ownership record once the child is provably gone.
            # A process still running (or one whose state we cannot read) keeps
            # its entry: losing the record of a live child is the failure this
            # registry exists to prevent, and a stale entry is harmless because
            # pid plus creation time must both match to act on it.
            pid = getattr(process, "pid", None)
            poll = getattr(process, "poll", None)
            if pid is None or not callable(poll):
                continue
            try:
                exited = poll() is not None
            except Exception:
                exited = False
            if exited:
                forget_owned_process(pid)
        if self._authenticated:
            self._broker.mark_authenticated(self._scope_id)
        if self._owns_scope:
            self._broker.cancel_session(self._scope_id)


class SshLauncher:
    """The only sanctioned way to construct or run a daemon OpenSSH child."""

    def __init__(
        self,
        launch_provider: Any,
        interaction_broker: Any,
        *,
        readiness_manager: Any = None,
        popen: Callable[..., Any] = subprocess.Popen,
        registry_kind: Optional[str] = None,
    ) -> None:
        self._provider = launch_provider
        self._broker = interaction_broker
        self._readiness = readiness_manager
        self._popen = popen
        self._registry_kind = registry_kind

    # -- one-shot operations -------------------------------------------------

    @contextmanager
    def open(
        self,
        *,
        scope_id: ScopeId,
        connection_id: Optional[ConnectionId] = None,
        registry_kind: str = KIND_SESSION,
        owns_scope: bool = True,
    ) -> Iterator[LaunchScope]:
        """Open an interaction scope and always tear it down correctly.

        Remembered credentials are committed first whenever the caller
        reported success, and every child started or adopted inside the scope
        reaches the process registry.

        ``owns_scope=False`` marks but does not cancel. A few operations run
        *inside* a scope someone else owns: privileged sudo commands borrow
        the SFTP session's scope, because a prompt raised while editing a file
        must reach the presenter already bound to that session. Cancelling
        there would destroy a live session's askpass context, so the owner --
        the session -- cancels, and the borrower only reports success.
        """

        if self._broker is None:
            raise SshPilotError(
                ErrorCode.ASKPASS_HELPER_UNAVAILABLE,
                "Typed SSH interactions are unavailable",
                connection_id=connection_id,
            )
        scope = LaunchScope(
            launcher=self,
            broker=self._broker,
            scope_id=scope_id,
            connection_id=connection_id,
            popen=self._popen,
            registry_kind=self._registry_kind or registry_kind,
            owns_scope=owns_scope,
        )
        try:
            yield scope
        finally:
            scope.close()

    # -- long-lived sessions -------------------------------------------------

    def prepare_session(
        self, spec: Any, intent: "LaunchIntent"
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        """Build argv/env for a session runtime that owns its own process.

        Terminal, SFTP and forward sessions are spawned by runtimes that
        already register their children and cancel their scopes when the
        session ends. The launcher owns construction so those three cannot
        drift from the policy table on the axes they share with operations,
        and hands back argv/env because the spawn genuinely belongs to the
        runtime -- a PTY child needs ``pass_fds``, which no shared spawner
        can express.
        """

        if self._broker is None:
            raise SshPilotError(
                ErrorCode.ASKPASS_HELPER_UNAVAILABLE,
                "Typed SSH interactions are unavailable",
                connection_id=getattr(spec, "connection_id", None),
                session_id=getattr(spec, "session_id", None),
            )
        policy = _policy_for(intent)
        argv, environment = self._broker.prepare_launch(
            spec,
            self._session_builder(intent, policy),
            trailing_args=policy.trailing_args,
            headless=policy.headless,
        )
        if policy.diagnostics and self._readiness is not None:
            diagnostics_path = self._readiness.prepare_launch(
                spec.session_id, argv, environment
            )
            if diagnostics_path is not None:
                from .ssh_readiness import insert_ssh_diagnostics_options

                argv = insert_ssh_diagnostics_options(argv, diagnostics_path)
        return argv, environment

    # -- internals -----------------------------------------------------------

    def _session_builder(
        self, intent: "LaunchIntent", policy: _KindPolicy
    ) -> Callable[..., tuple]:
        method = self._provider_method(policy)
        extra = intent.provider_kwargs()

        def _builder(connection_id: Any, **kwargs: Any) -> tuple:
            # The broker supplies ``interaction_policy`` and the session's own
            # remote_command/force_tty; the intent supplies the rest.
            merged = dict(extra)
            merged.update(kwargs)
            return method(connection_id, **merged)

        return _builder

    def _compose(
        self,
        intent: "LaunchIntent",
        connection_id: Optional[ConnectionId],
        policy: _KindPolicy,
    ) -> Tuple[Sequence[str], Mapping[str, str]]:
        method = self._provider_method(policy)
        args = getattr(intent, "provider_args", lambda: ())()
        kwargs = dict(intent.provider_kwargs())
        # ``ssh-copy-id`` composes its own auth and takes no policy argument.
        if policy.interaction_policy and intent.kind is not LaunchKind.COPY_ID:
            kwargs["interaction_policy"] = policy.interaction_policy
        # Only sent when set: every existing provider fake spells its
        # parameters explicitly, and the default path must stay identical.
        if getattr(intent, "require_master", False):
            kwargs["require_master"] = True
        result = method(connection_id, *args, **kwargs)
        if not result:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH launch could not be prepared",
                connection_id=connection_id,
            )
        argv, environment = result
        return argv, environment

    def _provider_method(self, policy: _KindPolicy) -> Callable[..., tuple]:
        for name in policy.provider_methods:
            method = getattr(self._provider, name, None)
            if callable(method):
                return method
        raise SshPilotError(
            ErrorCode.SESSION_STARTUP_FAILED,
            "The launch provider cannot prepare this launch",
        )


def _policy_for(intent: "LaunchIntent") -> _KindPolicy:
    kind = getattr(intent, "kind", None)
    policy = _POLICIES.get(kind) if kind is not None else None
    if policy is None:
        raise SshPilotError(
            ErrorCode.INVALID_REQUEST,
            "The launch intent is not supported",
        )
    return policy


__all__ = [
    "CopyIdLaunch",
    "ForwardLaunch",
    "IO_CAPTURE",
    "IO_MERGED_TEXT",
    "IO_CAPTURE_WITH_STDIN",
    "IO_STDERR_ONLY",
    "IoPolicy",
    "LaunchKind",
    "LaunchStartError",
    "LaunchScope",
    "PreparedLaunch",
    "RemoteCommandLaunch",
    "ScopeId",
    "ScpLaunch",
    "SftpLaunch",
    "SshLauncher",
    "TerminalLaunch",
]
