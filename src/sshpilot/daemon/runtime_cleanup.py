"""Runtime-directory hygiene for the local daemon and askpass sockets."""

from __future__ import annotations

import logging
import os
import socket
import stat
from pathlib import Path
from typing import Iterable, Optional, Set

logger = logging.getLogger(__name__)

ASKPASS_SOCKET_PREFIX = "askpass-"
ASKPASS_SOCKET_SUFFIX = ".sock"
DAEMON_SOCKET_NAME = "sshpilotd.sock"


def resolve_runtime_directory(explicit: Optional[os.PathLike] = None) -> Path:
    """Return the per-user sshpilot runtime directory."""

    if explicit is not None:
        return Path(explicit)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "sshpilot"
    if not hasattr(os, "getuid"):
        raise RuntimeError("Unix runtime directory requires a user ID")
    return Path("/tmp") / f"sshpilot-{os.getuid()}"


def _owned_private_socket(path: Path, owner_uid: int) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        return False
    if info.st_uid != owner_uid:
        return False
    if stat.S_IMODE(info.st_mode) & 0o177:
        return False
    return True


def _socket_is_accepting(path: Path, *, timeout: float = 0.05) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        # Ambiguous — do not treat as stale.
        return True
    finally:
        try:
            probe.close()
        except OSError:
            pass
    return True


def is_askpass_socket_name(name: str) -> bool:
    return name.startswith(ASKPASS_SOCKET_PREFIX) and name.endswith(
        ASKPASS_SOCKET_SUFFIX
    )


def sweep_stale_askpass_sockets(
    runtime_dir: Optional[os.PathLike] = None,
    *,
    owner_uid: Optional[int] = None,
    protect_paths: Optional[Iterable[os.PathLike]] = None,
    probe_timeout: float = 0.05,
    limit: int = 512,
) -> int:
    """Remove owned askpass sockets that no longer accept connections.

    Returns the number of sockets unlinked. Never touches ``sshpilotd.sock``
    or paths listed in ``protect_paths``. Symlinks and foreign UIDs are skipped.
    """

    if not hasattr(os, "getuid"):
        return 0
    uid = os.getuid() if owner_uid is None else int(owner_uid)
    base = resolve_runtime_directory(runtime_dir)
    protected: Set[Path] = {Path(p).resolve() for p in (protect_paths or ())}
    removed = 0
    try:
        entries = list(base.iterdir())
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.debug("runtime cleanup: cannot list %s: %s", base, exc)
        return 0
    for entry in entries:
        if removed >= limit:
            break
        if not is_askpass_socket_name(entry.name):
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if resolved in protected:
            continue
        if not _owned_private_socket(entry, uid):
            continue
        if _socket_is_accepting(entry, timeout=probe_timeout):
            continue
        try:
            # Re-check identity before unlink (TOCTOU).
            if not _owned_private_socket(entry, uid):
                continue
            if _socket_is_accepting(entry, timeout=probe_timeout):
                continue
            entry.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.debug("runtime cleanup: unlink %s failed: %s", entry, exc)
    if removed:
        logger.info("runtime cleanup: removed %s stale askpass socket(s)", removed)
    return removed


def sweep_runtime_directory_on_startup(
    runtime_dir: Optional[os.PathLike] = None,
    *,
    daemon_socket_name: str = DAEMON_SOCKET_NAME,
    owner_uid: Optional[int] = None,
) -> int:
    """Bounded startup hygiene: stale askpass sockets only.

    The daemon socket itself is recovered by :func:`prepare_socket_path`.
    """

    base = resolve_runtime_directory(runtime_dir)
    protect = []
    daemon_sock = base / daemon_socket_name
    if daemon_sock.exists():
        protect.append(daemon_sock)
    return sweep_stale_askpass_sockets(
        base,
        owner_uid=owner_uid,
        protect_paths=protect,
    )


def terminate_orphaned_ssh_masters(*, timeout: float = 2.0) -> None:
    """Terminate ControlMasters no daemon is left to clean up.

    A daemon that exits normally retires its own masters. One that had to be
    killed cannot, and OpenSSH backgrounds a master into its own session — so
    it would otherwise keep an authenticated connection to the remote host
    alive for the rest of its ``ControlPersist`` window, after the application
    is gone. This is the sweep for exactly that case; it is a no-op when no
    master sockets remain.
    """

    try:
        from sshpilot.ssh_multiplex import expire_all_masters

        expire_all_masters(background=False, timeout=timeout, mode="exit")
    except Exception:
        logger.debug("orphaned ControlMaster sweep failed", exc_info=True)
