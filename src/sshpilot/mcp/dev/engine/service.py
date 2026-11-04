"""One engine, several adapters: the operations behind the V2 tools.

:class:`DevIntelligence` owns the index for one checkout and exposes the
deterministic operations that both the MCP server and the CLI call. Every
operation refreshes the index incrementally first, so no result is served
from stale rows.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

from ..._scope import RepoScope
from .graph import Graph
from .index import DevIndex
from .models import EngineError, envelope


class DevIntelligence:
    def __init__(self, scope: RepoScope, index_dir: Optional[Path] = None, prefixes: Optional[List[str]] = None):
        self.scope = scope
        self.index = DevIndex(scope, index_dir=index_dir, prefixes=prefixes)
        self._lock = threading.RLock()

    def _graph(self) -> Graph:
        self.index.refresh()
        return Graph(self.index)

    def freshness(self) -> dict:
        return self.index.freshness()

    # -- index management ----------------------------------------------------
    def index_status(self, rebuild: bool = False) -> dict:
        with self._lock:
            stats = self.index.refresh(full=rebuild)
            return envelope(
                "index_status",
                self.freshness(),
                status="ok",
                refresh=stats,
                parse_errors=self.index.parse_errors()[:20],
                index_path=str(self.index.path),
            )

    # -- V2 operations -------------------------------------------------------------
    def get_context(self, reference: str, include_source: bool = True, depth: int = 1, budget: Optional[int] = None) -> dict:
        from .context import get_context

        with self._lock:
            return get_context(self._graph(), reference, self.freshness, include_source=include_source, depth=depth, budget=budget)

    def inspect_impact(
        self,
        base: Optional[str] = None,
        paths: Optional[List[str]] = None,
        depth: int = 2,
        budget: Optional[int] = None,
    ) -> dict:
        from .impact import inspect_impact

        with self._lock:
            return inspect_impact(self._graph(), self.freshness, base=base, paths=paths, depth=depth, budget=budget)

    def diagnose_bug(
        self,
        symptoms: str,
        traceback: Optional[str] = None,
        test_name: Optional[str] = None,
        changed_paths: Optional[List[str]] = None,
        base: Optional[str] = None,
        max_items: int = 8,
        budget: Optional[int] = None,
    ) -> dict:
        from .diagnosis import diagnose

        with self._lock:
            changed = list(changed_paths or [])
            if base is not None:
                from .._git import GitError, GitInspector

                try:
                    git = GitInspector(self.scope.root)
                    git.validate_revision(base)
                    changed.extend(git.changed_paths(base))
                except GitError as error:
                    raise EngineError(str(error)) from error
            return diagnose(
                self._graph(),
                self.freshness,
                symptoms,
                traceback=traceback,
                test_name=test_name,
                changed_paths=changed,
                max_items=max_items,
                budget=budget,
            )

    def verify_change(
        self,
        base: str = "HEAD",
        mode: str = "static",
        suites: Optional[List[str]] = None,
        max_test_files: int = 8,
        budget: Optional[int] = None,
        test_timeout: Optional[float] = None,
    ) -> dict:
        from .. import execution
        from .verify import verify_change

        with self._lock:
            return verify_change(
                self._graph(),
                self.freshness,
                base=base,
                mode=mode,
                suites=suites,
                max_test_files=max_test_files,
                budget=budget,
                test_timeout=test_timeout or execution.EXECUTION_TIMEOUT_SECONDS,
            )

    def close(self) -> None:
        self.index.close()


__all__ = ["DevIntelligence", "EngineError"]
