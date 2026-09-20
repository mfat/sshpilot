"""Frontend-neutral notices for the launcher's pre-connection command step.

A connection may carry a *pre-connection command* -- a local shell string run
just before SSH dials out, almost always a port knock (``fwknop``/``knock``)
or a VPN dial-up that authorises a short access window. The daemon launcher
owns it (:mod:`sshpilot.daemon.pre_connection_command`), and it runs for every
launch kind, not just terminals.

The step **never fails a launch**: SSH's own error tells the user far more
than a pre-step veto, which is the behaviour this feature shipped with. So
these notices are not failures in the domain sense and deliberately do not
travel through ``SessionFailure``/``SftpFailure``/``ScpFailure`` or any other
per-domain failure vocabulary -- a pre-command that exits non-zero must not
make a connection that then succeeds look like it failed.

What travels is a stable reason code plus numbers. The daemon never sends the
command text, its output, or a rendered sentence: the command line can embed a
token or a password, and the wording belongs to the frontend
(``sshpilot.gtk.pre_command_messages``), which owns translation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .common import ConnectionId, require_identifier


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
