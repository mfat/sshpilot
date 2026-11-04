"""Test-to-code association from static evidence.

Evidence levels, strongest first (all are *static* evidence, never coverage):

``calls``            a test function calls the symbol (resolved name)
``references``       a test function references it (callback, enum member…)
``imports-symbol``   a test file imports the symbol by name — weak: an
                     import does not show the test exercises it
``imports-module``   a test file imports the symbol's module — weak: it does
                     not imply the test exercises the changed lines.

Coverage-based association is not implemented; nothing here claims a test
covers a line.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Set, Tuple

from .graph import Graph

EVIDENCE_RANK = {"calls": 0, "references": 1, "connects_signal": 1, "imports-symbol": 2, "imports-module": 3}
#: Evidence strong enough to call a changed symbol "mapped" to a test: a test
#: function calls or references it. Importing it proves nothing about use.
STRONG_EVIDENCE = frozenset({"calls", "references", "connects_signal"})


def pytest_id(path: str, qualname: str) -> str:
    """``tests/x.py::Class::test_y`` for a test symbol, else the file."""
    if qualname in ("", "<module>"):
        return path
    return f"{path}::{qualname.split('@')[0].replace('.', '::')}"


def _is_test_function(qualname: str) -> bool:
    leaf = qualname.split("@")[0].rsplit(".", 1)[-1]
    return leaf.startswith("test")


def related_tests(graph: Graph, symbols: Iterable[dict], limit: int = 40) -> List[dict]:
    """Rank tests statically associated with *symbols* (deterministic)."""
    best: Dict[str, Tuple[int, dict]] = {}

    def offer(test_id: str, evidence: str, target: dict, line: int) -> None:
        rank = EVIDENCE_RANK.get(evidence, 9)
        entry = {
            "test": test_id,
            "evidence": evidence,
            "target": target["id"],
            "line": line,
            "confidence": "heuristic",
        }
        if evidence == "imports-module":
            entry["note"] = "module import only; does not imply the changed lines are exercised"
        elif evidence == "imports-symbol":
            entry["note"] = "import only; does not show the test calls it"
        current = best.get(test_id)
        if current is None or rank < current[0]:
            best[test_id] = (rank, entry)

    seen_modules: Set[str] = set()
    for symbol in symbols:
        targets = graph.aliases_of(symbol["fqname"])
        placeholders = ",".join("?" for _ in targets)
        rows = graph.db.execute(
            f"SELECT s.path, s.qualname, s.kind, e.kind, e.line FROM edges e JOIN symbols s ON s.sid = e.src "
            f"WHERE e.target IN ({placeholders}) AND s.path LIKE 'tests/%' ORDER BY s.path, e.line",
            targets,
        ).fetchall()
        for path, qualname, kind, edge_kind, line in rows:
            if edge_kind == "imports":
                offer(path, "imports-symbol", symbol, line)
            elif _is_test_function(qualname) and kind in ("function", "async_function", "method", "async_method"):
                offer(pytest_id(path, qualname), edge_kind, symbol, line)
            else:
                # helper/fixture in a test module: attribute to the file
                offer(path, edge_kind, symbol, line)
        module_fq = symbol["fqname"] if symbol["kind"] == "module" else _module_of(graph, symbol)
        if module_fq and module_fq not in seen_modules:
            seen_modules.add(module_fq)
            escaped = module_fq.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
            for path, line in graph.db.execute(
                "SELECT s.path, e.line FROM edges e JOIN symbols s ON s.sid = e.src "
                "WHERE e.kind='imports' AND (e.target=? OR e.target LIKE ? ESCAPE '\\') "
                "AND s.path LIKE 'tests/%' ORDER BY s.path, e.line",
                (module_fq, escaped + ".%"),
            ):
                offer(path, "imports-module", symbol, line)
    ranked = sorted(best.values(), key=lambda item: (item[0], item[1]["test"]))
    return [entry for _rank, entry in ranked[:limit]]


def _module_of(graph: Graph, symbol: dict) -> str:
    module = graph.module_symbol(symbol["path"])
    return module["fqname"] if module else ""


def unique_test_files(entries: Iterable[dict]) -> List[str]:
    """Unique test files (in rank order) for a list of related_tests entries."""
    files: List[str] = []
    for entry in entries:
        path = entry["test"].split("::", 1)[0]
        if path not in files:
            files.append(path)
    return files
