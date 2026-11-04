"""``inspect_impact``: changed symbols, dependents, contract risks, tests."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from ..._scope import ScopeError
from .. import api_surface
from ..architecture import _classify_path
from .domain import DOMAIN_INPUTS
from .extract import module_name_for
from .graph import Graph, compact_symbol
from .models import EngineError, UNRESOLVED, envelope, fit_to_budget, normalize_budget
from .tests import STRONG_EVIDENCE, related_tests, unique_test_files

#: Lines examined per changed file (bounded work for huge diffs).
MAX_LINES_PER_FILE = 5000
#: Non-Python paths with SSH Pilot-specific consequences.
_SPECIAL_PATHS = (
    (api_surface.SCHEMA_RELPATH, "generated-api-schema", "Run validate_api_artifacts and check_api_drift."),
    ("docs/api/generated/", "generated-api-artifact", "Regenerate with scripts/generate_api_artifacts.py; never hand-edit."),
    ("po/", "localization", "Rebuild .mo catalogues for every po/LINGUAS language."),
    ("data/", "resources", "GResource/UI data: not statically analysed here; run GUI tests."),
)


def collect_changes(
    graph: Graph, base: Optional[str], paths: Optional[Sequence[str]]
) -> Tuple[Dict[str, Optional[List[Tuple[int, int]]]], List[str]]:
    """Map changed path → new-side line ranges (None = whole file)."""
    from .._git import GitError, GitInspector

    notes: List[str] = []
    changes: Dict[str, Optional[List[Tuple[int, int]]]] = {}
    wanted = None
    if paths:
        wanted = []
        for raw in paths:
            if not isinstance(raw, str) or not raw.strip():
                raise EngineError("paths must be non-empty repository-relative strings")
            candidate = raw.strip().replace("\\", "/")
            if candidate.startswith("./"):
                candidate = candidate[2:]
            try:
                graph.index.scope.resolve(candidate)
            except ScopeError as error:
                raise EngineError(str(error)) from error
            if candidate not in wanted:
                wanted.append(candidate)
    if base is not None:
        git = GitInspector(graph.index.scope.root)
        try:
            ranges = git.changed_line_ranges(base)
            all_paths = git.changed_paths(base)
        except GitError as error:
            raise EngineError(str(error)) from error
        for path in all_paths:
            # Untracked files have no diff hunks: the whole file is new.
            changes[path] = ranges.get(path) if path in ranges else None
        notes.append(
            f"git diff {base} vs working tree (committed since base, staged, unstaged) plus untracked files"
        )
        if wanted is not None:
            changes = {path: value for path, value in changes.items() if any(path == w or path.startswith(w.rstrip("/") + "/") for w in wanted)}
    elif wanted is not None:
        for path in wanted:
            changes[path] = None
        notes.append("explicit paths: every symbol in each file is treated as changed")
    else:
        raise EngineError("provide base (a Git revision) or paths")
    return changes, notes


def _changed_symbols(graph: Graph, path: str, ranges: Optional[List[Tuple[int, int]]]) -> List[dict]:
    symbols = graph.in_file(path)
    if ranges is None:
        # Whole file: every definition is potentially changed.
        return [s for s in symbols if s["kind"] not in ("module", "variable", "attribute")]
    definitions = [s for s in symbols if s["kind"] not in ("module", "variable", "attribute")]
    assignments = [s for s in symbols if s["kind"] in ("variable", "attribute")]
    module = next((s for s in symbols if s["kind"] == "module"), None)
    found: Dict[str, dict] = {}
    budget = MAX_LINES_PER_FILE
    for start, end in ranges:
        for line in range(start, end + 1):
            budget -= 1
            if budget < 0:
                break
            containing = [s for s in definitions if s["line"] <= line <= s["end_line"]]
            if containing:
                innermost = min(containing, key=lambda s: (s["end_line"] - s["line"], -s["line"]))
            else:
                assigned = [s for s in assignments if s["line"] <= line <= s["end_line"]]
                innermost = assigned[0] if assigned else module
            if innermost is not None:
                found.setdefault(innermost["id"], innermost)
    return sorted(found.values(), key=lambda s: s["line"])


def _dangling_references(graph: Graph, module_fq: str, limit: int = 15) -> List[dict]:
    """Static references into *module_fq* that no longer resolve (renamed or
    removed names). Heuristic: a name may be created at runtime."""
    rows = graph.db.execute(
        "SELECT DISTINCT e.target FROM edges e WHERE e.kind IN ('calls','references','imports') "
        "AND e.confidence='static' AND (e.target = ? OR e.target LIKE ? ESCAPE '\\') LIMIT 400",
        (module_fq, module_fq.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%") + ".%"),
    ).fetchall()
    dangling: List[dict] = []
    for (target,) in rows:
        resolved, _how = graph.resolve_target(target)
        if resolved:
            continue
        # ``module.Class.attr``: Class exists, attr is probably an instance
        # attribute — not evidence of a broken reference.
        owner = target.rsplit(".", 1)[0]
        if owner != module_fq and graph.resolve_target(owner)[0]:
            continue
        users = graph.db.execute(
            "SELECT s.id, s.path, e.line FROM edges e JOIN symbols s ON s.sid = e.src WHERE e.target=? ORDER BY s.path, e.line LIMIT 3",
            (target,),
        ).fetchall()
        dangling.append(
            {
                "name": target,
                "used_by": [{"source": sid, "path": path, "line": line} for sid, path, line in users],
                "confidence": "heuristic",
            }
        )
        if len(dangling) >= limit:
            break
    return dangling


def inspect_impact(
    graph: Graph,
    freshness_fn,
    base: Optional[str] = None,
    paths: Optional[Sequence[str]] = None,
    depth: int = 2,
    budget: Optional[int] = None,
) -> dict:
    budget = normalize_budget(budget)
    if not isinstance(depth, int) or depth < 0 or depth > 4:
        raise EngineError("depth must be between 0 and 4")
    changes, notes = collect_changes(graph, base, paths)

    changed: List[dict] = []
    changed_symbols: List[dict] = []
    not_analyzed: List[dict] = []
    deleted: List[str] = []
    risks: List[dict] = []
    warnings: List[str] = []
    layers: Dict[str, List[str]] = {}

    parse_errors = {entry["path"]: entry for entry in graph.index.parse_errors(changes.keys())}
    for path in sorted(changes):
        layers.setdefault(_classify_path(path), []).append(path)
        for prefix, kind, action in _SPECIAL_PATHS:
            if path == prefix or path.startswith(prefix):
                risks.append({"kind": kind, "path": path, "action": action, "confidence": "verified_static"})
                break
        if not path.endswith(".py"):
            not_analyzed.append({"path": path, "reason": "non-Python file: not statically analysed"})
            continue
        on_disk = (graph.index.scope.root / path).is_file()
        indexed = graph.module_symbol(path)
        if not on_disk:
            deleted.append(path)
            module_fq = module_name_for(path)
            dangling = _dangling_references(graph, module_fq)
            if dangling:
                risks.append({"kind": "deleted-module-still-referenced", "path": path, "references": dangling, "confidence": "heuristic"})
            continue
        if path in parse_errors:
            warnings.append(f"{path}: {parse_errors[path]['status']} ({parse_errors[path]['error']}); symbols unknown")
            continue
        if indexed is None:
            not_analyzed.append({"path": path, "reason": "outside the indexed tree (src/, tests/, scripts/) or excluded"})
            continue
        symbols = _changed_symbols(graph, path, changes[path])
        changed_symbols.extend(symbols)
        changed.append(
            {
                "path": path,
                "mode": "whole-file" if changes[path] is None else "hunks",
                "symbols": [s["id"].split("::", 1)[1] for s in symbols][:25],
                **({"symbols_total": len(symbols)} if len(symbols) > 25 else {}),
            }
        )
        if path in DOMAIN_INPUTS:
            risks.append(
                {
                    "kind": "api-registration-source",
                    "path": path,
                    "action": "Run check_api_drift and validate_api_artifacts; update tests/api contract tests.",
                    "confidence": "verified_static",
                }
            )
        dangling = _dangling_references(graph, indexed["fqname"])
        if dangling:
            risks.append({"kind": "unresolved-references-into-changed-module", "path": path, "references": dangling, "confidence": "heuristic"})

    seen_wire = set()
    wire_surfaces: List[dict] = []
    for symbol in changed_symbols:
        chain = graph.wire_chain(symbol["fqname"])
        wires = sorted({edge["from"] if edge["from"].startswith("wire:") else edge["to"] for edge in chain if "wire:" in (edge["from"] + edge["to"])})
        for wire in wires:
            if wire in seen_wire:
                continue
            seen_wire.add(wire)
            capabilities = sorted({e["to"] for e in graph.domain(wire) if e["kind"] == "uses_capability"})
            clients = sorted({e["from"] for e in graph.domain(wire) if e["kind"] == "api_to_wire"})
            wire_surfaces.append(
                {
                    "wire": wire.split(":", 1)[1],
                    "changed": symbol["id"].split("::", 1)[1],
                    "clients": [c.rsplit(".", 1)[-1] for c in clients],
                    "capabilities": [c.split(":", 1)[1] for c in capabilities],
                }
            )
    if wire_surfaces:
        risks.insert(
            0,
            {
                "kind": "wire-protocol-surface",
                "count": len(wire_surfaces),
                "action": "Keep request/result shapes compatible; update tests/api and the daemon dispatch tests.",
                "confidence": "verified_static",
            },
        )

    dependents, deps_truncated = graph.reverse_dependencies(changed_symbols, depth=depth, limit=150) if changed_symbols else ([], False)
    possible: List[dict] = []
    for symbol in changed_symbols:
        if symbol["kind"] not in ("method", "async_method"):
            continue
        total = graph.incoming(symbol)["possible_total"]
        if total:
            possible.append({"symbol": symbol["id"], "unresolved_name_matches": total})
    possible.sort(key=lambda item: (-item["unresolved_name_matches"], item["symbol"]))

    direct_tests = related_tests(graph, changed_symbols, limit=30)
    dependent_symbols = [graph.symbol(d["id"]) for d in dependents if d["depth"] == 1]
    indirect_tests = related_tests(graph, [s for s in dependent_symbols if s], limit=20)
    seen_tests = {entry["test"] for entry in direct_tests}
    indirect_tests = [dict(entry, via="dependent") for entry in indirect_tests if entry["test"] not in seen_tests]
    changed_tests = [path for path in changes if path.startswith("tests/") and path.endswith(".py") and path not in deleted]
    focused_files = unique_test_files([{"test": path} for path in changed_tests] + direct_tests + indirect_tests)

    high_risk = bool(seen_wire) or any(r["kind"] in ("api-registration-source", "generated-api-schema") for r in risks)
    fallback_suites: List[str] = []
    try:
        from ..architecture import recommend_tests

        existing = [p for p in changes if p not in deleted]
        if existing:
            fallback_suites = recommend_tests(graph.index.scope, existing)["suites"]
    except Exception as error:  # architecture errors must not hide the rest
        warnings.append(f"layer suite recommendation failed: {error}")
    if high_risk and "api" not in fallback_suites:
        # Wire-protocol / registration changes are API contract changes even
        # when only daemon files moved.
        fallback_suites = sorted({*fallback_suites, "api"})
    unmapped = [
        s["id"]
        for s in changed_symbols
        if s["kind"] != "module"
        and not any(t["target"] == s["id"] and t["evidence"] in STRONG_EVIDENCE for t in direct_tests)
    ]
    test_plan = {
        "focused_files": focused_files[:20],
        "direct": direct_tests,
        "via_dependents": indirect_tests,
        "suites_fallback": fallback_suites,
        "unmapped_changed_symbols": unmapped[:20],
        "broad_run_recommended": bool(unmapped) or high_risk,
        "reason": (
            "Changed symbols without static test evidence or wire-protocol/registration changes: run the suites too."
            if (unmapped or high_risk)
            else "Every changed symbol has static test evidence."
        ),
    }

    if not changes:
        warnings.append("no changes found")
    result = envelope(
        "inspect_impact",
        freshness_fn(),
        status="ok",
        input={"base": base, "paths": list(paths) if paths else None, "depth": depth},
        notes=notes,
        layers={key: sorted(value) for key, value in sorted(layers.items())},
        changed=changed,
        deleted=deleted,
        risks=risks,
        wire_surfaces=wire_surfaces,
        dependents=dependents,
        dependents_truncated=deps_truncated,
        possible_dependents=possible[:10],
        possible_dependents_note=(
            "unresolved attribute calls with the same method name; inspect before trusting"
            if possible
            else None
        ),
        tests=test_plan,
        not_analyzed=not_analyzed,
        warnings=warnings,
        analysis_complete=not warnings and not deps_truncated,
    )
    return fit_to_budget(
        result,
        budget,
        [
            "possible_dependents",
            "tests.via_dependents",
            "wire_surfaces",
            "dependents",
            "tests.direct",
            "not_analyzed",
            "risks",
            "changed",
        ],
        next_context_ref=(changed_symbols[0]["id"] if changed_symbols else None),
    )


def compact(symbols: Sequence[dict]) -> List[dict]:
    return [compact_symbol(s) for s in symbols]


__all__ = ["UNRESOLVED", "collect_changes", "inspect_impact"]
