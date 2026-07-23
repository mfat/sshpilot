"""Background, cached check of connections against their effective SSH config.

The sidebar shows a warning icon on a connection whose host block does not match
what SSH actually resolves (a ``Host *`` global, a directive before the first
``Host`` block, or an included file overrides/adds settings). Running ``ssh -G``
is too slow for the row/menu build path, so this service:

- caches each connection's result until :meth:`invalidate`,
- computes off the main thread on a single persistent worker (throttled — one
  connection at a time),
- reads (:meth:`status`) are O(1) dict lookups, so the sidebar hot path does no
  work,
- fast-paths the common case: if the config has no global-scope directives at
  all, nothing can override a host, so every connection matches without a
  per-host ``ssh -G`` (that fact is probed once per config and cached).

Results are delivered back on the GTK main thread via ``GLib.idle_add``.
"""

from __future__ import annotations

import logging
import os
import queue
import tempfile
import threading
from typing import Callable, Optional

from gi.repository import GLib

logger = logging.getLogger(__name__)

# A host name no real ``Host`` block should match, used to probe whether the
# config carries any global-scope directives (it still matches ``Host *``).
_PROBE_HOST = "sshpilot-effective-config-probe-donotmatch"


class EffectiveConfigChecker:
    def __init__(self, connection_manager,
                 on_result: Optional[Callable[[str, bool], None]] = None) -> None:
        self._cm = connection_manager
        self._on_result = on_result
        self._cache: dict[str, bool] = {}
        self._queued: set[str] = set()
        # Per-nickname generation. A check captures the generation at enqueue
        # time; its result is only cached/published if the generation still
        # matches, so an in-flight compute that finishes after an invalidate can
        # never overwrite the cache with a stale value.
        self._gen: dict[str, int] = {}
        self._globals_present: Optional[bool] = None  # probed lazily, per config
        self._globals_gen = 0  # bumped on full invalidate; guards the probe cache
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
            gen = self._gen.setdefault(nickname, 0)
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
                self._queued.clear()
                self._globals_present = None  # config changed -> re-probe
                self._globals_gen += 1        # cancel any in-flight probe
                for key in self._gen:
                    self._gen[key] += 1
            else:
                self._cache.pop(nickname, None)
                self._queued.discard(nickname)
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
                differs = self._compute(connection)
            except Exception:
                logger.debug("effective-config check failed for %s", nickname, exc_info=True)
                differs = None
            publish = False
            with self._lock:
                self._queued.discard(nickname)
                # Drop the result if an invalidate bumped the generation while
                # this compute was running.
                if differs is not None and gen == self._gen.get(nickname, 0):
                    self._cache[nickname] = differs
                    publish = True
            if publish and self._on_result is not None:
                GLib.idle_add(self._on_result, nickname, differs)

    def _compute(self, connection) -> Optional[bool]:
        # Fast path: no global-scope directives anywhere -> nothing overrides a
        # host, so it matches without a per-host ssh -G.
        if not self._config_has_globals():
            return False
        host = getattr(connection, 'nickname', '') or ''
        if not host:
            return None
        from .effective_config_dialog import saved_connection_block
        from .ssh_config_utils import diff_effective_config
        own_block = saved_connection_block(self._cm, connection)
        try:
            root_config = connection._resolve_config_override_path()
        except Exception:
            root_config = None
        result = diff_effective_config(host, root_config, own_block)
        return None if result is None else bool(result.get('has_diff'))

    def _config_has_globals(self) -> bool:
        # Wildcard/negated Host blocks (Host *, Host prod-*, Host !x) are tracked
        # in rules and may match some connection, so force per-host checking —
        # the sentinel probe alone would miss host-specific patterns it doesn't
        # happen to match. The probe is only needed for the untracked case:
        # directives before the first Host block.
        if getattr(self._cm, 'rules', None):
            return True
        with self._lock:
            cached = self._globals_present
            gen = self._globals_gen
        if cached is not None:
            return cached
        present = self._probe_globals()
        with self._lock:
            # Only cache if no invalidate happened while probing (else the probe
            # ran against a now-stale config); this call still returns its own
            # result and the next one re-probes.
            if gen == self._globals_gen:
                self._globals_present = present
        return present

    def _probe_globals(self) -> bool:
        """Does the config carry any global-scope directive?

        Compares the effective config of a sentinel host (which no specific
        ``Host`` block matches) between the real config and an empty one. A
        difference means there is a ``Host *`` block or a directive before the
        first ``Host`` — i.e. something that applies to every host. Errors are
        treated as "yes" so the correct (per-host) path runs.
        """
        from .ssh_config_utils import get_effective_ssh_config, _effective_config_lines
        root = getattr(self._cm, 'ssh_config_path', None)
        if not root:
            return True
        empty_fd, empty_path = tempfile.mkstemp(prefix='.sshpilot-empty-', suffix='.conf')
        try:
            os.close(empty_fd)
            with_root = get_effective_ssh_config(_PROBE_HOST, config_file=root)
            defaults = get_effective_ssh_config(_PROBE_HOST, config_file=empty_path)
        except Exception:
            logger.debug("globals probe failed", exc_info=True)
            return True
        finally:
            try:
                os.unlink(empty_path)
            except OSError:
                pass
        if not with_root or not defaults:
            return True
        return _effective_config_lines(with_root) != _effective_config_lines(defaults)
