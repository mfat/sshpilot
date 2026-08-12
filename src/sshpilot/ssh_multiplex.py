"""Shared ControlMaster policy used by the daemon's native launch path.

The daemon injects the options into OpenSSH commands and retires stale masters
after SSH override changes. Frontend acquire/release pooling is intentionally
gone; plugin compatibility methods remain inert in ``PluginContext``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import List

from sshpilot.daemon.lifecycle import ensure_private_runtime_directory

logger = logging.getLogger(__name__)

# Default master idle lifetime. The Docker poll (~3 s) keeps it warm; after a tab
# closes the master lingers this long before expiring (release() also tears it
# down explicitly via ``ssh -O exit``).
DEFAULT_PERSIST = "60"


def socket_dir() -> str:
    """Directory holding the control sockets (created if missing).

    Prefers ``$XDG_RUNTIME_DIR/sshpilot/cm`` (tmpfs, auto-cleaned at logout); falls
    back to ``~/.ssh/sockets``. Kept short so the socket path stays under the
    ~104-char ``sun_path`` limit once ssh appends the ``%C`` hash.

    The runtime-dir branch shares ``$XDG_RUNTIME_DIR/sshpilot`` with the daemon
    socket, whose launcher refuses a parent that is not exactly mode 0700 — so
    both levels are enrolled in the shared private-directory rule (create with
    0700, tighten an existing owned dir created under a loose umask) instead
    of a bare ``makedirs``, which would be normalized by the umask to 0755/0775
    and block the next daemon launch with ``unsafe_socket``.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        root = os.path.join(runtime, "sshpilot")
        base = os.path.join(root, "cm")
        try:
            ensure_private_runtime_directory(Path(root))
            ensure_private_runtime_directory(Path(base))
        except OSError:
            pass
        return base
    base = os.path.expanduser("~/.ssh/sockets")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    return base


def control_path() -> str:
    """The ``ControlPath`` value. ``%C`` is ssh's per-connection hash (~40 hex),
    so it is unique per host and short."""
    return os.path.join(socket_dir(), "%C")


def controlmaster_args(persist: str = DEFAULT_PERSIST) -> List[str]:
    """The ssh ``-o`` options that enable multiplexing on the shared socket."""
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path()}",
        "-o", f"ControlPersist={persist}",
    ]


def expire_all_masters(*, background: bool = True) -> None:
    """Gracefully retire every live ControlMaster after a global SSH-settings
    change (Preferences ▸ ssh_overrides), so new connections negotiate fresh
    masters with the new options instead of riding stale transports.

    ``ssh -O stop`` connects straight to each socket file (the host argument
    only feeds token expansion, which a literal ControlPath doesn't need):
    the master stops accepting new clients and unlinks its socket while live
    sessions drain naturally. Sockets that don't answer are stale leftovers
    (crashed master) and are unlinked here. Best-effort throughout.
    """
    def _stop_all() -> None:
        try:
            entries = os.listdir(socket_dir())
        except OSError:
            return
        for name in entries:
            path = os.path.join(socket_dir(), name)
            try:
                result = subprocess.run(
                    ["ssh", "-o", f"ControlPath={path}", "-O", "stop",
                     "sshpilot-invalidate"],
                    capture_output=True, timeout=10, check=False)
                if result.returncode != 0 and os.path.exists(path):
                    os.unlink(path)  # stale socket, master already gone
            except Exception:
                logger.debug("expire_all_masters: %s not stopped", path,
                             exc_info=True)

    if background:
        threading.Thread(target=_stop_all, daemon=True,
                         name="ssh-mux-expire").start()
    else:
        _stop_all()
