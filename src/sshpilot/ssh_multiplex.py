"""Shared ControlMaster policy used by the daemon's native launch path.

The daemon injects the options into OpenSSH commands and retires stale masters
after SSH override changes. Frontend acquire/release pooling is intentionally
gone; plugin compatibility methods remain inert in ``PluginContext``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import List

from sshpilot.daemon.lifecycle import ensure_private_runtime_directory

logger = logging.getLogger(__name__)

# Default master idle lifetime. The Docker poll (~3 s) keeps it warm; after a tab
# closes the master lingers this long before expiring (release() also tears it
# down explicitly via ``ssh -O exit``).
DEFAULT_PERSIST = "60"

#: Options for every ``ssh -O`` aimed at a literal ControlPath. Each one closes a
#: way for the user's environment to turn a socket check into something else:
#:
#: * ``-F <devnull>`` ignores the user and system ssh_config, which can carry
#:   ``ControlMaster yes``/``ask`` (``-O`` then skips the socket entirely --
#:   issue #1361), ``ProxyJump``, ``Match exec`` and the like.
#: * ``ControlMaster=no`` keeps the mux client path explicit.
#: * ``ProxyCommand=false``: when the socket accepts but the mux handshake
#:   fails, OpenSSH falls through to a *real* connection to the placeholder
#:   host. A DNS search domain can make that name resolve, so without this the
#:   check opens a network connection (or hangs until its timeout). With a
#:   proxy command ssh never resolves the host itself, and ``false`` exits at
#:   once, so the fallback fails locally and immediately.
CONTROL_COMMAND_OPTIONS = (
    "-F", os.devnull,
    "-o", "ControlMaster=no",
    "-o", "ProxyCommand=false",
)


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


def output_shows_no_master(output: bytes) -> bool:
    """Whether failed ``ssh -O`` output positively shows nothing is listening.

    Only a refused connection or a missing socket counts. A failed mux
    handshake, a DNS error from the placeholder host, or anything
    unrecognised does not: something may still be behind the socket.
    """
    return bool(_ABSENT_PATTERN.search(output or b""))


#: OpenSSH for Windows has no connection multiplexing (no Unix sockets), and a
#: ControlMaster option makes its launches fail outright.
MULTIPLEXING_SUPPORTED = sys.platform != "win32"


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

    On Windows nothing is ever multiplexed; the directory exists only so the
    master surveys and shutdown sweep resolve it, and it stays empty.
    """
    if not MULTIPLEXING_SUPPORTED:
        from sshpilot.platform.paths import get_runtime_dir

        base = get_runtime_dir() / "cm"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return str(base)
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
    """The ssh ``-o`` options that enable multiplexing on the shared socket.

    Where multiplexing is unsupported this pins it *off* instead, so a forced
    master (Host Info) or a Host block carrying ``ControlMaster`` cannot break
    the launch.
    """
    if not MULTIPLEXING_SUPPORTED:
        return ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path()}",
        "-o", f"ControlPersist={persist}",
    ]


def expire_all_masters(
    *, background: bool = True, timeout: float = 10.0, mode: str = "stop"
) -> None:
    """Retire every live ControlMaster.

    Used after a global SSH-settings change (Preferences ▸ ssh_overrides), so
    new connections negotiate fresh masters with the new options instead of
    riding stale transports — and at daemon shutdown, where a master left
    alive would outlive the application by its whole ``ControlPersist`` window.

    ``ssh -O <mode>`` connects straight to each socket file (the host argument
    only feeds token expansion, which a literal ControlPath doesn't need).
    ``mode="stop"`` (the settings-change default) stops the master accepting
    new clients and lets live sessions drain; ``mode="exit"`` terminates it
    immediately and is what shutdown uses, where nothing is left to drain.
    Sockets that don't answer are stale leftovers (crashed master) and are
    unlinked here. Best-effort throughout; ``timeout`` bounds each individual
    ``ssh`` call so an unresponsive master cannot stall a quit.
    """
    if not MULTIPLEXING_SUPPORTED:
        return

    def _stop_all() -> None:
        try:
            entries = os.listdir(socket_dir())
        except OSError:
            return
        for name in entries:
            path = os.path.join(socket_dir(), name)
            try:
                result = subprocess.run(
                    ["ssh", *CONTROL_COMMAND_OPTIONS,
                     "-o", f"ControlPath={path}", "-O", mode,
                     "sshpilot-invalidate"],
                    capture_output=True, timeout=timeout, check=False)
                # Unlink only on positive evidence the master is gone. Any
                # other failure (a handshake that failed, a wedged master)
                # may be a live master, and removing its socket would strand
                # it with no way left to reach or close it.
                if result.returncode != 0 and os.path.exists(path):
                    output = (result.stderr or b"") + (result.stdout or b"")
                    if output_shows_no_master(output):
                        os.unlink(path)  # stale socket, master already gone
            except Exception:
                logger.debug("expire_all_masters: %s not stopped", path,
                             exc_info=True)

    if background:
        threading.Thread(target=_stop_all, daemon=True,
                         name="ssh-mux-expire").start()
    else:
        _stop_all()
