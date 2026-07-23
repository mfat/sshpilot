"""Background, cached check of connections against their effective SSH config.

The sidebar shows a warning icon on a connection whose host block does not match
what SSH actually resolves (a ``Host *`` global or included file overrides/adds
settings). Running ``ssh -G`` is too slow for the row/menu build path, so this
service:

- caches each connection's result until :meth:`invalidate`,
- computes off the main thread on a single persistent worker (throttled — one
  connection at a time),
- reads (:meth:`status`) are O(1) dict lookups, so the sidebar hot path does no
  work,
- fast-paths the common case: with no wildcard/global blocks in the config,
  nothing can override a host, so the answer is "matches" without any subprocess.

Results are delivered back on the GTK main thread via ``GLib.idle_add``.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

from gi.repository import GLib

logger = logging.getLogger(__name__)


class EffectiveConfigChecker:
    def __init__(self, connection_manager,
                 on_result: Optional[Callable[[str, bool], None]] = None) -> None:
        self._cm = connection_manager
        self._on_result = on_result
        self._cache: dict[str, bool] = {}
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._queue: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    # ---- main-thread API (cheap) -------------------------------------------

    def status(self, nickname: str) -> Optional[bool]:
        """Cached differs-flag, or None if not computed yet. O(1)."""
        with self._lock:
            return self._cache.get(nickname)

    def schedule(self, connection) -> None:
        """Enqueue a background check unless already cached/pending. No ssh here."""
        nickname = getattr(connection, 'nickname', '') or ''
        if not nickname:
            return
        with self._lock:
            if nickname in self._cache or nickname in self._queued:
                return
            self._queued.add(nickname)
        self._queue.put(connection)
        self._ensure_worker()

    def invalidate(self, nickname: Optional[str] = None) -> None:
        """Forget cached results so they are recomputed on the next schedule().

        Pass a nickname to drop just that connection (e.g. after it is saved),
        or nothing to drop everything (e.g. after the SSH config file changes).
        """
        with self._lock:
            if nickname is None:
                self._cache.clear()
                self._queued.clear()
            else:
                self._cache.pop(nickname, None)
                self._queued.discard(nickname)
        if nickname is None:
            # Drop any still-queued work; survivors get re-scheduled by the UI.
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass

    # ---- worker ------------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="effcfg-check", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            connection = self._queue.get()  # blocks; persistent daemon worker
            nickname = getattr(connection, 'nickname', '') or ''
            try:
                differs = self._compute(connection)
            except Exception:
                logger.debug("effective-config check failed for %s", nickname, exc_info=True)
                differs = None
            with self._lock:
                self._queued.discard(nickname)
                if differs is not None:
                    self._cache[nickname] = differs
            if differs is not None and self._on_result is not None:
                GLib.idle_add(self._on_result, nickname, differs)

    def _compute(self, connection) -> Optional[bool]:
        cm = self._cm
        # Fast path: no wildcard/global blocks -> nothing can override a host.
        if not getattr(cm, 'rules', None):
            return False
        host = getattr(connection, 'nickname', '') or ''
        if not host:
            return None
        from .effective_config_dialog import connection_config_data
        from .ssh_config_utils import diff_effective_config
        own_block = cm.format_ssh_config_entry(connection_config_data(connection))
        try:
            root_config = connection._resolve_config_override_path()
        except Exception:
            root_config = None
        result = diff_effective_config(host, root_config, own_block)
        return None if result is None else bool(result.get('has_diff'))
