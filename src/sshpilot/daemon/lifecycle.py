"""Secure per-user Unix-socket path handling."""

from __future__ import annotations

import logging
import os
import signal
import socket
import stat
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

from .process_registry import identify_process


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

# macOS ``getsockopt`` level/names for Unix-socket peer identity, from
# ``<sys/un.h>``:
#     #define SOL_LOCAL        0
#     #define LOCAL_PEERCRED   0x001   /* struct xucred */
#     #define LOCAL_PEERPID    0x002   /* pid_t */
# Linux uses SO_PEERCRED, which carries pid/uid/gid together.
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 1
_LOCAL_PEERPID = 2
# struct xucred begins: u_int cr_version; uid_t cr_uid; ...
_XUCRED_PREFIX = "=II"


def peer_credentials_supported() -> bool:
    """Whether this platform can identify the process behind a Unix socket.

    Both production platforms can, and startup self-healing depends on it: a
    daemon whose process cannot be named can neither be signalled safely nor
    have its socket unlinked safely. This is asserted as a platform invariant
    by the test suite rather than being allowed to degrade silently.
    """

    return hasattr(socket, "SO_PEERCRED") or sys.platform == "darwin"


def peer_process_id(sock: socket.socket) -> Optional[int]:
    """Return the PID behind a connected Unix socket, or ``None``.

    Linux reports pid/uid/gid together through ``SO_PEERCRED``. macOS reports
    the PID through ``LOCAL_PEERPID`` and the uid separately through
    ``LOCAL_PEERCRED``; both are consulted, so neither platform hands back a
    PID without also confirming the peer runs as this user. A PID is never
    returned for PID 0 or 1, which cannot be ours and are catastrophic to
    signal.

    ``None`` means "not identified" and is always safe: every caller treats it
    as grounds to refuse to signal, never as permission to proceed.
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

    if sys.platform != "darwin":
        return None

    if not _darwin_peer_is_this_user(sock):
        return None
    try:
        raw = sock.getsockopt(
            _SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i")
        )
        (pid,) = struct.unpack("i", raw)
    except (OSError, struct.error):
        return None
    return pid if pid > 1 else None


def _darwin_peer_is_this_user(sock: socket.socket) -> bool:
    """Confirm a macOS peer's uid via ``LOCAL_PEERCRED`` (``struct xucred``).

    A socket in a 0700 directory this user owns already implies this, so an
    unreadable xucred is not treated as a failure — but when the kernel does
    answer, a foreign uid is refused outright.
    """

    try:
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, 256)
    except OSError:
        return True
    try:
        _version, uid = struct.unpack_from(_XUCRED_PREFIX, raw, 0)
    except struct.error:
        return True
    if not hasattr(os, "getuid"):
        return True
    return uid == os.getuid()


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


def wait_until_socket_free(
    path: Path, *, timeout: float, poll_interval: float = 0.05
) -> bool:
    """Block until nothing accepts on ``path``. True when it came free."""

    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        accepting, _pid = probe_socket_owner(path, timeout=0.1)
        if not accepting:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


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

    A PID is only a name for whoever held the socket at the moment we looked,
    and the process we are evicting is by definition one that may be exiting
    right now — so the owner is re-confirmed immediately before *every*
    signal, never once up front.  If the socket has changed hands, eviction
    stops rather than signalling a number that may since have been reused by
    an unrelated process; the caller re-probes and deals with the new owner
    on its own terms.  The residual window between that confirmation and
    ``os.kill`` is one syscall wide and cannot be closed with POSIX PIDs
    alone (it would need pidfd/kqueue handles), but it is the same standard
    :func:`remove_dead_socket` holds itself to before unlinking.
    """

    expected_pid = pid
    if expected_pid is None:
        _accepting, expected_pid = probe_socket_owner(path)

    expected_identity = (
        identify_process(expected_pid) if expected_pid is not None else None
    )
    if (
        expected_pid is not None
        and expected_pid > 1
        and expected_pid != os.getpid()
        and expected_identity is not None
    ):
        for signal_number, timeout in (
            (signal.SIGTERM, term_timeout),
            (signal.SIGKILL, kill_timeout),
        ):
            accepting, current_pid = probe_socket_owner(path)
            if not accepting:
                break  # It let go; nothing left to signal.
            if not expected_identity.matches_live_process():
                # Same number, different process: the daemon exited and the
                # PID was recycled. Signalling now would hit a stranger.
                logger.info(
                    "Daemon pid=%s was recycled during eviction; not signalling",
                    expected_pid,
                )
                break
            if current_pid != expected_pid:
                # Either another process now owns the socket, or this
                # platform cannot re-confirm the owner. Both mean we can no
                # longer prove the PID still identifies the daemon we set
                # out to evict, which is the only warrant we have to kill it.
                logger.info(
                    "Daemon socket changed hands during eviction; "
                    "not signalling pid=%s",
                    expected_pid,
                )
                break
            try:
                os.kill(expected_pid, signal_number)
            except (ProcessLookupError, PermissionError, OSError):
                break
            logger.info(
                "Signalled unusable daemon pid=%s signal=%s",
                expected_pid,
                signal_number.name,
            )
            if wait_until_socket_free(
                path, timeout=timeout, poll_interval=poll_interval
            ):
                break

    accepting, _pid = probe_socket_owner(path)
    if accepting:
        return False
    return remove_dead_socket(path)
