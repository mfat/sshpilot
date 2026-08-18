"""Secure per-user Unix-socket path handling."""

from __future__ import annotations

import logging
import os
import signal
import socket
import stat
import struct
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple


logger = logging.getLogger(__name__)


class SocketSecurityError(RuntimeError):
    """The requested socket location does not meet local-user security rules."""


class DaemonAlreadyRunningError(RuntimeError):
    """A process is already accepting connections on the daemon socket."""


def resolve_socket_path(explicit: Optional[os.PathLike] = None) -> Path:
    """Return the Linux per-user daemon socket path.

    Production prefers ``$XDG_RUNTIME_DIR``. The UID-specific temporary
    fallback is retained for development and platforms without a runtime dir;
    its child directory is still created and verified as mode 0700.
    """

    if explicit is not None:
        return Path(explicit)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "sshpilot" / "sshpilotd.sock"
    if not hasattr(os, "getuid"):
        raise SocketSecurityError("Unix-domain daemon transport requires a user ID")
    return Path(tempfile.gettempdir()) / f"sshpilot-{os.getuid()}" / "sshpilotd.sock"


def ensure_secure_socket_directory(path: Path) -> None:
    """Create or validate the socket parent as an owned mode-0700 directory."""

    parent = path.parent
    try:
        parent.lstat()
        created = False
    except FileNotFoundError:
        try:
            parent.mkdir(mode=0o700, parents=False, exist_ok=False)
            created = True
        except FileExistsError:
            # A concurrent creator won; validate it as pre-existing.
            created = False
        except FileNotFoundError:
            # An explicit nested path must have an intentional existing parent.
            raise SocketSecurityError(
                "The daemon socket parent does not exist"
            ) from None
        except OSError as exc:
            raise SocketSecurityError(
                "The daemon socket directory is unavailable"
            ) from exc
    if created:
        os.chmod(parent, 0o700)
    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SocketSecurityError("The daemon socket parent must be a real directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SocketSecurityError("The daemon socket directory has the wrong owner")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise SocketSecurityError("The daemon socket directory must use mode 0700")


def ensure_private_runtime_directory(path: Path) -> None:
    """Create or tighten ``path`` itself as an owned mode-0700 directory.

    The shared rule for the per-user runtime tree (``$XDG_RUNTIME_DIR/sshpilot``
    and its ``cm`` child), used by the daemon, the askpass prompt server, and
    SSH ControlMaster socket placement. Unlike
    :func:`ensure_secure_socket_directory` — the daemon's strict launch gate,
    which only *rejects* wrong modes — this repairs an already-created
    directory back to 0700, but only after confirming it is a real directory
    owned by the current user, so a misplaced ``makedirs`` under a loose umask
    cannot poison the tree or the launcher's validation.
    """

    try:
        path.lstat()
        created = False
    except FileNotFoundError:
        try:
            # OSError from here (read-only runtime, racy parent removal)
            # propagates unchanged so soft-fail callers keep their contract.
            path.mkdir(mode=0o700, parents=False, exist_ok=False)
            created = True
        except FileExistsError:
            # A concurrent creator won; validate it as pre-existing.
            created = False
    if created:
        os.chmod(path, 0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SocketSecurityError("The runtime path must be a real directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SocketSecurityError("The runtime directory has the wrong owner")
    if stat.S_IMODE(info.st_mode) != 0o700:
        # Ownership and type are verified, so tightening is safe.
        os.chmod(path, 0o700, follow_symlinks=False)


def validate_client_socket_path(path: Path) -> bool:
    """Validate a client endpoint without unlinking it.

    Returns ``True`` when an owned private Unix socket exists and ``False`` when
    the secure parent exists but the endpoint does not.  This check deliberately
    gives the GTK launcher no authority to recover stale sockets; that remains
    the daemon's bind-time responsibility.
    """

    ensure_secure_socket_directory(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise SocketSecurityError("The daemon endpoint must be a real Unix socket")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SocketSecurityError("The daemon socket has the wrong owner")
    if stat.S_IMODE(info.st_mode) & 0o177:
        raise SocketSecurityError("The daemon socket must not be group/world accessible")
    return True


def prepare_socket_path(path: Path, *, probe_timeout: float = 0.2) -> None:
    """Refuse unsafe targets and unlink only a verified stale socket."""

    ensure_secure_socket_directory(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISSOCK(before.st_mode):
        raise SocketSecurityError("Refusing to replace a non-socket daemon path")
    if hasattr(os, "getuid") and before.st_uid != os.getuid():
        raise SocketSecurityError("The daemon socket has the wrong owner")

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(probe_timeout)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        pass
    except OSError:
        # An ambiguous live/path failure is not sufficient authority to unlink.
        raise DaemonAlreadyRunningError("The daemon socket is already in use") from None
    else:
        raise DaemonAlreadyRunningError("The daemon is already running")
    finally:
        probe.close()

    try:
        after = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISSOCK(after.st_mode)
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise SocketSecurityError("The daemon socket changed during stale-path recovery")
    path.unlink()


def verify_bound_socket(path: Path) -> Tuple[int, int]:
    """Verify ownership/type/mode and return the created socket identity."""

    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise SocketSecurityError("The bound daemon path is not a socket")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SocketSecurityError("The bound daemon socket has the wrong owner")
    if stat.S_IMODE(info.st_mode) & 0o177:
        raise SocketSecurityError("The daemon socket must not be group/world accessible")
    return info.st_dev, info.st_ino


def unlink_owned_socket(path: Path, identity: Optional[Tuple[int, int]]) -> None:
    """Remove only the exact socket created by this daemon instance."""

    if identity is None:
        return
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISSOCK(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and (info.st_dev, info.st_ino) == identity
    ):
        path.unlink()


# ---------------------------------------------------------------------------
# Peer identification and eviction.
#
# A daemon that cannot serve this build (different API implementation, wedged
# before the handshake, or dead but still holding a socket file) must never be
# allowed to make application startup fail.  The launcher and the quit path
# both need to replace such a peer, which means identifying the process behind
# the socket and, as a last resort, signalling it.
#
# Authority to do that comes from the socket's own location: the parent
# directory is validated as a mode-0700 directory owned by this user, so any
# process accepting there is this user's own sshPilot daemon.
# ---------------------------------------------------------------------------

# macOS ``getsockopt`` level/name for the peer PID of a Unix socket
# (``<sys/un.h>``: SOL_LOCAL / LOCAL_PEERPID).  Linux uses SO_PEERCRED.
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2


def peer_process_id(sock: socket.socket) -> Optional[int]:
    """Return the PID behind a connected Unix socket, or ``None``.

    Linux reports pid/uid/gid through ``SO_PEERCRED``; macOS reports the pid
    alone through ``LOCAL_PEERPID``.  Where the uid is available it must match
    this user, so a pid is never returned for a peer we would not be entitled
    to signal.  Returns ``None`` on any platform that reports neither.
    """

    if hasattr(socket, "SO_PEERCRED"):
        try:
            raw = sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            pid, uid, _gid = struct.unpack("3i", raw)
        except (OSError, struct.error):
            return None
        if hasattr(os, "getuid") and uid != os.getuid():
            return None
        return pid if pid > 1 else None
    try:
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i"))
        (pid,) = struct.unpack("i", raw)
    except (OSError, struct.error):
        return None
    return pid if pid > 1 else None


def probe_socket_owner(
    path: Path, *, timeout: float = 0.25
) -> Tuple[bool, Optional[int]]:
    """Return ``(accepting, peer_pid)`` for a daemon socket.

    ``accepting`` is True only when a process completed a connection, so a
    leftover socket *file* whose owner has died reports ``(False, None)``.
    """

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(max(0.01, float(timeout)))
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError, NotADirectoryError):
        return False, None
    except OSError:
        # Ambiguous: something is there but would not complete a connection.
        # Treat it as live so callers never unlink on a guess.
        return True, None
    else:
        return True, peer_process_id(probe)
    finally:
        probe.close()


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _wait_until_socket_free(path: Path, *, deadline: float, poll: float) -> bool:
    while True:
        accepting, _pid = probe_socket_owner(path, timeout=0.1)
        if not accepting:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def remove_dead_socket(path: Path) -> bool:
    """Unlink a socket file that no process is accepting on.

    Returns True when the path is gone afterwards.  Refuses to touch anything
    that is not an owned Unix socket, and re-probes immediately before the
    unlink so a daemon that just claimed the path is never removed.
    """

    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        return False
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return False
    accepting, _pid = probe_socket_owner(path)
    if accepting:
        return False
    try:
        after = path.lstat()
    except FileNotFoundError:
        return True
    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
        # Replaced between the probe and now — that is a live daemon's socket.
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def evict_socket_owner(
    path: Path,
    *,
    pid: Optional[int] = None,
    term_timeout: float = 2.0,
    kill_timeout: float = 1.0,
    poll_interval: float = 0.05,
) -> bool:
    """Force the process holding ``path`` to release it, then clear the path.

    The escalation is SIGTERM, then SIGKILL, then unlink of the orphaned
    socket file.  Callers are expected to have already tried the graceful
    ``daemon.stop`` RPC; this is the fallback for a peer that will not or
    cannot answer it.  Returns True when nothing accepts on ``path`` and the
    stale socket file (if any) has been removed.
    """

    if pid is None:
        _accepting, pid = probe_socket_owner(path)

    if pid is not None and pid != os.getpid() and _process_is_alive(pid):
        for signal_number, timeout in (
            (signal.SIGTERM, term_timeout),
            (signal.SIGKILL, kill_timeout),
        ):
            try:
                os.kill(pid, signal_number)
            except (ProcessLookupError, PermissionError, OSError):
                break
            logger.info(
                "Signalled unusable daemon pid=%s signal=%s",
                pid,
                signal_number.name,
            )
            if _wait_until_socket_free(
                path,
                deadline=time.monotonic() + max(0.0, float(timeout)),
                poll=poll_interval,
            ):
                break

    accepting, _pid = probe_socket_owner(path)
    if accepting:
        return False
    return remove_dead_socket(path)
