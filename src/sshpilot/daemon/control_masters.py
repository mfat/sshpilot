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
from pathlib import Path
from typing import Optional, Tuple

from .lifecycle import resolve_socket_path

logger = logging.getLogger(__name__)

# ``ssh -O check`` prints e.g. "Master running (pid=12345)".
_MASTER_PID_PATTERN = re.compile(rb"\(pid=(\d+)\)")

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
    try:
        return Path(socket_path) == resolve_socket_path()
    except Exception:
        return False


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
    """The directory sshPilot points ``ControlPath`` at, if it is resolvable."""

    try:
        from sshpilot.ssh_multiplex import socket_dir

        return Path(socket_dir())
    except Exception:
        logger.debug("Could not resolve the ControlMaster directory", exc_info=True)
        return None


def _run_control_command(
    path: Path, action: str, *, timeout: float
) -> Optional[subprocess.CompletedProcess]:
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
) -> Optional[OwnedControlMaster]:
    """Return the live master answering on ``path``, or ``None``.

    A socket file that answers nothing is a leftover from a crashed master,
    not a surviving process.
    """

    result = _run_control_command(path, "check", timeout=timeout)
    if result is None or result.returncode != 0:
        return None
    match = _MASTER_PID_PATTERN.search(result.stderr or b"") or _MASTER_PID_PATTERN.search(
        result.stdout or b""
    )
    pid = int(match.group(1)) if match else None
    return OwnedControlMaster(path=path, pid=pid)


def list_owned_control_masters(
    *, directory: Optional[Path] = None, timeout: float = DEFAULT_CONTROL_COMMAND_TIMEOUT
) -> Tuple[OwnedControlMaster, ...]:
    """Every live master on a ControlPath sshPilot created."""

    base = directory if directory is not None else control_master_directory()
    if base is None:
        return ()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return ()
    masters = []
    for name in names:
        candidate = base / name
        master = probe_control_master(candidate, timeout=timeout)
        if master is not None:
            masters.append(master)
    return tuple(masters)


def terminate_owned_control_masters(
    *,
    directory: Optional[Path] = None,
    timeout: float = DEFAULT_CONTROL_COMMAND_TIMEOUT,
) -> Tuple[OwnedControlMaster, ...]:
    """Ask every owned master to exit; return the ones still alive after.

    ``ssh -O exit`` terminates the master immediately and unlinks its socket.
    A socket that answers nothing is unlinked here instead, since it is a
    crashed master's leftover rather than a process. The return value is the
    point of the exercise: a swallowed exception must not be mistaken for a
    master that actually went away.
    """

    base = directory if directory is not None else control_master_directory()
    if base is None:
        return ()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return ()

    for name in names:
        candidate = base / name
        master = probe_control_master(candidate, timeout=timeout)
        if master is None:
            # Nothing is listening; remove the stale socket file only.
            try:
                candidate.unlink()
            except OSError:
                pass
            continue
        _run_control_command(candidate, "exit", timeout=timeout)

    return list_owned_control_masters(directory=base, timeout=timeout)
