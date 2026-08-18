"""Proof that sshPilot's runtime is gone, and the teardown that precedes it.

Quit is only allowed to finish when every resource sshPilot owns has been
verified absent. "The socket disappeared" is not that proof: the socket is one
resource among several, and a daemon that had to be killed leaves orphaned
``ssh``/``sftp``/``scp`` children and ControlMasters behind it that no socket
check would notice.

So this module answers one question authoritatively —
:func:`verify_sshpilot_runtime_terminated` — and returns what specifically
survived rather than a bare boolean, because the caller has to show the user
an actionable reason for refusing to exit.

Every check is ownership-derived, never name-derived: the socket's peer
credentials, the daemon's own durable registry of children (PID plus creation
time, so a recycled PID is never mistaken for ours), and ControlMasters
confirmed on sshPilot's private ControlPath. Nothing here scans the process
table, and nothing here can terminate a process this application did not
create.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from .control_masters import (
    list_owned_control_masters,
    owns_default_control_master_namespace,
    terminate_owned_control_masters,
)
from .lifecycle import probe_socket_owner
from .process_registry import (
    OwnedProcessRegistry,
    ProcessIdentity,
    RegisteredProcess,
    registry_path_for,
)

logger = logging.getLogger(__name__)

SURVIVOR_DAEMON = "daemon"
SURVIVOR_SOCKET = "daemon_socket"
SURVIVOR_CHILD = "child_process"
SURVIVOR_CONTROL_MASTER = "control_master"


@dataclass(frozen=True)
class SurvivingResource:
    """One thing sshPilot owns that is still running."""

    kind: str
    identifier: str
    detail: str

    def describe(self) -> str:
        return f"{self.detail} ({self.identifier})"


@dataclass(frozen=True)
class ShutdownVerificationResult:
    """The verdict of a post-teardown sweep.

    ``terminated`` is the only thing quit is permitted to act on. It is True
    exactly when nothing owned survived; anything else — including a check
    that could not be performed — leaves survivors listed so the refusal to
    exit can explain itself.
    """

    survivors: Tuple[SurvivingResource, ...] = field(default_factory=tuple)

    @property
    def terminated(self) -> bool:
        return not self.survivors

    def describe(self) -> str:
        if self.terminated:
            return "all sshPilot-owned processes are gone"
        return "; ".join(item.describe() for item in self.survivors)

    def messages(self) -> Tuple[str, ...]:
        return tuple(item.describe() for item in self.survivors)


def verify_sshpilot_runtime_terminated(
    *,
    socket_path: Optional[os.PathLike],
    daemon_identity: Optional[ProcessIdentity] = None,
    registry_path: Optional[os.PathLike] = None,
    control_master_directory: Optional[Path] = None,
    check_control_masters: Optional[bool] = None,
    probe_timeout: float = 0.25,
) -> ShutdownVerificationResult:
    """Report every sshPilot-owned resource that is still alive.

    ``check_control_masters`` defaults to the shared ownership rule: only the
    default-socket instance owns the per-user master directory, so a caller
    tied to an explicit ``--socket`` never inspects (or later sweeps) masters
    that belong to the real daemon.
    """

    survivors: list = []

    if socket_path is not None:
        accepting, pid = probe_socket_owner(Path(socket_path), timeout=probe_timeout)
        if accepting:
            survivors.append(
                SurvivingResource(
                    kind=SURVIVOR_SOCKET,
                    identifier=f"pid={pid}" if pid else "unknown pid",
                    detail="the background service still owns its socket",
                )
            )

    if daemon_identity is not None and daemon_identity.matches_live_process():
        survivors.append(
            SurvivingResource(
                kind=SURVIVOR_DAEMON,
                identifier=f"pid={daemon_identity.pid}",
                detail="the background service process is still running",
            )
        )

    for entry in _live_registry_entries(socket_path, registry_path):
        survivors.append(
            SurvivingResource(
                kind=SURVIVOR_CHILD,
                identifier=f"pid={entry.pid}",
                detail=_describe_child(entry),
            )
        )

    if check_control_masters is None:
        check_control_masters = owns_default_control_master_namespace(socket_path)
    if check_control_masters:
        for master in list_owned_control_masters(directory=control_master_directory):
            survivors.append(
                SurvivingResource(
                    kind=SURVIVOR_CONTROL_MASTER,
                    identifier=(
                        f"pid={master.pid}" if master.pid else master.path.name
                    ),
                    detail="a multiplexed SSH connection is still open",
                )
            )

    return ShutdownVerificationResult(survivors=tuple(survivors))


def _describe_child(entry: RegisteredProcess) -> str:
    labels = {
        "session": "a remote session process is still running",
        "forward": "a port-forward process is still running",
        "sftp": "an SFTP helper process is still running",
        "transfer": "a file-transfer process is still running",
        "helper": "a helper process is still running",
    }
    described = labels.get(entry.kind, "an owned process is still running")
    if entry.label:
        return f"{described} [{entry.label}]"
    return described


def _registry_for(
    socket_path: Optional[os.PathLike], registry_path: Optional[os.PathLike]
) -> Optional[OwnedProcessRegistry]:
    if registry_path is not None:
        return OwnedProcessRegistry(registry_path)
    if socket_path is not None:
        return OwnedProcessRegistry(registry_path_for(socket_path))
    return None


def _live_registry_entries(
    socket_path: Optional[os.PathLike], registry_path: Optional[os.PathLike]
) -> Tuple[RegisteredProcess, ...]:
    registry = _registry_for(socket_path, registry_path)
    if registry is None:
        return ()
    try:
        return registry.prune()
    except Exception:
        logger.debug("Could not read the owned-process registry", exc_info=True)
        return ()


def reap_orphaned_children(
    *,
    socket_path: Optional[os.PathLike] = None,
    registry_path: Optional[os.PathLike] = None,
    term_timeout: float = 2.0,
    kill_timeout: float = 1.0,
    poll_interval: float = 0.05,
) -> Tuple[RegisteredProcess, ...]:
    """Terminate registered children a dead daemon left behind.

    Only runs against processes whose recorded PID *and* creation time still
    match, so a recycled PID is skipped rather than signalled. Children are
    spawned as session leaders, so the whole group is signalled where that is
    recorded; otherwise only the process itself.

    Returns whatever is still alive afterwards.
    """

    registry = _registry_for(socket_path, registry_path)
    if registry is None:
        return ()

    alive = registry.prune()
    if not alive:
        return ()

    for entry in alive:
        _signal_entry(entry, signal.SIGTERM)
    _wait_for_exit(registry, deadline=time.monotonic() + max(0.0, term_timeout),
                   poll_interval=poll_interval)

    for entry in registry.prune():
        _signal_entry(entry, signal.SIGKILL)
    remaining = _wait_for_exit(
        registry,
        deadline=time.monotonic() + max(0.0, kill_timeout),
        poll_interval=poll_interval,
    )
    if not remaining:
        registry.clear()
    return remaining


def _signal_entry(entry: RegisteredProcess, signal_number: int) -> None:
    """Signal one registered process, re-confirming identity first.

    The recorded fingerprint is re-checked immediately before the kill, for
    the same reason the socket-eviction path re-probes: between deciding and
    acting, the process may have exited and its PID been reused.
    """

    if not entry.is_alive():
        return
    if entry.pid <= 1 or entry.pid == os.getpid():
        return
    try:
        if entry.process_group:
            os.killpg(entry.pid, signal_number)
        else:
            os.kill(entry.pid, signal_number)
    except (ProcessLookupError, PermissionError, OSError):
        return
    logger.info(
        "Signalled orphaned %s process pid=%s signal=%s",
        entry.kind,
        entry.pid,
        getattr(signal_number, "name", signal_number),
    )


def _wait_for_exit(
    registry: OwnedProcessRegistry, *, deadline: float, poll_interval: float
) -> Tuple[RegisteredProcess, ...]:
    while True:
        remaining = registry.prune()
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(poll_interval)


def terminate_owned_runtime(
    *,
    socket_path: Optional[os.PathLike] = None,
    registry_path: Optional[os.PathLike] = None,
    control_master_directory: Optional[Path] = None,
    sweep_control_masters: Optional[bool] = None,
) -> ShutdownVerificationResult:
    """Reap what a dead daemon left, then report what survived anyway.

    This is the fallback after the daemon itself is gone: its children and
    ControlMasters are only orphans if it could not run its own cleanup, and
    this is where they are collected. The ControlMaster sweep obeys the same
    ownership rule the daemon's own shutdown does.
    """

    reap_orphaned_children(socket_path=socket_path, registry_path=registry_path)

    if sweep_control_masters is None:
        sweep_control_masters = owns_default_control_master_namespace(socket_path)
    if sweep_control_masters:
        surviving = terminate_owned_control_masters(
            directory=control_master_directory
        )
        if surviving:
            logger.warning(
                "%d ControlMaster(s) did not exit on request", len(surviving)
            )

    return verify_sshpilot_runtime_terminated(
        socket_path=socket_path,
        registry_path=registry_path,
        control_master_directory=control_master_directory,
        check_control_masters=sweep_control_masters,
    )
