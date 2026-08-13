"""AST-based symbol lookup for the dev MCP server.

Finds class/function/method definitions across repository Python sources.
"""

from __future__ import annotations

import ast
from typing import List, Optional

from .._scope import RepoScope

#: Kinds this lookup can report.
SYMBOL_KINDS = frozenset(
    {"class", "method", "async_method", "function", "async_function"}
)
#: Default cap so a broad query cannot flood the model context.
DEFAULT_MAX_RESULTS = 100
MAX_FILE_BYTES = 2 * 1024 * 1024


def _matches(name: str, partial: bool) -> "object":
    if partial:
        return lambda node_name, needle=name.casefold(): needle in node_name.casefold()
    return lambda node_name: node_name.casefold() == name.casefold()


class _Collector(ast.NodeVisitor):
    """Collect definition nodes, tracking whether we are inside a class."""

    def __init__(self, predicate, kind_filter: Optional[str]) -> None:
        self._predicate = predicate
        self._kind_filter = kind_filter
        self._in_class = False
        self.found: List[dict] = []

    def _record(self, node_kind: str, name: str, lineno: int) -> None:
        if self._kind_filter is not None and node_kind != self._kind_filter:
            return
        if self._predicate(name):
            self.found.append({"name": name, "kind": node_kind, "line": lineno})

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record("class", node.name, node.lineno)
        previous, self._in_class = self._in_class, True
        self.generic_visit(node)
        self._in_class = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self._in_class else "function"
        self._record(kind, node.name, node.lineno)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        kind = "async_method" if self._in_class else "async_function"
        self._record(kind, node.name, node.lineno)
        self.generic_visit(node)


def find_symbols(
    scope: RepoScope,
    name: str,
    kind: Optional[str] = None,
    partial: bool = True,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """Find symbol definitions (classes/functions/methods) across the repo.

    ``partial=True`` (default) matches symbols whose name contains *name*
    (case-insensitive); ``partial=False`` requires an exact name match. The
    optional *kind* filters to one of ``class``, ``method``, ``async_method``,
    ``function``, or ``async_function``.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string")
    if kind is not None and kind not in SYMBOL_KINDS:
        raise ValueError(f"kind must be one of {sorted(SYMBOL_KINDS)}")
    if not isinstance(max_results, int) or max_results < 1 or max_results > 10_000:
        raise ValueError("max_results must be an integer between 1 and 10000")

    predicate = _matches(name, partial)
    collector = _Collector(predicate, kind)
    found: List[dict] = []
    for relative, text in scope.iter_text_within(max_bytes=MAX_FILE_BYTES):
        if relative.suffix != ".py":
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        collector.found = []
        collector.visit(tree)
        for symbol in collector.found:
            found.append({"file": str(relative), **symbol})
            if len(found) >= max_results:
                return {"symbols": found, "total": len(found), "truncated": True}
    return {"symbols": found, "total": len(found), "truncated": False}