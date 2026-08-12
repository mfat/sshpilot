"""Secure per-user Unix-socket path handling."""

from __future__ import annotations

import os
import socket
import stat
import tempfile
from pathlib import Path
from typing import Optional, Tuple


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
