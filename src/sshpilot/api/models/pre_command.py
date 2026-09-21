"""Frontend-neutral notices for the launcher's pre-connection command step.

A connection may carry a *pre-connection command* -- a local shell string run
just before SSH dials out, almost always a port knock (``fwknop``/``knock``)
or a VPN dial-up that authorises a short access window. The daemon launcher
owns it (:mod:`sshpilot.daemon.pre_connection_command`), and it runs for every
launch kind, not just terminals.

By default the step **does not fail a launch**: SSH's own error tells the user
far more than a pre-step veto, which is the behaviour this feature shipped
with. A connection may opt into the opposite with
:attr:`PreCommandSettings.abort_on_failure`, and the notice then reports
:attr:`PreConnectionCommandNotice.aborted` so the frontend does not tell
someone the connection continues when it did not.

Either way these notices are not failures in the domain sense and deliberately
do not travel through ``SessionFailure``/``SftpFailure``/``ScpFailure`` or any
other per-domain failure vocabulary -- a pre-command that exits non-zero must
not make a connection that then succeeds look like it failed.

What travels is a stable reason code plus numbers. The daemon never sends the
command text, its output, or a rendered sentence: the command line can embed a
token or a password, and the wording belongs to the frontend
(``sshpilot.gtk.pre_command_messages``), which owns translation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional

from .common import ConnectionId, require_identifier


#: Metadata keys the pre-connection command occupies on a connection. It
#: lives in ``connections.json`` metadata rather than as a comment in
#: ``~/.ssh/config``: it is not an SSH directive, and a comment shaped like one
#: invites the reader to believe OpenSSH honours it. Wake-on-LAN -- the same
#: kind of thing, an app-owned action that runs before connecting -- already
#: stores itself this way, and metadata applies to every protocol, so SSH and
#: plugin connections need no separate arrangement.
PRE_COMMAND_METADATA_KEYS = ("pre_command", "pre_command_timeout", "pre_command_abort")


@dataclass(frozen=True)
class PreCommandSettings:
    """One connection's pre-connection command and how to run it."""

    command: str = ""
    #: Seconds before the command is killed. ``0`` defers to the daemon-wide
    #: default, so a connection that never set one follows the app.
    timeout: int = 0
    #: Refuse the launch when the command does not succeed. Off by default:
    #: SSH's own error tells the user far more than a pre-step veto, which is
    #: the behaviour this feature shipped with. On, it is a hard gate.
    abort_on_failure: bool = False

    def __post_init__(self) -> None:
        if type(self.command) is not str:
            raise TypeError("pre-connection command must be a string")
        if type(self.timeout) is not int or isinstance(self.timeout, bool):
            raise TypeError("pre-connection command timeout must be an integer")
        if self.timeout < 0:
            raise ValueError("pre-connection command timeout must not be negative")
        if type(self.abort_on_failure) is not bool:
            raise TypeError("pre-connection command abort flag must be a boolean")

    @property
    def configured(self) -> bool:
        return bool(self.command.strip())

    @classmethod
    def from_metadata(cls, values) -> "PreCommandSettings":
        """Read the settings out of one connection's metadata mapping.

        Tolerant by design: metadata is a free-form JSON mapping, so a value
        of the wrong shape reads as "unset" rather than failing a launch.
        """
        values = values if isinstance(values, Mapping) else {}
        command = values.get("pre_command")
        command = command.strip() if isinstance(command, str) else ""
        timeout = values.get("pre_command_timeout")
        if type(timeout) is not int or isinstance(timeout, bool) or timeout < 0:
            timeout = 0
        abort = values.get("pre_command_abort")
        return cls(
            command=command,
            timeout=timeout,
            abort_on_failure=abort is True,
        )


def expand_pre_command_tokens(
    command: str, *, hostname: str = "", port: object = "", username: str = ""
) -> str:
    """Expand ``%h``/``%p``/``%u`` in *command*, shell-quoted.

    The same three tokens OpenSSH uses, so the vocabulary is one a user of
    this app already knows. Without them a knock has to hardcode the host it
    is knocking for, and then rots silently the first time the connection is
    re-pointed or renamed.

    Values are shell-quoted because the result is handed to ``sh -lc``: a
    hostname is the user's own configuration rather than hostile input, but a
    space or a quote in one should stay a hostname rather than becoming
    another argument. ``%%`` is a literal percent, and an unknown ``%x`` is
    left alone -- ``date +%H`` must survive being written here.
    """
    import shlex

    if not isinstance(command, str) or "%" not in command:
        return command if isinstance(command, str) else ""
    replacements = {
        "h": shlex.quote(str(hostname or "")),
        "p": shlex.quote(str(port or "")),
        "u": shlex.quote(str(username or "")),
        "%": "%",
    }
    out = []
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if char != "%" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        token = command[index + 1]
        if token in replacements:
            out.append(replacements[token])
            index += 2
        else:
            # Not ours. Leave it exactly as written -- strftime and printf
            # formats are ordinary things to find in a shell command.
            out.append(char)
            index += 1
    return "".join(out)


