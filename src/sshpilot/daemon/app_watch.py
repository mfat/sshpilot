"""Stop the daemon when the app that uses it is gone.

Quitting the app stops the daemon explicitly. A crash, SIGKILL or logout
does not, and left SSH sessions, forwards and transfers running with no
window able to reach them — which also kept the daemon from idling out.

The daemon therefore watches the processes of the clients that declared an
app frontend type in their handshake. Once every watched app process has
exited, ``on_all_gone`` runs (the server issues a forced stop, exactly what a
clean quit asks for). A dropped connection alone is not enough: the app's
client closes and reopens its transport on a request timeout, so only the
process ending counts.

Clients that are not the app (the CLI, MCP servers, the diagnostics viewer)
are never watched, so a daemon used without the app keeps its normal idle
lifetime.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Frontend types whose process lifetime bounds the daemon's. A new
#: full-application frontend adds its handshake ``frontend_type`` here.
APP_FRONTEND_TYPES = frozenset({"gtk"})

DEFAULT_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class ProcessIdentity:
    """A process, pinned by creation time so a recycled PID never matches."""

    pid: int
    create_time: float


def is_app_frontend(frontend_type: object) -> bool:
    return isinstance(frontend_type, str) and frontend_type in APP_FRONTEND_TYPES


def peer_is_this_process(sock: socket.socket) -> bool:
    """A client inside the daemon's own process dies with it: nothing to watch."""

    return _peer_pid(sock) == os.getpid()


def identify_peer_process(sock: socket.socket) -> Optional[ProcessIdentity]:
    """Identify the process on the other end of ``sock``, or ``None``.

    ``None`` means the peer could not be identified (or is this process).
    The server treats an unidentified *app* peer as grounds to stop
    watching altogether, never as an exited app.
    """

    pid = _peer_pid(sock)
    if pid is None or pid == os.getpid():
        return None
    try:
        import psutil

        create_time = psutil.Process(pid).create_time()
    except Exception:
        return None
    return ProcessIdentity(pid=pid, create_time=create_time)


def _peer_pid(sock: socket.socket) -> Optional[int]:
    family = getattr(sock, "family", None)
    if family == getattr(socket, "AF_UNIX", object()):
        from sshpilot.daemon.lifecycle import peer_process_id

        return peer_process_id(sock)
    if family in (socket.AF_INET, socket.AF_INET6):
        return _tcp_peer_pid(sock)
    return None


def _tcp_peer_pid(sock: socket.socket) -> Optional[int]:
    """Owner of the loopback TCP peer (Windows transport), via the TCP table."""

    try:
        import psutil

        local = sock.getsockname()[:2]
        remote = sock.getpeername()[:2]
        own = os.getpid()
        for conn in psutil.net_connections(kind="tcp"):
            if (
                conn.pid
                and conn.pid != own
                and conn.laddr
                and conn.raddr
                and tuple(conn.laddr)[:2] == tuple(remote)
                and tuple(conn.raddr)[:2] == tuple(local)
            ):
                return int(conn.pid)
    except Exception:
        logger.debug("Could not identify the TCP peer process", exc_info=True)
    return None


def process_alive(identity: ProcessIdentity) -> bool:
    """Whether the exact process still runs (a zombie counts as exited)."""

    try:
        import psutil
    except Exception:
        return True  # cannot tell: never shut down on a guess
    try:
        process = psutil.Process(identity.pid)
        if abs(process.create_time() - identity.create_time) > 0.01:
            return False  # the PID was reused
        return process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except Exception:
        return True  # AccessDenied or unknown: never shut down on a guess


class AppProcessWatcher:
    """Calls ``on_all_gone`` once every registered app process has exited."""

    def __init__(
        self,
        on_all_gone: Callable[[Tuple[ProcessIdentity, ...]], None],
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        alive: Callable[[ProcessIdentity], bool] = process_alive,
    ) -> None:
        self._on_all_gone = on_all_gone
        self._poll_seconds = float(poll_seconds)
        self._alive = alive
        self._lock = threading.Lock()
        self._watched: Dict[int, ProcessIdentity] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._fired = False
        self._disabled = False
        self._thread: Optional[threading.Thread] = None

    def disable(self, reason: str) -> None:
        """Never fire again: an app process could not be tracked.

        With an unidentified app connected, "every watched process exited"
        no longer means the app is gone, so the daemon falls back to its
        normal idle lifetime instead of guessing.
        """

        with self._lock:
            if self._disabled or self._fired:
                return
            self._disabled = True
        logger.warning("App exit watching disabled: %s", reason)
        self._wake.set()

    @property
    def disabled(self) -> bool:
        return self._disabled

    def register(self, identity: ProcessIdentity) -> None:
        with self._lock:
            if self._fired or self._disabled or self._stop.is_set():
                return
            known = self._watched.get(identity.pid)
            if known == identity:
                return
            self._watched[identity.pid] = identity
            start = self._thread is None
            if start:
                self._thread = threading.Thread(
                    target=self._run, name="sshpilot-app-watch", daemon=True
                )
        logger.info("Watching app process pid=%s", identity.pid)
        if start:
            self._thread.start()

    def poke(self) -> None:
        """Check now (an app connection just closed)."""

        self._wake.set()

    def check(self) -> bool:
        """Drop exited processes; fire and return True when none remain."""

        with self._lock:
            if self._fired or self._disabled or not self._watched:
                return False
            watched = tuple(self._watched.values())
        gone = tuple(identity for identity in watched if not self._alive(identity))
        with self._lock:
            if self._fired or self._disabled:
                return False
            for identity in gone:
                if self._watched.get(identity.pid) == identity:
                    del self._watched[identity.pid]
            if self._watched:
                return False
            self._fired = True
        logger.warning(
            "The app exited without stopping the background service "
            "(pids=%s); ending its sessions and shutting down",
            [identity.pid for identity in gone],
        )
        try:
            self._on_all_gone(gone)
        except Exception:
            logger.exception("App-exit shutdown failed")
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._poll_seconds)
            self._wake.clear()
            if self._stop.is_set():
                return
            if self._disabled:
                return
            try:
                if self.check():
                    return
            except Exception:
                logger.debug("App process check failed", exc_info=True)


__all__ = [
    "APP_FRONTEND_TYPES",
    "AppProcessWatcher",
    "ProcessIdentity",
    "identify_peer_process",
    "is_app_frontend",
    "peer_is_this_process",
    "process_alive",
]
