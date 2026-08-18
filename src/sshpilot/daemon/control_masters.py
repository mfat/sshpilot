"""Ownership, termination, and verification of sshPilot's ControlMasters.

OpenSSH backgrounds a multiplexing master into its own session, so it is not
a child of anything we hold a handle to and it outlives both the session that
created it and the daemon that launched that session — for its whole
``ControlPersist`` window, holding an authenticated connection to the remote
host open. Quitting the application has to end those too, which means being
able to name exactly which masters are ours.

Ownership here is structural, not heuristic: sshPilot passes an explicit
``ControlPath`` under a private 0700 directory it created, so a socket in that
directory is by construction a master sshPilot asked for. It is then confirmed
per socket with ``ssh -O check``, which answers only if that socket really is
a live master, and reports its PID — giving a verifiable identity to terminate
and, afterwards, to prove terminated.

The directory is per *user*, not per daemon. A daemon started on an explicit
``--socket`` (a test fixture, a second development instance) shares it with
the user's real daemon and owns none of it, so both the daemon's own shutdown
and the GUI's forced-teardown fallback must ask the same question before
sweeping. That question is :func:`owns_default_control_master_namespace`,
defined once here so the two callers cannot drift apart.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

from .lifecycle import resolve_socket_path

logger = logging.getLogger(__name__)

# ``ssh -O check`` prints e.g. "Master running (pid=12345)".
_MASTER_PID_PATTERN = re.compile(rb"\(pid=(\d+)\)")
# Positive evidence that nothing is listening on the ControlPath: OpenSSH
# reports a refused connection or a missing/stale socket. Anything else that
# merely fails is not evidence of absence.
_ABSENT_PATTERN = re.compile(
    rb"No such file or directory"
    rb"|Connection refused"
    rb"|control socket connect|Control socket connect"
    rb"|not a control socket|No control (path|socket)",
    re.IGNORECASE,
)

DEFAULT_CONTROL_COMMAND_TIMEOUT = 2.0


def owns_default_control_master_namespace(socket_path: Optional[os.PathLike]) -> bool:
    """Whether a daemon on ``socket_path`` owns the shared master directory.

    The ControlMaster directory is per-user while daemons are per-socket, so
    only the daemon on the default socket may retire the masters in it. Any
    instance on an explicit path shares the directory with the real daemon and
    would be tearing down someone else's live sessions.
    """

    if socket_path is None:
        return False
    # A failure to resolve the default socket propagates: callers must decide
    # explicitly, because "cannot tell" is not "does not own".
    return Path(socket_path) == resolve_socket_path()


class MasterState(str, Enum):
    """What we actually know about a ControlPath.

    ABSENT requires *positive evidence* that nothing is listening. Anything
    that merely prevented us from finding out — ssh missing, a timed-out or
    failed probe, an unreadable directory, output we cannot parse — is
    UNKNOWN, never ABSENT. The distinction is load-bearing: only ABSENT
    permits unlinking a socket, and only ABSENT permits a successful quit.
    """

    LIVE = "live"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ControlMasterProbe:
    """The result of asking one ControlPath whether a master is behind it."""

    path: Path
    state: MasterState
    pid: Optional[int] = None
    reason: str = ""

    def describe(self) -> str:
        if self.state is MasterState.LIVE:
            if self.pid is None:
                return f"ControlMaster on {self.path.name}"
            return f"ControlMaster pid={self.pid} on {self.path.name}"
        return f"ControlMaster on {self.path.name} could not be checked ({self.reason})"


@dataclass(frozen=True)
class OwnedControlMaster:
    """A live master on a ControlPath sshPilot created."""

    path: Path
    pid: Optional[int]

    def describe(self) -> str:
        if self.pid is None:
            return f"ControlMaster on {self.path.name}"
        return f"ControlMaster pid={self.pid} on {self.path.name}"


def control_master_directory() -> Optional[Path]:
    """The directory sshPilot points ``ControlPath`` at, if it is resolvable.

    ``None`` means *unresolved*, not *empty*: callers must treat it as an
    inability to enumerate masters rather than as proof there are none.
    """

    try:
        from sshpilot.ssh_multiplex import socket_dir

        return Path(socket_dir())
    except Exception:
        logger.debug("Could not resolve the ControlMaster directory", exc_info=True)
        return None


def list_control_paths(directory: Path) -> Tuple[Path, ...]:
    """Every candidate ControlPath in ``directory``.

    Raises ``OSError`` when the directory cannot be listed — an unreadable
    directory is unknown territory, not an empty one. A directory that does
    not exist is genuinely empty: sshPilot creates it before the first master.
    """

    try:
        return tuple(sorted(directory / name for name in os.listdir(directory)))
    except FileNotFoundError:
        return ()


def _run_control_command(
    path: Path, action: str, *, timeout: float
) -> Optional[subprocess.CompletedProcess]:
    """Run one ``ssh -O`` command. ``None`` means it could not be completed."""
    try:
        return subprocess.run(
            [
                "ssh",
                "-o",
                f"ControlPath={path}",
                "-O",
                action,
                # The host argument only feeds token expansion, which a
                # literal ControlPath does not need.
                "sshpilot-controlmaster",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("ssh -O %s failed for %s", action, path.name, exc_info=True)
        return None


def probe_control_master(
    path: Path, *, timeout: float = DEFAULT_CONTROL_COMMAND_TIMEOUT
) -> ControlMasterProbe:
    """Ask one ControlPath whether a master is behind it.

    Only a completed ``ssh -O check`` that reports failure is evidence of
    ABSENT — that is a socket whose master is gone. A probe that could not
    run or could not finish tells us nothing, and saying "absent" there would
    unlink the socket of a master that is alive and holding a remote
    connection open.
    """

    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                f"ControlPath={path}",
                "-O",
                "check",
                # The host argument only feeds token expansion, which a
                # literal ControlPath does not need.
                "sshpilot-controlmaster",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ControlMasterProbe(
            path=path, state=MasterState.UNKNOWN, reason="the check timed out"
        )
    except FileNotFoundError:
        return ControlMasterProbe(
            path=path, state=MasterState.UNKNOWN, reason="ssh is unavailable"
        )
    except (OSError, subprocess.SubprocessError):
        return ControlMasterProbe(
            path=path, state=MasterState.UNKNOWN, reason="the check could not run"
        )

    output = (result.stderr or b"") + (result.stdout or b"")
    if result.returncode == 0:
        match = _MASTER_PID_PATTERN.search(output)
        if match is None:
            # It answered as a live master but we cannot name it. Live is the
            # safe reading; an unnameable master is still a master.
            return ControlMasterProbe(
                path=path,
                state=MasterState.LIVE,
                reason="the master did not report a pid",
            )
        return ControlMasterProbe(
            path=path, state=MasterState.LIVE, pid=int(match.group(1))
        )

    if _ABSENT_PATTERN.search(output):
        return ControlMasterProbe(path=path, state=MasterState.ABSENT)
    return ControlMasterProbe(
        path=path,
        state=MasterState.UNKNOWN,
        reason="the check failed for an unrecognized reason",
    )


def survey_control_masters(
    *, directory: Optional[Path] = None, timeout: float = DEFAULT_CONTROL_COMMAND_TIMEOUT
) -> Tuple[ControlMasterProbe, ...]:
    """Probe every ControlPath sshPilot created.

    Raises ``OSError`` when the directory itself cannot be enumerated, and
    ``LookupError`` when it cannot even be resolved. Neither is reported as
    "no masters": an unreadable directory could be hiding any number of live
    connections.
    """

    base = directory if directory is not None else control_master_directory()
    if base is None:
        raise LookupError("the ControlMaster directory could not be resolved")
    return tuple(
        probe_control_master(path, timeout=timeout) for path in list_control_paths(base)
    )


def list_owned_control_masters(
    *, directory: Optional[Path] = None, timeout: float = DEFAULT_CONTROL_COMMAND_TIMEOUT
) -> Tuple[OwnedControlMaster, ...]:
    """Every master known to be LIVE. Unknown states are *not* included here.

    Callers that must not fail open should use :func:`survey_control_masters`
    and handle UNKNOWN explicitly; this convenience view is for reporting.
    """

    return tuple(
        OwnedControlMaster(path=probe.path, pid=probe.pid)
        for probe in survey_control_masters(directory=directory, timeout=timeout)
        if probe.state is MasterState.LIVE
    )


def terminate_owned_control_masters(
    *,
    directory: Optional[Path] = None,
    timeout: float = DEFAULT_CONTROL_COMMAND_TIMEOUT,
) -> Tuple[ControlMasterProbe, ...]:
    """Ask every owned master to exit; return everything not proven gone.

    ``ssh -O exit`` terminates the master immediately and unlinks its socket.
    A ControlPath positively proven ABSENT is a crashed master's leftover and
    its socket file is removed here. A path we could not check is left
    strictly alone — unlinking it would strand a live master with an open
    authenticated connection and no way to reach it again.

    The return value is the point: anything still LIVE, or whose state is
    UNKNOWN, comes back so quit can refuse rather than assume.
    """

    probes = survey_control_masters(directory=directory, timeout=timeout)
    for probe in probes:
        if probe.state is MasterState.ABSENT:
            try:
                probe.path.unlink()
            except OSError:
                pass
        elif probe.state is MasterState.LIVE:
            _run_control_command(probe.path, "exit", timeout=timeout)

    remaining = survey_control_masters(directory=directory, timeout=timeout)
    return tuple(
        probe for probe in remaining if probe.state is not MasterState.ABSENT
    )