@dataclass(frozen=True)
class PreCommandTestResult:
    """What happened when the user asked to try the command now.

    Unlike a launch notice this *does* carry output: the user asked to see it,
    it is on screen only for them, and a knock that fails is usually only
    diagnosable from what the tool printed. It is still bounded, and the
    daemon never logs it above DEBUG.
    """

    reason: "PreCommandReason"
    exit_code: Optional[int] = None
    duration_ms: int = 0
    output: str = ""

    MAX_OUTPUT_CHARS = 4000

    def __post_init__(self) -> None:
        if not isinstance(self.reason, PreCommandReason):
            raise TypeError("reason must be a PreCommandReason")
        if self.exit_code is not None and (
            type(self.exit_code) is not int or isinstance(self.exit_code, bool)
        ):
            raise TypeError("exit_code must be an integer or None")
        if (
            type(self.duration_ms) is not int
            or isinstance(self.duration_ms, bool)
            or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must not be negative")
        if type(self.output) is not str:
            raise TypeError("output must be a string")
        if len(self.output) > self.MAX_OUTPUT_CHARS:
            raise ValueError("test output exceeds the size limit")

    @property
    def succeeded(self) -> bool:
        return self.reason is PreCommandReason.OK


class PreCommandLaunchKind(str, Enum):
    """Which kind of launch the pre-connection command is running for.

    Mirrors ``sshpilot.daemon.ssh_launch.LaunchKind`` value for value. It is
    restated here because the API layer must not import the daemon, and
    ``tests/daemon/test_pre_connection_command.py`` asserts the two sets stay
    identical so the copy cannot drift.

    The frontend routes on it: a terminal notice belongs on the tab, an SFTP
    notice on the file-manager pane, and a forward or helper notice has no
    surface of its own and falls back to a window toast.
    """

    TERMINAL = "terminal"
    SFTP = "sftp"
    FORWARD = "forward"
    SCP = "scp"
    REMOTE_COMMAND = "remote_command"
    COPY_ID = "copy_id"


class PreCommandPhase(str, Enum):
    """Two phases, so the UI is a trivial state machine.

    Show progress on :attr:`RUNNING`, clear it on :attr:`FINISHED`, and alert
    when a finished notice carries a reason other than
    :attr:`PreCommandReason.OK`.
    """

    RUNNING = "running"
    FINISHED = "finished"


class PreCommandReason(str, Enum):
    """Why a finished pre-connection command finished.

    :attr:`OK` is also what a :attr:`PreCommandPhase.RUNNING` notice carries,
    since nothing has gone wrong yet.
    """

    OK = "ok"
    NONZERO_EXIT = "nonzero_exit"
    TIMED_OUT = "timed_out"
    START_FAILED = "start_failed"
    #: Reused a recent run instead of executing again. Not a failure, and not
    #: presented -- a coalesced notice exists so a frontend that showed the
    #: running state can clear it.
    COALESCED = "coalesced"


@dataclass(frozen=True)
class PreConnectionCommandNotice:
    """One pre-connection command lifecycle notice.

    :attr:`scope_id` is the launch scope the frontend already knows -- the
    session, SFTP service, forward, transfer or operation id -- so a surface
    bound to that scope can claim the notice the same way an interaction
    presenter claims a prompt. A frontend that has not bound the scope yet
    simply does not show the inline status; the failure alert is raised from
    the application-level subscription instead, so it can never be missed.
    """

    connection_id: ConnectionId
    scope_id: str
    kind: PreCommandLaunchKind
    phase: PreCommandPhase
    reason: PreCommandReason = PreCommandReason.OK
    #: Exit status of the shell, when it ran to completion.
    exit_code: Optional[int] = None
    #: Wall-clock duration of the attempt. Zero for a coalesced notice.
    duration_ms: int = 0
    #: The launch was refused because this command did not succeed and the
    #: connection asked for that. Only ever true on a finished, failed notice.
    aborted: bool = False
    #: What the command printed, carried only when it failed.
    #:
    #: A shell shows you a failing knock's own words; the terminal tab is
    #: where a user of this app is already looking, so the same output goes
    #: there. Success is silent, exactly as it is in a shell -- a clean
    #: ``knock`` prints nothing -- which also keeps the wire lean and means
    #: output crosses only when it is the thing being asked about.
    output: str = ""

    MAX_OUTPUT_CHARS = 4000

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        require_identifier(self.scope_id, "pre-connection command scope id")
        if not isinstance(self.kind, PreCommandLaunchKind):
            raise TypeError("kind must be a PreCommandLaunchKind")
        if not isinstance(self.phase, PreCommandPhase):
            raise TypeError("phase must be a PreCommandPhase")
        if not isinstance(self.reason, PreCommandReason):
            raise TypeError("reason must be a PreCommandReason")
        if self.phase is PreCommandPhase.RUNNING:
            # A running command has not finished, so it cannot have a verdict
            # or an exit status yet. Allowing one would let a frontend render
            # an outcome that is still being decided.
            if self.reason is not PreCommandReason.OK:
                raise ValueError("a running pre-command notice carries no reason")
            if self.exit_code is not None:
                raise ValueError("a running pre-command notice carries no exit code")
        if self.exit_code is not None and (
            type(self.exit_code) is not int or isinstance(self.exit_code, bool)
        ):
            raise TypeError("exit_code must be an integer or None")
        if (
            type(self.duration_ms) is not int
            or isinstance(self.duration_ms, bool)
            or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must not be negative")
        if type(self.aborted) is not bool:
            raise TypeError("aborted must be a boolean")
        if type(self.output) is not str:
            raise TypeError("output must be a string")
        if len(self.output) > self.MAX_OUTPUT_CHARS:
            raise ValueError("pre-command notice output exceeds the size limit")
        if self.output and (
            self.phase is not PreCommandPhase.FINISHED
            or self.reason in (PreCommandReason.OK, PreCommandReason.COALESCED)
        ):
            # Output is carried to explain a failure. Attaching it to anything
            # else would put a command's words on screen for a connection that
            # is working.
            raise ValueError("only a failed pre-command notice carries output")
        if self.aborted and self.reason in (
            PreCommandReason.OK,
            PreCommandReason.COALESCED,
        ):
            # Nothing went wrong, so nothing can have been refused. Allowing
            # it would let a frontend announce a cancelled connection that is
            # in fact opening.
            raise ValueError("a successful pre-command notice cannot be aborted")
