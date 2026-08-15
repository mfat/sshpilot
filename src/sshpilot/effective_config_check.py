"""Background, cached check of daemon-provided effective SSH comparisons.

The sidebar shows a warning icon on a connection whose authored host block does
not match what the daemon's OpenSSH resolver returns. The GTK worker only calls
the typed daemon API, so it never selects a config root or runs OpenSSH itself.
This service:

- caches each connection's result until :meth:`invalidate`,
- computes off the main thread on a single persistent worker (throttled — one
  connection at a time),
- reads (:meth:`status`) are O(1) dict lookups, so the sidebar hot path does no
  work,
- rejects stale results when a connection or daemon generation changes.

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
                 on_result: Optional[Callable[[str, bool], None]] = None,
                 client_provider: Optional[Callable[[], object]] = None) -> None:
        del connection_manager  # retained for constructor compatibility only
        self._on_result = on_result
        self._client_provider = client_provider
        self._cache: dict[str, bool] = {}
        # Store the generation of the pending request, rather than just a
        # boolean marker. A stale completion must not clear a newer request.
        self._queued: dict[str, int] = {}
        # Per-nickname generation. A check captures the generation at enqueue
        # time; its result is only cached/published if the generation still
        # matches, so an in-flight compute that finishes after an invalidate can
        # never overwrite the cache with a stale value.
        self._gen: dict[str, int] = {}
        self._daemon_gen: dict[str, int] = {}
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
            gen = self._gen.setdefault(nickname, 0)
            self._queued[nickname] = gen
        self._queue.put((connection, nickname, gen))
        self._ensure_worker()

    def invalidate(self, nickname: Optional[str] = None) -> None:
        """Forget cached results so they are recomputed on the next schedule().

        Pass a nickname to drop just that connection (e.g. after it is saved),
        or nothing to drop everything and re-probe globals (e.g. after the SSH
        config file changes). Bumping the generation also cancels any in-flight
        computation for the affected nickname(s).
        """
        with self._lock:
            if nickname is None:
                self._cache.clear()
                self._daemon_gen.clear()
                self._queued.clear()
                for key in self._gen:
                    self._gen[key] += 1
            else:
                self._cache.pop(nickname, None)
                self._daemon_gen.pop(nickname, None)
                self._queued.pop(nickname, None)
                self._gen[nickname] = self._gen.get(nickname, 0) + 1
        if nickname is None:
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
            connection, nickname, gen = self._queue.get()  # blocks; persistent
            try:
                computed = self._compute(connection)
            except Exception:
                logger.debug("effective-config check failed for %s", nickname, exc_info=True)
                computed = None
            differs = computed[0] if computed is not None else None
            daemon_generation = computed[1] if computed is not None else None
            publish = self._accept_result(
                nickname,
                gen,
                None if differs is None else (differs, daemon_generation or 0),
            )
            if publish and self._on_result is not None:
                GLib.idle_add(self._on_result, nickname, differs)

    def _accept_result(
        self,
        nickname: str,
        local_generation: int,
        computed: Optional[tuple[bool, int]],
    ) -> bool:
        """Publish only a current local and daemon snapshot generation."""
        with self._lock:
            queued_generation = self._queued.get(nickname)
            if computed is None or local_generation != self._gen.get(nickname, 0):
                if queued_generation == local_generation:
                    self._queued.pop(nickname, None)
                return False
            if queued_generation == local_generation:
                self._queued.pop(nickname, None)
            differs, daemon_generation = computed
            if daemon_generation < self._daemon_gen.get(nickname, -1):
                return False
            self._cache[nickname] = differs
            self._daemon_gen[nickname] = daemon_generation
            return True

    def _compute(self, connection) -> Optional[tuple[bool, int]]:
        """Ask the daemon for the generation-tagged effective-config result."""
        if self._client_provider is None:
            return None
        try:
            client = self._client_provider()
            if client is None:
                return None
            from .api.connection_identity import connection_id_for

            result = client.get_effective_config(connection_id_for(connection))
            if not result.available:
                return None
            # Keep the daemon snapshot generation attached to the computation;
            # the worker drops a response older than the last published one.
            return bool(result.has_diff), int(getattr(result, "generation", 0))
        except Exception:
            logger.debug("daemon effective-config check failed", exc_info=True)
            return None
