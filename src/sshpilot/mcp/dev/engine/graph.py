"""Query-time resolution and bounded traversal over the index.

Edges are stored with the *name* the source used; this module resolves them
against the current symbol table on every query and labels the outcome:

* ``verified_static`` – a static name binding that resolves to an indexed
  definition;
* ``heuristic``       – ``self.m()`` / ``super()`` dispatch, callbacks,
  signal handlers, re-export hops — plausible, not proven;
* ``unresolved``      – no indexed definition (external library, dynamic
  attribute, unknown receiver). Never promoted to a stronger label.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .extract import UNKNOWN_OWNER
from .index import DevIndex
from .models import CONFIDENCE_RANK, HEURISTIC, UNRESOLVED, VERIFIED_STATIC, EngineError

_SYMBOL_COLUMNS = "sid, id, path, qualname, name, fqname, kind, line, end_line, signature, doc, parent"
#: Edge kinds followed when walking *dependents* of a symbol.
DEPENDENCY_KINDS = ("calls", "references", "inherits", "imports", "connects_signal")
_MAX_ALIAS_HOPS = 3
_MAX_BASE_DEPTH = 5


def symbol_row(row: Sequence) -> dict:
    keys = [key.strip() for key in _SYMBOL_COLUMNS.split(",")]
    return dict(zip(keys, row))


def compact_symbol(symbol: dict) -> dict:
    """The few fields an agent needs to locate a symbol."""
    return {
        "id": symbol["id"],
        "kind": symbol["kind"],
        "path": symbol["path"],
        "lines": [symbol["line"], symbol["end_line"]],
    }


class Graph:
    """Read-only queries. Callers must refresh the index first."""

    def __init__(self, index: DevIndex):
        self.index = index
        self.db: sqlite3.Connection = index.db
        self._fq_cache: Dict[str, List[dict]] = {}

    # -- symbols ---------------------------------------------------------------
    def symbol(self, symbol_id: str) -> Optional[dict]:
        row = self.db.execute(f"SELECT {_SYMBOL_COLUMNS} FROM symbols WHERE id=?", (symbol_id,)).fetchone()
        return symbol_row(row) if row else None

    def by_fqname(self, fqname: str) -> List[dict]:
        if fqname not in self._fq_cache:
            rows = self.db.execute(
                f"SELECT {_SYMBOL_COLUMNS} FROM symbols WHERE fqname=? ORDER BY path, line", (fqname,)
            ).fetchall()
            self._fq_cache[fqname] = [symbol_row(row) for row in rows]
        return self._fq_cache[fqname]

    def by_name(self, name: str, limit: int = 50) -> List[dict]:
        rows = self.db.execute(
            f"SELECT {_SYMBOL_COLUMNS} FROM symbols WHERE name=? AND kind != 'module' ORDER BY path, line LIMIT ?",
            (name, limit),
        ).fetchall()
        return [symbol_row(row) for row in rows]

    def in_file(self, path: str) -> List[dict]:
        rows = self.db.execute(
            f"SELECT {_SYMBOL_COLUMNS} FROM symbols WHERE path=? ORDER BY line, end_line DESC", (path,)
        ).fetchall()
        return [symbol_row(row) for row in rows]

    def symbol_at(self, path: str, line: int, kinds: Optional[Iterable[str]] = None) -> Optional[dict]:
        """Innermost definition whose range contains *line* (module otherwise)."""
        wanted = set(kinds) if kinds else None
        containing = [
            symbol
            for symbol in self.in_file(path)
            if symbol["line"] <= line <= symbol["end_line"]
            and (wanted is None or symbol["kind"] in wanted)
        ]
        definitions = [s for s in containing if s["kind"] != "module"]
        if definitions:
            return min(definitions, key=lambda s: (s["end_line"] - s["line"], -s["line"]))
        modules = [s for s in containing if s["kind"] == "module"]
        if modules:
            return modules[0]
        return self.module_symbol(path) if wanted is None else None

    def module_symbol(self, path: str) -> Optional[dict]:
        rows = [s for s in self.in_file(path) if s["kind"] == "module"]
        return rows[0] if rows else None

    # -- resolution --------------------------------------------------------------
    def resolve_target(self, target: str, mechanism: str = "", src_class_fq: str = "") -> Tuple[List[dict], str]:
        """Resolve an edge target to symbols. Returns (symbols, how)."""
        if target.startswith(UNKNOWN_OWNER):
            return [], "unknown-receiver"
        if mechanism == "super-call" and "." in target:
            owner, attribute = target.rsplit(".", 1)
            for base in self._ancestors(owner):
                found = self.by_fqname(f"{base}.{attribute}")
                if found:
                    return found, "base-class"
            return [], "not-indexed"
        direct = self.by_fqname(target)
        if direct:
            return direct, "exact"
        # Re-export: ``pkg.Name`` where pkg/__init__ imports Name from a submodule.
        hopped = self._follow_reexport(target)
        if hopped:
            return hopped, "re-export"
        # self.m() defined on a base class.
        if mechanism in ("self-method", "self-method-reference") and "." in target:
            owner, attribute = target.rsplit(".", 1)
            for base in self._ancestors(owner):
                found = self.by_fqname(f"{base}.{attribute}")
                if found:
                    return found, "base-class"
        # ``module.Class.attr`` where attr is an instance attribute, not indexed.
        return [], "not-indexed"

    def _follow_reexport(self, target: str) -> List[dict]:
        current = target
        for _ in range(_MAX_ALIAS_HOPS):
            if "." not in current:
                return []
            owner, name = current.rsplit(".", 1)
            modules = [s for s in self.by_fqname(owner) if s["kind"] == "module"]
            if not modules:
                # Maybe ``pkg.Name.attr``: resolve the owner first.
                resolved = self._follow_reexport(owner) if owner.count(".") >= 1 else []
                for symbol in resolved:
                    found = self.by_fqname(f"{symbol['fqname']}.{name}")
                    if found:
                        return found
                return []
            row = self.db.execute(
                "SELECT target FROM edges WHERE src=? AND kind='imports' AND detail=? LIMIT 1",
                (modules[0]["sid"], name),
            ).fetchone()
            if row is None:
                return []
            current = row[0]
            found = self.by_fqname(current)
            if found:
                return found
        return []

    def _ancestors(self, class_fq: str) -> List[str]:
        """Base-class fq names (breadth-first, cycle-safe, *class_fq* excluded)."""
        order: List[str] = []
        seen = {class_fq}
        queue = [class_fq]
        for _ in range(_MAX_BASE_DEPTH):
            next_queue = []
            for current in queue:
                for symbol in self.by_fqname(current):
                    if symbol["kind"] != "class":
                        continue
                    for (base,) in self.db.execute(
                        "SELECT target FROM edges WHERE src=? AND kind='inherits' ORDER BY line, target",
                        (symbol["sid"],),
                    ):
                        resolved, _how = self.resolve_target(base)
                        for name in [s["fqname"] for s in resolved] or [base]:
                            if name not in seen:
                                seen.add(name)
                                order.append(name)
                                next_queue.append(name)
            queue = next_queue
            if not queue:
                break
        return order

    def subclasses(self, class_fq: str, depth: int = 3) -> List[str]:
        found: List[str] = []
        queue = [class_fq]
        seen = {class_fq}
        for _ in range(depth):
            next_queue = []
            for current in queue:
                for target in self.aliases_of(current):
                    for (src_id,) in self.db.execute(
                        "SELECT s.id FROM edges e JOIN symbols s ON s.sid = e.src "
                        "WHERE e.kind='inherits' AND e.target=? ORDER BY s.id",
                        (target,),
                    ):
                        symbol = self.symbol(src_id)
                        if symbol and symbol["fqname"] not in seen:
                            seen.add(symbol["fqname"])
                            found.append(symbol["fqname"])
                            next_queue.append(symbol["fqname"])
            queue = next_queue
        return found

    def aliases_of(self, fqname: str) -> List[str]:
        """Names under which *fqname* is importable (itself + re-exports)."""
        names = [fqname]
        frontier = [fqname]
        for _ in range(_MAX_ALIAS_HOPS):
            next_frontier = []
            for current in frontier:
                for module_fq, alias in self.db.execute(
                    "SELECT s.fqname, e.detail FROM edges e JOIN symbols s ON s.sid = e.src "
                    "WHERE e.kind='imports' AND e.target=? AND s.kind='module' ORDER BY s.fqname",
                    (current,),
                ):
                    if not alias or alias == "*":
                        continue
                    name = f"{module_fq}.{alias}"
                    if name not in names:
                        names.append(name)
                        next_frontier.append(name)
            frontier = next_frontier
        return names

    def _edge_confidence(self, stored: str, resolved: bool, how: str) -> str:
        if not resolved:
            return UNRESOLVED
        if stored == "static" and how == "exact":
            return VERIFIED_STATIC
        return HEURISTIC

    # -- relationships -------------------------------------------------------
    def outgoing(self, symbol: dict, kinds: Sequence[str] = ("calls", "references", "inherits", "connects_signal")) -> List[dict]:
        rows = self.db.execute(
            "SELECT kind, target, line, confidence, mechanism, detail FROM edges WHERE src=? ORDER BY line, kind, target",
            (symbol["sid"],),
        ).fetchall()
        results = []
        seen: Set[Tuple[str, str]] = set()
        for kind, target, line, stored, mechanism, detail in rows:
            if kind not in kinds:
                continue
            resolved, how = self.resolve_target(target, mechanism)
            key = (kind, resolved[0]["id"] if resolved else target)
            if key in seen:
                continue
            seen.add(key)
            entry = {
                "kind": kind,
                "line": line,
                "confidence": self._edge_confidence(stored, bool(resolved), how),
                "mechanism": mechanism,
            }
            if resolved:
                entry["target"] = resolved[0]["id"]
                if len(resolved) > 1:
                    entry["ambiguous_targets"] = len(resolved)
            else:
                entry["target_name"] = target
            if detail and kind == "connects_signal":
                entry["signal"] = detail
            results.append(entry)
        results.sort(key=lambda e: (CONFIDENCE_RANK[e["confidence"]], e["line"]))
        return results

    def incoming(self, symbol: dict, kinds: Sequence[str] = DEPENDENCY_KINDS, include_possible: bool = True) -> Dict[str, List[dict]]:
        """Dependents of *symbol*: ``resolved`` (verified/heuristic) and
        ``possible`` (unresolved attribute calls with the same name)."""
        targets = set(self.aliases_of(symbol["fqname"]))
        if symbol["kind"] in ("method", "async_method", "attribute") and symbol["parent"]:
            owner = self.symbol(symbol["parent"])
            if owner and owner["kind"] == "class":
                for sub in self.subclasses(owner["fqname"]):
                    targets.add(f"{sub}.{symbol['name']}")
        placeholders = ",".join("?" for _ in targets)
        rows = self.db.execute(
            f"SELECT s.id, s.path, e.kind, e.target, e.line, e.confidence, e.mechanism FROM edges e "
            f"JOIN symbols s ON s.sid = e.src WHERE e.target IN ({placeholders}) ORDER BY s.path, e.line",
            sorted(targets),
        ).fetchall()
        resolved: List[dict] = []
        seen: Set[Tuple[str, str]] = set()
        for src_id, src_path, kind, target, line, stored, mechanism in rows:
            if kind not in kinds or src_id == symbol["id"]:
                continue
            if (src_id, kind) in seen:
                continue
            seen.add((src_id, kind))
            how = "exact" if target == symbol["fqname"] else "re-export"
            confidence = VERIFIED_STATIC if stored == "static" and how == "exact" else HEURISTIC
            if stored == "unresolved":
                confidence = UNRESOLVED
            resolved.append(
                {"source": src_id, "path": src_path, "kind": kind, "line": line, "confidence": confidence, "mechanism": mechanism}
            )
        resolved.sort(key=lambda e: (CONFIDENCE_RANK[e["confidence"]], e["path"], e["line"]))
        possible: List[dict] = []
        possible_total = 0
        if include_possible and symbol["kind"] in ("method", "async_method", "function", "async_function"):
            possible_total = self.db.execute(
                "SELECT COUNT(*) FROM edges WHERE target=? AND kind='calls'", (UNKNOWN_OWNER + symbol["name"],)
            ).fetchone()[0]
            for src_id, src_path, line, mechanism in self.db.execute(
                "SELECT s.id, s.path, e.line, e.mechanism FROM edges e JOIN symbols s ON s.sid = e.src "
                "WHERE e.target=? AND e.kind='calls' ORDER BY s.path, e.line LIMIT 25",
                (UNKNOWN_OWNER + symbol["name"],),
            ):
                possible.append(
                    {"source": src_id, "path": src_path, "line": line, "confidence": UNRESOLVED, "mechanism": mechanism}
                )
        return {"resolved": resolved, "possible": possible, "possible_total": possible_total}

    def domain(self, node: str) -> List[dict]:
        """Domain edges touching *node* (an fq name or ``wire:``/``capability:``)."""
        rows = self.db.execute(
            "SELECT src, kind, target, path, line, confidence, mechanism, detail FROM domain_edges "
            "WHERE src=? OR target=? ORDER BY kind, src, target",
            (node, node),
        ).fetchall()
        return [
            {
                "from": src,
                "kind": kind,
                "to": target,
                "evidence": {"path": path, "line": line or None, "mechanism": mechanism},
                "confidence": confidence,
                **({"handler": detail} if kind == "wire_to_handler" and detail else {}),
            }
            for src, kind, target, path, line, confidence, mechanism, detail in rows
        ]

    def wire_chain(self, fqname: str) -> List[dict]:
        """Follow domain edges outward from a client/handler symbol (2 hops)."""
        chain: List[dict] = []
        seen: Set[Tuple[str, str, str]] = set()
        frontier = [fqname]
        for _ in range(3):
            next_frontier = []
            for node in frontier:
                for edge in self.domain(node):
                    key = (edge["from"], edge["kind"], edge["to"])
                    if key in seen:
                        continue
                    seen.add(key)
                    chain.append(edge)
                    other = edge["to"] if edge["from"] == node else edge["from"]
                    if other.startswith("wire:") or edge["kind"] == "implemented_by":
                        next_frontier.append(other)
            frontier = next_frontier
        return chain

    def domain_dependents(self, symbol: dict) -> List[dict]:
        """Client-side symbols that reach *symbol* through the wire protocol.

        handler ← ``wire:<m>`` ← ``DaemonClient.<m>`` ← ``SshPilotClient.<m>``.
        Each hop comes from a literal registration table (verified_static).
        """
        found: List[dict] = []
        wires = [e["from"] for e in self.domain(symbol["fqname"]) if e["kind"] == "wire_to_handler" and e["to"] == symbol["fqname"]]
        callers: List[Tuple[str, str]] = []
        for wire in wires:
            for edge in self.domain(wire):
                if edge["kind"] == "api_to_wire" and edge["to"] == wire:
                    callers.append((edge["from"], wire))
        for edge in self.domain(symbol["fqname"]):
            if edge["kind"] == "implemented_by" and edge["to"] == symbol["fqname"]:
                callers.append((edge["from"], "implemented_by"))
        for fqname, via in callers:
            for caller in self.by_fqname(fqname):
                found.append({"symbol": caller, "via": via})
        return found

    # -- traversal ---------------------------------------------------------------
    def reverse_dependencies(self, seeds: Sequence[dict], depth: int = 2, limit: int = 200) -> Tuple[List[dict], bool]:
        """Breadth-first dependents of *seeds* (deterministic, cycle-safe).

        Follows resolved incoming edges only (unresolved name matches are not
        dependencies). Imports are followed to the importing module but not
        beyond, so ``import x`` does not fan out to everything.
        Returns (entries, truncated).
        """
        if depth < 0 or depth > 5:
            raise EngineError("depth must be between 0 and 5")
        visited: Set[str] = {seed["id"] for seed in seeds}
        frontier = list(seeds)
        results: List[dict] = []
        truncated = False
        for level in range(1, depth + 1):
            next_frontier: List[dict] = []
            for current in sorted(frontier, key=lambda s: s["id"]):
                # Verified wire-protocol hops first: they are the strongest
                # evidence and must not be crowded out by the result cap.
                for dependent in self.domain_dependents(current):
                    source = dependent["symbol"]
                    if source["id"] in visited:
                        continue
                    visited.add(source["id"])
                    results.append(
                        {
                            "id": source["id"],
                            "path": source["path"],
                            "lines": [source["line"], source["end_line"]],
                            "depth": level,
                            "via": {"kind": "wire-protocol", "of": current["id"], "wire": dependent["via"]},
                            "confidence": VERIFIED_STATIC,
                        }
                    )
                    next_frontier.append(source)
                if len(results) >= limit:
                    truncated = True
                    break
                incoming = self.incoming(current, include_possible=False)["resolved"]
                for edge in incoming:
                    if edge["source"] in visited:
                        continue
                    visited.add(edge["source"])
                    source = self.symbol(edge["source"])
                    if source is None:
                        continue
                    results.append(
                        {
                            "id": source["id"],
                            "path": source["path"],
                            "lines": [source["line"], source["end_line"]],
                            "depth": level,
                            "via": {"kind": edge["kind"], "of": current["id"], "line": edge["line"]},
                            "confidence": edge["confidence"],
                        }
                    )
                    if len(results) >= limit:
                        truncated = True
                        break
                    if edge["kind"] != "imports":
                        next_frontier.append(source)
                if truncated:
                    break
            if truncated:
                break
            frontier = next_frontier
        results.sort(key=lambda e: (e["depth"], CONFIDENCE_RANK[e["confidence"]], e["path"], e["lines"][0]))
        return results, truncated


_REFERENCE_RE = re.compile(r"^(?P<path>[^:?]+\.py):(?P<line>\d+)$")


def resolve_reference(graph: Graph, reference: str) -> Tuple[List[dict], str]:
    """Resolve a user/agent reference to symbols.

    Accepts a symbol id (``path::Qual.name``), ``path.py:LINE``, a dotted fq
    name, a ``wire:``/``capability:`` node (returns []: handled by callers),
    or a bare name. Returns (candidates, how).
    """
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 512:
        raise EngineError("reference must be a non-empty string of at most 512 characters")
    reference = reference.strip()
    path_part = reference.split("::", 1)[0].split(":", 1)[0]
    if "/" in path_part or "\\" in path_part or path_part.startswith(".."):
        # Anything path-like is confined to the checkout before use.
        _check_path(graph, path_part.replace("\\", "/"))
    if "::" in reference:
        symbol = graph.symbol(reference)
        if symbol:
            return [symbol], "symbol-id"
        path = reference.split("::", 1)[0]
        _check_path(graph, path)
        return [], "unknown-symbol-id"
    match = _REFERENCE_RE.match(reference)
    if match:
        path = match.group("path")
        _check_path(graph, path)
        symbol = graph.symbol_at(path, int(match.group("line")))
        return ([symbol] if symbol else []), "path-line"
    if reference.endswith(".py") and "/" in reference:
        _check_path(graph, reference)
        module = graph.module_symbol(reference)
        return ([module] if module else []), "path"
    if reference.startswith(("wire:", "capability:")):
        return [], "domain-node"
    if "." in reference:
        found, how = graph.resolve_target(reference)
        if found:
            return found, f"fqname:{how}"
        tail = reference.rsplit(".", 1)[-1]
        candidates = [s for s in graph.by_name(tail) if s["fqname"].endswith(reference)]
        return candidates, "fqname-suffix"
    return graph.by_name(reference), "name"


def _check_path(graph: Graph, path: str) -> None:
    from ..._scope import ScopeError

    try:
        graph.index.scope.resolve(path)
    except ScopeError as error:
        raise EngineError(str(error)) from error
