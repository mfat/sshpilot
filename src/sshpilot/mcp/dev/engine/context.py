"""``get_context``: bounded, freshness-verified context for one reference."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .graph import Graph, compact_symbol, resolve_reference
from .models import EngineError, envelope, fit_to_budget, normalize_budget
from .tests import related_tests

_SUFFIX_RE = re.compile(r"^(?P<ref>.+?)\?(?:lines=(?P<a>\d+)-(?P<b>\d+)|offset=(?P<offset>\d+))$")
_PAGE = 15


def _split_reference(reference: str) -> Tuple[str, Optional[Tuple[int, int]], int]:
    match = _SUFFIX_RE.match(reference.strip())
    if not match:
        return reference, None, 0
    lines = (int(match.group("a")), int(match.group("b"))) if match.group("a") else None
    return match.group("ref"), lines, int(match.group("offset") or 0)


def _rank_candidates(candidates: List[dict]) -> List[dict]:
    def key(symbol: dict) -> tuple:
        return (
            symbol["path"].startswith("tests/"),
            symbol["kind"] in ("variable", "attribute"),
            symbol["path"].count("/"),
            symbol["path"],
            symbol["line"],
        )

    return sorted(candidates, key=key)


def _excerpt(graph: Graph, symbol: dict, window: Optional[Tuple[int, int]], max_bytes: int) -> Dict[str, object]:
    path = graph.index.scope.resolve(symbol["path"])
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start, end = window or (symbol["line"], symbol["end_line"])
    start = max(1, start)
    end = min(len(lines), end)
    taken: List[str] = []
    used = 0
    last = start - 1
    for number in range(start, end + 1):
        text = lines[number - 1]
        cost = len(text.encode("utf-8")) + 1
        if taken and used + cost > max_bytes:
            break
        taken.append(text)
        used += cost
        last = number
    excerpt = {"path": symbol["path"], "lines": [start, last], "text": "\n".join(taken)}
    if last < end:
        excerpt["truncated"] = True
        excerpt["next_lines_ref"] = f"{symbol['id']}?lines={last + 1}-{end}"
    return excerpt


def domain_context(graph: Graph, node: str, budget: int, freshness: dict) -> dict:
    edges = graph.domain(node)
    result = envelope(
        "get_context",
        freshness,
        status="ok" if edges else "not_found",
        reference=node,
        domain_edges=edges,
    )
    for edge in edges:
        if edge["kind"] == "wire_to_handler":
            for symbol in graph.by_fqname(edge["to"]):
                result["handler"] = compact_symbol(symbol)
    return fit_to_budget(result, budget, ["domain_edges"])


def get_context(
    graph: Graph,
    reference: str,
    freshness_fn,
    include_source: bool = True,
    depth: int = 1,
    budget: Optional[int] = None,
) -> dict:
    budget = normalize_budget(budget)
    if not isinstance(depth, int) or depth < 0 or depth > 3:
        raise EngineError("depth must be between 0 and 3")
    base_ref, window, offset = _split_reference(reference)
    if base_ref.startswith(("wire:", "capability:")):
        return domain_context(graph, base_ref, budget, freshness_fn())

    candidates, how = resolve_reference(graph, base_ref)
    if not candidates:
        like = graph.db.execute(
            "SELECT id FROM symbols WHERE name LIKE ? AND kind != 'module' ORDER BY length(name), path LIMIT 10",
            (f"%{base_ref.rsplit('.', 1)[-1].rsplit('::', 1)[-1]}%",),
        ).fetchall()
        result = envelope(
            "get_context",
            freshness_fn(),
            status="not_found",
            reference=reference,
            resolution=how,
            suggestions=[row[0] for row in like],
        )
        return fit_to_budget(result, budget, ["suggestions"])
    if len(candidates) > 1 and how not in ("symbol-id", "path-line", "path"):
        ranked = _rank_candidates(candidates)
        result = envelope(
            "get_context",
            freshness_fn(),
            status="ambiguous",
            reference=reference,
            resolution=how,
            candidates=[{**compact_symbol(s), "signature": s["signature"]} for s in ranked],
            hint="Call get_context again with one candidate id.",
        )
        return fit_to_budget(result, budget, ["candidates"])

    symbol = candidates[0]
    # Freshness: re-hash the file right before returning its lines.
    if not graph.index.file_is_current(symbol["path"]):
        graph.index.refresh(force_paths=[symbol["path"]])
        graph = Graph(graph.index)
        symbol = graph.symbol(symbol["id"])
        if symbol is None or not graph.index.file_is_current(candidates[0]["path"]):
            raise EngineError(f"{candidates[0]['id']} changed while reading; retry")

    incoming = graph.incoming(symbol)
    callers = incoming["resolved"]
    callees = graph.outgoing(symbol)
    chain = graph.wire_chain(symbol["fqname"])
    tests = related_tests(graph, [symbol], limit=12)

    result = envelope(
        "get_context",
        freshness_fn(),
        status="ok",
        reference=reference,
        resolution=how,
        symbol={
            **compact_symbol(symbol),
            "fqname": symbol["fqname"],
            "signature": symbol["signature"],
            "doc": symbol["doc"],
        },
    )
    if include_source:
        source_budget = max(400, int(budget * 0.55))
        result["source"] = _excerpt(graph, symbol, window, source_budget)
    result["callers"] = callers[offset : offset + _PAGE]
    result["callers_total"] = len(callers)
    if len(callers) > offset + _PAGE:
        result["callers_next_ref"] = f"{symbol['id']}?offset={offset + _PAGE}"
    result["possible_callers"] = incoming["possible"][:8]
    result["possible_callers_total"] = incoming["possible_total"]
    if incoming["possible_total"]:
        result["possible_callers_note"] = (
            "unresolved: an attribute call with the same name on an object of unknown type"
        )
    result["callees"] = callees
    if chain:
        result["api_chain"] = chain
    result["related_tests"] = tests
    if depth > 1:
        dependents, truncated = graph.reverse_dependencies([symbol], depth=depth, limit=60)
        result["dependents"] = dependents
        result["dependents_truncated"] = truncated
    return fit_to_budget(
        result,
        budget,
        [
            "possible_callers",
            "dependents",
            "callees",
            "related_tests",
            "api_chain",
            "callers",
        ],
        next_context_ref=symbol["id"],
    )
