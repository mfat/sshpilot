"""SSH connection multiplexing (ControlMaster) policy + a small refcounted pool.

Spawning ``ssh -F config host <cmd>`` per call pays a full TCP connect and auth
handshake every time. OpenSSH's ControlMaster lets the first connection open a
master socket that later ``ssh`` invocations reuse over a new channel — no
re-auth, ~10–50 ms per call instead of hundreds of ms / seconds. This is the
right fix for polling surfaces such as the Docker Console (a 3-second ``ps`` +
``stats`` refresh).

This module owns two things:

* **Socket policy** — where the control sockets live and the exact ``-o`` options
  that turn multiplexing on. A single source of truth so every caller (the global
  Preferences toggle and the per-plugin pool) uses the *same* ``ControlPath`` and
  therefore shares one master per host with no conflict.
* **A refcounted pool** — ``acquire``/``release`` keyed by connection nickname so
  a page can keep a master warm while it is open. ``ControlMaster=auto`` means the
  first real command creates the master and ``ControlPersist`` keeps it alive, so
  there is no separate background ssh process to spawn or authenticate, and a
  dropped master is recreated transparently by the next call.

It is GTK-free and dependency-free so it is unit-testable offline.
"""

from __future__ import annotations

import os
import threading
from typing import Dict, List

# Default master idle lifetime. The Docker poll (~3 s) keeps it warm; after a tab
# closes the master lingers this long before expiring (release() also tears it
# down explicitly via ``ssh -O exit``).
DEFAULT_PERSIST = "60"


def socket_dir() -> str:
    """Directory holding the control sockets (created if missing).

    Prefers ``$XDG_RUNTIME_DIR/sshpilot/cm`` (tmpfs, auto-cleaned at logout); falls
    back to ``~/.ssh/sockets``. Kept short so the socket path stays under the
    ~104-char ``sun_path`` limit once ssh appends the ``%C`` hash."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = os.path.join(runtime, "sshpilot", "cm") if runtime \
        else os.path.expanduser("~/.ssh/sockets")
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


class _MultiplexPool:
    """Refcount of how many open surfaces want a warm master per nickname.

    Thread-safe: pages acquire/release on the UI thread while ``run_command``
    consults ``is_active`` from worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refcounts: Dict[str, int] = {}

    def acquire(self, nickname: str) -> None:
        if not nickname:
            return
        with self._lock:
            self._refcounts[nickname] = self._refcounts.get(nickname, 0) + 1

    def release(self, nickname: str) -> bool:
        """Drop a reference. Returns True when the last reference went away (the
        caller should then tear the master down)."""
        if not nickname:
            return False
        with self._lock:
            count = self._refcounts.get(nickname, 0)
            if count <= 1:
                self._refcounts.pop(nickname, None)
                return count == 1  # only "now zero" if it was actually active
            self._refcounts[nickname] = count - 1
            return False

    def is_active(self, nickname: str) -> bool:
        if not nickname:
            return False
        with self._lock:
            return self._refcounts.get(nickname, 0) > 0


# Process-wide singleton: masters are shared across all plugins/pages.
_pool = _MultiplexPool()


def acquire(nickname: str) -> None:
    _pool.acquire(nickname)


def release(nickname: str) -> bool:
    return _pool.release(nickname)


def is_active(nickname: str) -> bool:
    return _pool.is_active(nickname)
