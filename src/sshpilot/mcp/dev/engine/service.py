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

    def close(self) -> None:
        self.index.close()


__all__ = ["DevIntelligence", "EngineError"]
