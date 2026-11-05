"""``plan_feature``: existing patterns, affected surfaces and obligations.

Advisory and evidence-backed. Patterns are indexed symbols and API methods
whose names share words with the request (each with its path and the words
that matched); obligations are SSH Pilot's documented layer rules, each tied
to a path that exists in this checkout. The tool never proposes code.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Set

from .. import api_surface
from ..architecture import _classify_path
from .graph import Graph, compact_symbol, resolve_reference
from .models import EngineError, envelope, fit_to_budget, normalize_budget
from .tests import related_tests

MAX_REQUEST = 4000
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_STOPWORDS = frozenset(
    """a an and are as at be but by can do does for from has have how i if in into is it its let make
    must new not of on or our should so that the their them then there these this to use user users
    want when which while will with would add adds adding allow allows support supports feature option
    able also any each more some show shows show way via like need needs""".split()
)
_API_KEYWORDS = frozenset({"api", "daemon", "rpc", "wire", "capability", "capabilities", "client", "protocol", "headless", "sshpilotd"})
_LAYER_WORDS = frozenset({"gtk", "frontend", "core", "backend", "server", "method", "methods", "endpoint"})
_TERMINAL_INPUT_WORDS = frozenset({"terminal", "key", "keyboard", "shortcut", "shortcuts", "mouse", "scroll", "paste", "copy", "selection", "vte"})
_UI_TEXT_KEYWORDS = frozenset({"label", "text", "message", "dialog", "button", "string", "tooltip", "error", "notification", "menu", "title"})
_SUBSYSTEM_PREFIXES = {
    "daemon": ("src/sshpilot/daemon/",),
    "api": ("src/sshpilot/api/",),
    "core": ("src/sshpilot/core/",),
    "gtk": ("src/sshpilot/gtk/", "src/sshpilot/"),
    "frontend": ("src/sshpilot/gtk/", "src/sshpilot/"),
    "cli": ("src/sshpilot/cli/",),
    "mcp": ("src/sshpilot/mcp/",),
    "plugins": ("src/sshpilot/plugins/",),
}


def _words(text: str) -> List[str]:
    found = []
    for raw in _WORD_RE.findall(text):
        for part in re.split(r"(?<=[a-z0-9])(?=[A-Z])|_", raw):
            word = part.lower()
            if len(word) >= 3 and word not in _STOPWORDS and word not in found:
                found.append(word)
    return found


def _name_words(name: str) -> Set[str]:
    return {w.lower() for w in re.split(r"[_.]|(?<=[a-z0-9])(?=[A-Z])", name) if len(w) >= 3}


def _existing(graph: Graph, paths: Sequence[str]) -> List[str]:
    root = graph.index.scope.root
    return [path for path in paths if (root / path.rstrip("/")).exists()]


def _api_obligations(graph: Graph, analogue: Optional[dict]) -> List[dict]:
    """The layered API rules, each tied to a path present in the checkout."""
    rows = [
        ("request/result models", ["src/sshpilot/api/models/"], "Typed DTOs for the request, result, events and error codes."),
        ("client contract", [api_surface.CLIENT_RELPATH, api_surface.DAEMON_CLIENT_RELPATH], "SshPilotClient method + DaemonClient wire call (self._request('<wire>'))."),
        ("client capability tables", [api_surface.IMPLEMENTED_RELPATH, api_surface.DAEMON_CLIENT_RELPATH, api_surface.UNSUPPORTED_RELPATH], "Register the method's status/capability exactly once."),
        ("capability enum", [api_surface.CAPABILITIES_RELPATH], "Advertise availability only when the full implementation exists."),
        ("daemon dispatch", [api_surface.DISPATCH_RELPATH], "HANDLERS entry + DAEMON_METHOD_CAPABILITIES row; authoritative state stays in the daemon."),
        ("wire codec", ["src/sshpilot/api/transport/codec.py"], "*_to_wire / *_from_wire for new models."),
        ("generated artifacts", ["scripts/generate_api_artifacts.py", api_surface.SCHEMA_RELPATH], "Regenerate; check with validate_api_artifacts. Never hand-edit."),
        ("user-visible text", ["src/sshpilot/gtk/", "docs/development/localization.md", "po/LINGUAS"], "Daemon/core/API send code enums + params; a gtk/<feature>_messages.py presenter owns wording; ship translations. New codes bump the API version."),
        ("contract tests", ["tests/api/", "tests/architecture/"], "Contract + i18n-boundary tests; headless before GTK."),
        ("docs", ["docs/api/CHANGELOG.md", "docs/api/"], "API changelog entry; do not bump the wire protocol unless compatibility changes."),
    ]
    obligations = []
    for surface, paths, rule in rows:
        present = _existing(graph, paths)
        entry = {"surface": surface, "paths": present, "rule": rule}
        missing = [p for p in paths if p not in present]
        if missing:
            entry["missing_paths"] = missing
        obligations.append(entry)
    if analogue:
        obligations.insert(0, {"surface": "copy-the-pattern-of", "pattern": analogue, "rule": "Closest existing API method by name; follow its client → wire → handler → capability chain."})
    return obligations


def _frontend_obligations(graph: Graph, ui_text: bool, terminal_input: bool) -> List[dict]:
    obligations = [
        {"surface": "frontend only", "paths": _existing(graph, ["src/sshpilot/gtk/", "src/sshpilot/"]), "rule": "Keep logic headless where possible; GTK code stays out of core/api/daemon (frontend-neutrality)."},
        {"surface": "GUI tests", "paths": _existing(graph, ["tests/"]), "rule": "Prefer a headless unit test; GUI tests need Xvfb."},
    ]
    if terminal_input:
        obligations.insert(
            1,
            {"surface": "terminal input", "paths": _existing(graph, ["docs/architecture/terminal-input-routing.md"]), "rule": "Event-controller order matters; read before adding controllers to the terminal."},
        )
    if ui_text:
        obligations.append(
            {"surface": "user-visible text", "paths": _existing(graph, ["po/LINGUAS", "docs/development/localization.md", "po/POTFILES"]), "rule": "Wrap with _(); translate every po/LINGUAS catalogue and commit rebuilt .mo files; new files go in po/POTFILES."}
        )
    return obligations


def plan_feature(
    graph: Graph,
    freshness_fn,
    request: str,
    subsystem: Optional[str] = None,
    symbol: Optional[str] = None,
    budget: Optional[int] = None,
) -> dict:
    budget = normalize_budget(budget)
    if not isinstance(request, str) or not request.strip():
        raise EngineError("request must be non-empty text")
    if len(request) > MAX_REQUEST:
        raise EngineError(f"request is limited to {MAX_REQUEST} characters")
    if subsystem is not None and subsystem not in _SUBSYSTEM_PREFIXES:
        raise EngineError(f"subsystem must be one of {', '.join(sorted(_SUBSYSTEM_PREFIXES))}")

    lowered = set(w.lower() for w in _WORD_RE.findall(request))
    # Layer words ("api", "daemon") steer obligations; they are not feature
    # names and would match unrelated symbols such as ``DaemonClient``.
    words = [w for w in _words(request) if w not in _API_KEYWORDS and w not in _LAYER_WORDS]
    uncertainties: List[str] = []
    prefixes = _SUBSYSTEM_PREFIXES.get(subsystem or "", ())

    # -- anchor symbol -----------------------------------------------------------
    anchor = None
    if symbol:
        found, _how = resolve_reference(graph, symbol)
        if len(found) == 1:
            anchor = found[0]
        elif found:
            uncertainties.append(f"symbol {symbol!r} is ambiguous ({len(found)} matches); pass a symbol id")
        else:
            uncertainties.append(f"symbol {symbol!r} not found")

    # -- API method patterns -------------------------------------------------
    api_patterns: List[dict] = []
    try:
        schema = api_surface.load_schema(graph.index.scope)
        client_methods = schema["client_method_contract"]
        wire_methods = schema["daemon_method_contract"]
    except (ValueError, OSError, Exception):
        client_methods, wire_methods = {}, {}
        uncertainties.append("generated API schema unavailable; API patterns not searched")
    word_set = set(words)
    for name, entry in client_methods.items():
        overlap = sorted(_name_words(name) & word_set)
        if overlap:
            api_patterns.append({"client_method": name, "matched": overlap, "status": entry.get("status"), "capability": entry.get("capability")})
    for name in wire_methods:
        overlap = sorted(_name_words(name) & word_set)
        if overlap and not any(name == p.get("wire") for p in api_patterns):
            api_patterns.append({"wire": name, "matched": overlap})
    api_patterns.sort(key=lambda p: (-len(p["matched"]), p.get("client_method") or p.get("wire")))

    # -- symbol patterns -----------------------------------------------------
    scores: Dict[str, dict] = {}
    for word in words[:12]:
        rows = graph.db.execute(
            "SELECT id, name, path, kind, line, end_line FROM symbols WHERE kind IN "
            "('class','function','async_function','method','async_method') AND name LIKE ? "
            "AND path NOT LIKE 'tests/%' ORDER BY length(name), path LIMIT 60",
            (f"%{word}%",),
        ).fetchall()
        for sid, name, path, kind, line, end_line in rows:
            if prefixes and not path.startswith(prefixes):
                continue
            if word not in _name_words(name):
                continue  # substring of a longer word, e.g. "port" in "report"
            entry = scores.setdefault(sid, {"id": sid, "kind": kind, "path": path, "lines": [line, end_line], "matched": []})
            if word not in entry["matched"]:
                entry["matched"].append(word)
    patterns = sorted(
        scores.values(),
        key=lambda e: (-len(e["matched"]), e["kind"] not in ("class",), e["path"], e["lines"][0]),
    )

    # -- layers & API involvement --------------------------------------------
    top_paths = [p["path"] for p in patterns[:15]]
    if anchor:
        top_paths.insert(0, anchor["path"])
    layer_counts: Dict[str, int] = {}
    for path in top_paths:
        layer = _classify_path(path)
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
    explicit_api = bool(lowered & _API_KEYWORDS) or subsystem in ("api", "daemon")
    strong_api = [p for p in api_patterns if len(p["matched"]) >= 2 or (len(words) <= 2 and p["matched"])]
    frontend_only = (
        not explicit_api
        and not strong_api
        and (subsystem in ("gtk", "frontend") or (layer_counts and set(layer_counts) <= {"frontend", "application"}))
    )
    nothing_matched = not patterns and not api_patterns and not anchor
    if nothing_matched and not explicit_api:
        obligations = []
        api_involvement = "unknown"
    elif frontend_only:
        obligations = _frontend_obligations(
            graph, bool(lowered & _UI_TEXT_KEYWORDS), bool(lowered & _TERMINAL_INPUT_WORDS)
        )
        api_involvement = "none-detected"
    else:
        analogue = None
        best = next((p for p in api_patterns if "client_method" in p), None)
        if best:
            trace = None
            try:
                trace = api_surface.inspect_method(graph.index.scope, best["client_method"])
            except Exception:
                trace = None
            analogue = {
                "client_method": best["client_method"],
                "matched": best["matched"],
                "wire_methods": [w["method"] for w in (trace or {}).get("wire_methods", [])],
                "handlers": [w["handler"] for w in (trace or {}).get("wire_methods", []) if w.get("handler")],
            }
        obligations = _api_obligations(graph, analogue)
        api_involvement = "explicit" if explicit_api else ("pattern-match" if strong_api else "possible")
        if not explicit_api and not strong_api:
            uncertainties.append("unclear whether the daemon/API is involved; obligations listed are the API path — drop them if the feature is UI-only")

    if nothing_matched:
        uncertainties.append("no existing pattern matched the request words; describe the feature with code names (e.g. `forward`, `sftp`) or pass symbol/subsystem")

    pattern_symbols = [graph.symbol(p["id"]) for p in patterns[:5]]
    if anchor:
        pattern_symbols.insert(0, anchor)
    tests = related_tests(graph, [s for s in pattern_symbols if s], limit=10)
    suites = sorted({layer for layer in layer_counts if layer in ("api", "core", "daemon", "mcp")} | ({"api", "architecture"} if api_involvement in ("explicit", "pattern-match", "possible") else set()))

    result = envelope(
        "plan_feature",
        freshness_fn(),
        status="ok",
        request_words=words[:12],
        anchor=({**compact_symbol(anchor), "fqname": anchor["fqname"], "api_chain": graph.wire_chain(anchor["fqname"])[:6]} if anchor else None),
        layers=dict(sorted(layer_counts.items(), key=lambda item: -item[1])),
        api_involvement=api_involvement,
        api_patterns=api_patterns[:8],
        patterns=patterns[:12],
        obligations=obligations,
        test_plan={"related_tests": tests, "suites": suites, "note": "related tests exercise the matched patterns; new behaviour needs new tests"},
        uncertainties=uncertainties,
        advisory="Patterns are name matches with paths, not instructions; read them with get_context before deciding.",
    )
    return fit_to_budget(
        result,
        budget,
        ["test_plan.related_tests", "patterns", "api_patterns", "obligations"],
        next_context_ref=(patterns[0]["id"] if patterns else None),
    )
