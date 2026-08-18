"""Durable record of the processes this daemon owns.

The daemon reaps its own children when it shuts down cleanly, and while it is
alive its ``subprocess.Popen`` handles are the authoritative ownership record.
Neither survives the case this module exists for: a daemon that had to be
killed, whose children are now orphans that no in-memory structure describes.

So every long-lived child is also written here, next to the daemon socket, and
removed when it is reaped. After the daemon is gone the file is what lets the
application prove — rather than guess — which processes it is still
responsible for, without ever matching on process names or scanning the
process table.

A bare PID cannot carry that proof: PIDs are recycled, and acting on a
recycled one means signalling a stranger. Every entry therefore records the
process creation time alongside the PID, and an entry is only ever treated as
ours when both still match. A PID that has been reused fails the comparison
and is reported as gone, which is the safe direction: the worst outcome is
that we decline to kill something.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

REGISTRY_FILE_NAME = "sshpilotd-processes.json"
_REGISTRY_VERSION = 1

# Process kinds. Stable strings: they are written to disk by one build and
# read back by another after an upgrade.
KIND_SESSION = "session"
KIND_FORWARD = "forward"
KIND_SFTP = "sftp"
KIND_TRANSFER = "transfer"
KIND_HELPER = "helper"


def registry_path_for(socket_path: os.PathLike) -> Path:
    """Return the registry file that belongs beside ``socket_path``."""

    return Path(socket_path).parent / REGISTRY_FILE_NAME


@dataclass(frozen=True)
class ProcessIdentity:
    """A PID plus the fingerprint that makes it unambiguous.

    ``create_time`` is the process start time as reported by the OS. It is the
    standard defence against PID reuse: a recycled PID belongs to a process
    that started later, so the pair never matches by accident.
    """

    pid: int
    create_time: float

    def matches_live_process(self) -> bool:
        """True when this exact process is still running.

        A zombie counts as gone. It has already exited — its connections are
        closed and it holds nothing — and it lingers only until whoever
        started it calls ``wait()``. Counting one as a survivor would block
        quit forever on a process that is, for every purpose this code cares
        about, dead.
        """

        if self.pid <= 1:
            return False
        current = _create_time(self.pid)
        if current is None:
            return False
        # Creation times are floats from the same source; compare with a
        # tolerance well under any plausible PID-recycle interval.
        if abs(current - self.create_time) >= 0.05:
            return False
        return not _is_zombie(self.pid)


def identify_process(pid: int) -> Optional[ProcessIdentity]:
    """Fingerprint a live PID, or ``None`` when it is not (or no longer) there.

    PID 0 and 1 are never identified: neither can be a process this
    application created, and both are catastrophic to signal.
    """

    if type(pid) is not int or pid <= 1:
        return None
    create_time = _create_time(pid)
    if create_time is None:
        return None
    return ProcessIdentity(pid=pid, create_time=create_time)


def _create_time(pid: int) -> Optional[float]:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        return _create_time_from_proc(pid)


def _is_zombie(pid: int) -> bool:
    """Whether ``pid`` has exited but not yet been waited for."""

    try:
        import psutil

        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except Exception:
        pass
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return False
    close = data.rfind(b")")
    if close < 0:
        return False
    fields = data[close + 2 :].split()
    return bool(fields) and fields[0] == b"Z"


def _create_time_from_proc(pid: int) -> Optional[float]:
    """Fallback for a Linux host without psutil (``/proc/<pid>/stat``)."""

    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except (OSError, ValueError):
        return None
    # The comm field is parenthesised and may itself contain spaces, so the
    # fields after it are located from the last ')'.
    close = data.rfind(b")")
    if close < 0:
        return None
    fields = data[close + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return float(fields[19])  # starttime, in clock ticks since boot
    except ValueError:
        return None


@dataclass(frozen=True)
class RegisteredProcess:
    """One owned process as recorded on disk."""

    identity: ProcessIdentity
    kind: str
    label: str
    process_group: bool

    @property
    def pid(self) -> int:
        return self.identity.pid

    def is_alive(self) -> bool:
        return self.identity.matches_live_process()

    def to_wire(self) -> dict:
        return {
            "pid": self.identity.pid,
            "create_time": self.identity.create_time,
            "kind": self.kind,
            "label": self.label,
            "process_group": self.process_group,
        }

    @classmethod
    def from_wire(cls, value: object) -> Optional["RegisteredProcess"]:
        if type(value) is not dict:
            return None
        try:
            pid = int(value["pid"])
            create_time = float(value["create_time"])
            kind = str(value["kind"])
            label = str(value.get("label", ""))
            process_group = bool(value.get("process_group", False))
        except (KeyError, TypeError, ValueError):
            return None
        if pid <= 1:
            return None
        return cls(
            identity=ProcessIdentity(pid=pid, create_time=create_time),
            kind=kind,
            label=label,
            process_group=process_group,
        )


class OwnedProcessRegistry:
    """File-backed set of the processes this daemon is responsible for.

    Writes are whole-file and atomic (``os.replace``), which is sufficient
    because exactly one daemon owns a socket directory at a time — the same
    single-instance guarantee the socket bind provides. Readers tolerate a
    missing or corrupt file by reporting nothing owned, since a registry that
    cannot be parsed must never be turned into signals.
    """

    def __init__(self, path: os.PathLike) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def register(
        self,
        pid: int,
        *,
        kind: str,
        label: str = "",
        process_group: bool = False,
    ) -> Optional[RegisteredProcess]:
        """Record a child. Returns the entry, or ``None`` if it already exited.

        Fingerprinting happens here, as close to the spawn as possible, so the
        recorded creation time belongs to the process we actually started.
        """

        identity = identify_process(pid)
        if identity is None:
            return None
        entry = RegisteredProcess(
            identity=identity,
            kind=kind,
            label=label,
            process_group=bool(process_group),
        )
        with self._lock:
            entries = [item for item in self._read() if item.pid != pid]
            entries.append(entry)
            self._write(entries)
        return entry

    def unregister(self, pid: int) -> None:
        """Drop a reaped child."""

        with self._lock:
            entries = self._read()
            remaining = [item for item in entries if item.pid != pid]
            if len(remaining) != len(entries):
                self._write(remaining)

    def entries(self) -> Tuple[RegisteredProcess, ...]:
        with self._lock:
            return tuple(self._read())

    def live_entries(self) -> Tuple[RegisteredProcess, ...]:
        """Only the entries whose exact process is still running."""

        return tuple(entry for entry in self.entries() if entry.is_alive())

    def prune(self) -> Tuple[RegisteredProcess, ...]:
        """Drop entries whose process is gone. Returns what is still alive."""

        with self._lock:
            entries = self._read()
            alive = [entry for entry in entries if entry.is_alive()]
            if len(alive) != len(entries):
                self._write(alive)
            return tuple(alive)

    def clear(self) -> None:
        with self._lock:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.debug("Could not remove the process registry", exc_info=True)

    # -- storage ---------------------------------------------------------
    def _read(self) -> list:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError:
            logger.debug("Could not read the process registry", exc_info=True)
            return []
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Ignoring an unreadable owned-process registry")
            return []
        if type(document) is not dict or document.get("version") != _REGISTRY_VERSION:
            return []
        raw_entries = document.get("entries")
        if type(raw_entries) is not list:
            return []
        entries = []
        for item in raw_entries:
            entry = RegisteredProcess.from_wire(item)
            if entry is not None:
                entries.append(entry)
        return entries

    def _write(self, entries: Iterable[RegisteredProcess]) -> None:
        document = {
            "version": _REGISTRY_VERSION,
            "entries": [entry.to_wire() for entry in entries],
        }
        payload = json.dumps(document).encode("utf-8")
        temporary = self._path.with_name(self._path.name + ".tmp")
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            try:
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
            os.replace(temporary, self._path)
        except OSError:
            logger.debug("Could not write the process registry", exc_info=True)
            try:
                temporary.unlink()
            except OSError:
                pass


# The daemon process installs its registry here so the runtimes that spawn
# children can reach it without threading it through every constructor. It
# stays ``None`` in the GTK process and in unit tests that never spawn.
_active_registry: Optional[OwnedProcessRegistry] = None
_active_lock = threading.Lock()


def set_active_registry(registry: Optional[OwnedProcessRegistry]) -> None:
    global _active_registry
    with _active_lock:
        _active_registry = registry


def active_registry() -> Optional[OwnedProcessRegistry]:
    with _active_lock:
        return _active_registry


def record_owned_process(
    pid: int, *, kind: str, label: str = "", process_group: bool = False
) -> None:
    """Register a child with the active registry, if one is installed."""

    registry = active_registry()
    if registry is None:
        return
    try:
        registry.register(pid, kind=kind, label=label, process_group=process_group)
    except Exception:
        logger.debug("Could not record an owned process", exc_info=True)


def forget_owned_process(pid: int) -> None:
    registry = active_registry()
    if registry is None:
        return
    try:
        registry.unregister(pid)
    except Exception:
        logger.debug("Could not forget an owned process", exc_info=True)
