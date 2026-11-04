"""``diagnose_bug``: rank candidate locations for a failure from evidence.

Inputs are untrusted text (symptoms, a traceback, a pytest id). They are
parsed in memory and never written to the index or any cache; the reply
carries paths, line numbers, function names and the exception *type* — the
exception message only as a short, redacted preview.

Scoring is deterministic and every point is explained in ``evidence``:

==========================  =====  ============================================
evidence                    score  meaning
==========================  =====  ============================================
traceback-frame (innermost)  130   the deepest frame inside the checkout
traceback-frame              100   other in-repo frames, -5 per level outward
test-frame / failing-test     40   the failing test itself
wire-method mention           45   ``sessions.open`` → its daemon handler
client-method of wire         30   …and the client method that sends it
identifier mention            30   exact symbol name in the symptoms (÷ matches)
message-literal               35   a quoted phrase found verbatim in src/ (≤6 hits)
keyword-overlap             ≤ 25   rare symptom words in symbol/module names (IDF)
test-callee                   25   a symbol the failing test calls (resolved)
changed-file                 +20   candidate lives in a changed file
changed-dependency           +10   a callee/caller of the candidate changed
==========================  =====  ============================================

Confidence: ``high`` ≥ 100 with an exact frame, ``medium`` ≥ 50, else ``low``.
No candidate is invented: without evidence the result says so.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..architecture import _classify_path
from .graph import Graph, compact_symbol
from .models import EngineError, envelope, fit_to_budget, normalize_budget

MAX_SYMPTOMS = 16 * 1024
MAX_TRACEBACK = 64 * 1024

_FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>[^\s]+)', re.M)
_PYTEST_FRAME_RE = re.compile(r"^(?P<file>[\w./\\-]+\.py):(?P<line>\d+): in (?P<func>[^\s]+)", re.M)
_PYTEST_ID_RE = re.compile(r"(?P<path>tests/[\w/.-]+\.py)(?:::(?P<name>[\w:\[\]./-]+))?")
_EXCEPTION_RE = re.compile(
    r"^(?:E\s+)?(?P<type>(?:[A-Za-z_][\w]*\.)*[A-Z]\w*(?:Error|Exception|Exit|Interrupt|Warning|Failure|Fault))\b(?::\s?(?P<message>.*))?$",
    re.M,
)
_IDENTIFIER_RE = re.compile(r"\b(?:[a-z_][a-z0-9]*_[a-z0-9_]+|_[a-z][a-z0-9_]*|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
#: Words too common in bug reports or English to identify code.
_REPORT_STOPWORDS = frozenset(
    """about after again also always and another any are because been before being between both but can
    cannot could does doing down each either else even every fail failed fails failing failure from gets
    have here instead into just keep keeps like many more most much must never once only other over same
    seem seems should shows since some still such than that the their them then there these they thing
    this those through time times very want what when where which while will with within without would
    your sure feels app application error errors crash crashes crashed broken wrong issue problem bug
    work works working happen happens happened sometimes using used user users""".split()
)
_KEYWORD_RE = re.compile(r"[A-Za-z][A-Za-z]{3,}")
_STOPWORDS_EXTRA = frozenset({"sshpilot", "pilot", "window", "button", "click", "clicked", "open", "opens", "show", "shown"})
_KEYWORD_MAX_SYMBOLS = 8
_KEYWORD_PER_FILE = 2
_KEYWORD_MAX_POINTS = 25
#: Quoted phrases (error/UI messages) searched literally in src/ files.
_QUOTED_RE = re.compile(r"[\"“”']([^\"“”'\n]{12,160})[\"“”']")
_MAX_LITERAL_PHRASES = 4
_MAX_LITERAL_HITS = 6
#: Plain words count as identifiers only when written as code: `name` or name().
_CODE_WORD_RE = re.compile(r"`([A-Za-z_][\w.]*)`|\b([A-Za-z_]\w*)\(\)")
_WIRE_RE = re.compile(r"\b[a-z][a-z_]*(?:\.[a-z][a-z_]*)+\b")
_NATIVE_MARKERS = (
    ("Gtk-CRITICAL", "gtk-critical"),
    ("Gtk-WARNING", "gtk-warning"),
    ("GLib-CRITICAL", "glib-critical"),
    ("GLib-GObject-CRITICAL", "gobject-critical"),
    ("Segmentation fault", "native-crash"),
    ("SIGSEGV", "native-crash"),
    ("Fatal Python error", "native-crash"),
    ("core dumped", "native-crash"),
)
#: Symptom keywords → subsystem hints (heuristic only).
_SUBSYSTEM_KEYWORDS = (
    (("terminal", "vte", "pty", "scrollback"), "terminal"),
    (("sftp", "file manager", "transfer", "upload", "download"), "sftp"),
    (("askpass", "passphrase", "password prompt", "keyring", "otp", "2fa"), "authentication"),
    (("known_hosts", "host key", "fingerprint"), "host-keys"),
    (("port forward", "forwarding", "tunnel", "socks"), "port-forwarding"),
    (("x11", "display"), "x11"),
    (("rdp", "freerdp", "vnc", "telnet", "serial"), "non-ssh-protocols"),
    (("daemon", "sshpilotd", "rpc", "socket"), "daemon"),
    (("sidebar", "dialog", "window", "button", "row"), "gtk-ui"),
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"(?i)\b(pass(?:word|phrase)?|secret|token|api[_-]?key|authorization|cookie|otp)\b(\s*[:=]\s*|\s+)(\S+)"),
    re.compile(r"\b[A-Za-z0-9+/=_-]{32,}\b"),
)


def redact(text: str, limit: int = 160) -> str:
    """Redact likely secrets and clip *text* for display."""
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 3:
            cleaned = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", cleaned)
        else:
            cleaned = pattern.sub("<redacted>", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _repo_relative(graph: Graph, filename: str) -> Optional[str]:
    """Map a traceback filename to a checkout path (None if external)."""
    name = filename.replace("\\", "/")
    root = str(graph.index.scope.root).replace("\\", "/").rstrip("/") + "/"
    if name.startswith(root):
        return name[len(root):]
    for marker, prefix in (("/src/sshpilot/", "src/sshpilot/"), ("/site-packages/sshpilot/", "src/sshpilot/"), ("/tests/", "tests/")):
        if marker in name:
            return prefix + name.rsplit(marker, 1)[1]
    if name.startswith(("src/", "tests/", "scripts/")):
        return name
    return None


def parse_traceback(text: str) -> Tuple[List[dict], Optional[dict]]:
    frames = [
        {"file": m.group("file"), "line": int(m.group("line")), "func": m.group("func"), "pos": m.start()}
        for regex in (_FRAME_RE, _PYTEST_FRAME_RE)
        for m in regex.finditer(text)
    ]
    frames.sort(key=lambda f: f["pos"])
    exception = None
    for match in _EXCEPTION_RE.finditer(text):
        exception = {"type": match.group("type"), "message": match.group("message") or ""}
    return frames, exception


def _classify_subsystem(paths: Sequence[str], text: str) -> List[dict]:
    hints: List[dict] = []
    counts: Dict[str, int] = {}
    for path in paths:
        layer = _classify_path(path)
        parts = path.split("/")
        if parts[0] == "src" and len(parts) > 3:
            detail = parts[3] if len(parts) > 4 or not parts[2].endswith(".py") else parts[2]
        elif parts[0] == "src" and len(parts) == 3:
            detail = parts[2]
        else:
            detail = parts[1] if len(parts) > 2 else ""
        key = f"{layer}:{detail.removesuffix('.py')}" if detail else layer
        counts[key] = counts.get(key, 0) + 1
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]:
        hints.append({"subsystem": key, "evidence": f"{count} ranked candidate(s)", "confidence": "verified_static"})
    lowered = text.lower()
    for keywords, subsystem in _SUBSYSTEM_KEYWORDS:
        hits = [word for word in keywords if word in lowered]
        if hits:
            hints.append({"subsystem": subsystem, "evidence": f"symptom keywords: {', '.join(hits[:3])}", "confidence": "heuristic"})
    return hints[:6]


class _Ranker:
    def __init__(self, graph: Graph):
        self.graph = graph
        self.candidates: Dict[str, dict] = {}

    def add(self, symbol: dict, score: int, evidence: dict) -> None:
        entry = self.candidates.setdefault(symbol["id"], {"symbol": symbol, "score": 0, "evidence": []})
        entry["score"] += score
        if len(entry["evidence"]) < 6:
            entry["evidence"].append({**evidence, "points": score})

    def ranked(self) -> List[dict]:
        def key(entry):
            symbol = entry["symbol"]
            return (-entry["score"], symbol["path"].startswith("tests/"), symbol["path"], symbol["line"])

        return sorted(self.candidates.values(), key=key)


def diagnose(
    graph: Graph,
    freshness_fn,
    symptoms: str,
    traceback: Optional[str] = None,
    test_name: Optional[str] = None,
    changed_paths: Optional[Sequence[str]] = None,
    max_items: int = 8,
    budget: Optional[int] = None,
) -> dict:
    budget = normalize_budget(budget)
    if not isinstance(symptoms, str):
        raise EngineError("symptoms must be text")
    if len(symptoms) > MAX_SYMPTOMS or (traceback and len(traceback) > MAX_TRACEBACK):
        raise EngineError(f"symptoms are limited to {MAX_SYMPTOMS} and traceback to {MAX_TRACEBACK} characters")
    if not isinstance(max_items, int) or not 1 <= max_items <= 25:
        raise EngineError("max_items must be between 1 and 25")
    if not symptoms.strip() and not (traceback or "").strip() and not test_name:
        raise EngineError("provide symptoms, a traceback or a test name")

    text = "\n".join(part for part in (symptoms, traceback or "") if part)
    ranker = _Ranker(graph)
    uncertainties: List[str] = []
    observations: Dict[str, object] = {}

    # 1. Traceback frames -------------------------------------------------
    frames, exception = parse_traceback(text)
    repo_frames: List[Tuple[dict, str]] = []
    external = 0
    for frame in frames:
        path = _repo_relative(graph, frame["file"])
        if path is None:
            external += 1
            continue
        repo_frames.append((frame, path))
    observations["frames"] = {"total": len(frames), "in_repo": len(repo_frames), "external": external}
    if exception:
        observations["exception"] = {"type": exception["type"], "message_preview": redact(exception["message"])}
    drifted = 0
    unknown_files: List[str] = []
    for depth_from_inner, (frame, path) in enumerate(reversed(repo_frames)):
        if graph.module_symbol(path) is None:
            unknown_files.append(path)
            continue
        symbol = graph.symbol_at(path, frame["line"])
        how = "exact-line"
        if symbol is None or (symbol["name"] != frame["func"] and frame["func"] != "<module>"):
            # The trace may come from another revision: fall back to the name.
            named = [s for s in graph.in_file(path) if s["name"] == frame["func"]]
            if named:
                symbol = named[0]
                how = "function-name (line drifted)"
                drifted += 1
            elif symbol is None:
                continue
            else:
                how = "line-only (function name differs)"
                drifted += 1
        is_test = path.startswith("tests/")
        if is_test:
            score = 40
            kind = "test-frame"
        else:
            score = 130 if depth_from_inner == 0 else max(50, 100 - 5 * depth_from_inner)
            kind = "traceback-frame"
        ranker.add(symbol, score, {"kind": kind, "path": path, "line": frame["line"], "function": frame["func"], "match": how})
    if drifted:
        uncertainties.append(f"{drifted} frame(s) did not match the current source by line; the traceback may come from another revision")
    if unknown_files:
        uncertainties.append(f"frames in files not in the index: {', '.join(sorted(set(unknown_files))[:3])}")
    if frames and not repo_frames:
        uncertainties.append("every traceback frame is outside the checkout (library or installed copy)")

    # 2. Native / GTK errors without a Python trace ------------------------
    native = sorted({label for marker, label in _NATIVE_MARKERS if marker in text})
    if native:
        observations["native_errors"] = native
        if not repo_frames:
            uncertainties.append("native GTK/GLib or crash output has no Python frames; static analysis cannot localise it")

    # 3. Failing test ---------------------------------------------------------
    test_ids = []
    if test_name:
        if len(test_name) > 512:
            raise EngineError("test_name is limited to 512 characters")
        test_ids.extend(m.group(0) for m in _PYTEST_ID_RE.finditer(test_name))
        if not test_ids and test_name.startswith("test"):
            test_ids.append(test_name)
    test_ids.extend(m.group(0) for m in _PYTEST_ID_RE.finditer(text))
    seen_tests = set()
    for test_id in test_ids[:5]:
        if test_id in seen_tests:
            continue
        seen_tests.add(test_id)
        path, _, name = test_id.partition("::")
        if path.endswith(".py"):
            qual = name.split("[")[0].replace("::", ".") if name else "<module>"
            symbol = graph.symbol(f"{path}::{qual}") or graph.module_symbol(path)
        else:
            matches = [s for s in graph.by_name(test_id.split("[")[0]) if s["path"].startswith("tests/")]
            symbol = matches[0] if len(matches) == 1 else None
        if symbol is None:
            uncertainties.append(f"failing test {test_id!r} not found in the index")
            continue
        ranker.add(symbol, 40, {"kind": "failing-test", "test": test_id})
        for edge in graph.outgoing(symbol, kinds=("calls",)):
            if edge["confidence"] == "unresolved" or "target" not in edge:
                continue
            target = graph.symbol(edge["target"])
            if target and not target["path"].startswith("tests/"):
                ranker.add(target, 25, {"kind": "test-callee", "test": test_id, "line": edge["line"], "confidence": edge["confidence"]})

    # 4. Wire methods and identifiers mentioned in the text ---------------
    for wire in sorted(set(_WIRE_RE.findall(text)))[:20]:
        edges = graph.domain(f"wire:{wire}")
        for edge in edges:
            if edge["kind"] == "wire_to_handler":
                for symbol in graph.by_fqname(edge["to"]):
                    ranker.add(symbol, 45, {"kind": "wire-method-mention", "wire": wire})
            elif edge["kind"] == "api_to_wire":
                for symbol in graph.by_fqname(edge["from"]):
                    ranker.add(symbol, 30, {"kind": "client-method-of-wire", "wire": wire})
    frame_funcs = {frame["func"] for frame, _path in repo_frames}
    code_words = [a or b for a, b in _CODE_WORD_RE.findall(symptoms)]
    code_words = [word.rsplit(".", 1)[-1] for word in code_words]
    identifiers = [
        token
        for token in dict.fromkeys([*_IDENTIFIER_RE.findall(symptoms), *code_words])
        if token not in frame_funcs and len(token) > 2
    ][:25]
    for token in identifiers:
        matches = [s for s in graph.by_name(token, limit=12) if s["kind"] not in ("variable",)]
        if not matches:
            continue
        share = max(5, 30 // len(matches))
        for symbol in matches[:6]:
            ranker.add(symbol, share, {"kind": "identifier-mention", "token": token, "matches": len(matches)})

    # 5. Quoted messages: literal search in indexed src/ files -------------
    phrases = [p.strip() for p in _QUOTED_RE.findall(symptoms) if len(p.strip()) >= 12]
    phrases = list(dict.fromkeys(phrases))[:_MAX_LITERAL_PHRASES]
    if phrases:
        literal_hits = _literal_hits(graph, phrases)
        observations["quoted_phrases"] = {"searched": len(phrases), "files_with_hits": len({h[0] for h in literal_hits})}
        for path, line, phrase in literal_hits:
            symbol = graph.symbol_at(path, line)
            if symbol is not None:
                ranker.add(symbol, 35, {"kind": "message-literal", "path": path, "line": line, "phrase": redact(phrase, limit=60)})

    # 6. Weak tier: symptom words vs symbol and module names (heuristic) ---
    credited = {token.lower() for token in identifiers} | {f["func"].lower() for f, _p in repo_frames}
    keyword_hits = _keyword_candidates(graph, symptoms, exclude=credited)
    if keyword_hits:
        observations["keyword_leads"] = len(keyword_hits)
    for symbol, matched, points in keyword_hits:
        ranker.add(symbol, points, {"kind": "keyword-overlap", "words": matched})

    # 7. Changed files --------------------------------------------------------
    changed = sorted({p.strip().replace("\\", "/") for p in (changed_paths or []) if isinstance(p, str) and p.strip()})
    for path in changed:
        graph.index.scope.resolve(path)  # confinement; raises ScopeError
    if changed:
        changed_set = set(changed)
        for entry in list(ranker.candidates.values()):
            symbol = entry["symbol"]
            if symbol["path"] in changed_set:
                ranker.add(symbol, 20, {"kind": "changed-file", "path": symbol["path"]})
            neighbours = [e.get("target", "") for e in graph.outgoing(symbol) if e["confidence"] != "unresolved"]
            neighbours += [e["source"] for e in graph.incoming(symbol, include_possible=False)["resolved"]]
            touched = sorted({n.split("::", 1)[0] for n in neighbours if n and n.split("::", 1)[0] in changed_set})
            if touched and symbol["path"] not in changed_set:
                ranker.add(symbol, 10, {"kind": "changed-dependency", "paths": touched[:3]})

    ranked = ranker.ranked()
    candidates = []
    for entry in ranked[:max_items]:
        symbol = entry["symbol"]
        exact = any(e["kind"] == "traceback-frame" and e.get("match") == "exact-line" for e in entry["evidence"])
        confidence = "high" if entry["score"] >= 100 and exact else ("medium" if entry["score"] >= 50 else "low")
        candidates.append(
            {
                **compact_symbol(symbol),
                "score": entry["score"],
                "confidence": confidence,
                "evidence": entry["evidence"],
            }
        )
    if len(ranked) > 1 and ranked[0]["score"] == ranked[1]["score"]:
        uncertainties.append("top candidates are tied; evidence does not separate them")
    if not candidates:
        uncertainties.append("no evidence linked the failure to indexed code")
    elif candidates[0]["confidence"] == "low":
        uncertainties.append("only weak evidence (name mentions or test associations); treat candidates as leads, not a diagnosis")

    overall = candidates[0]["confidence"] if candidates else "none"
    next_steps = _next_steps(candidates, test_ids, native, exception, repo_frames)
    result = envelope(
        "diagnose_bug",
        freshness_fn(),
        status="ok",
        confidence=overall,
        observations=observations,
        subsystems=_classify_subsystem([c["path"] for c in candidates[:5]], text),
        candidates=candidates,
        uncertainties=uncertainties,
        next_steps=next_steps,
        persisted_input=False,
    )
    return fit_to_budget(
        result,
        budget,
        ["candidates", "next_steps", "subsystems", "uncertainties"],
        next_context_ref=candidates[0]["id"] if candidates else None,
    )


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _keyword_candidates(graph: Graph, text: str, exclude: Set[str] = frozenset()) -> List[Tuple[dict, List[str], int]]:
    """Symbols whose name or module path shares rare words with *text*.

    Weak, explicitly labelled evidence for reports without traces or code
    names. Words are weighted by inverse document frequency over symbol
    names, at most two symbols per file are kept, and points are capped below
    every strong evidence kind.
    """
    words = []
    for raw in _KEYWORD_RE.findall(text):
        word = raw.lower()
        if word in _REPORT_STOPWORDS or word in _STOPWORDS_EXTRA or word in exclude:
            continue
        stem = _stem(word)
        if stem not in words:
            words.append(stem)
    words = words[:16]
    if not words:
        return []
    total = graph.db.execute("SELECT COUNT(*) FROM symbols WHERE path LIKE 'src/%'").fetchone()[0] or 1
    matched: Dict[str, Set[str]] = {}
    weights: Dict[str, float] = {}
    rows_by_id: Dict[str, dict] = {}
    for word in words:
        like = f"%{word}%"
        rows = graph.db.execute(
            "SELECT sid, id, path, qualname, name, fqname, kind, line, end_line, signature, doc, parent FROM symbols "
            "WHERE path LIKE 'src/%' AND kind IN ('class','function','async_function','method','async_method') "
            "AND (name LIKE ? OR path LIKE ?) LIMIT 3000",
            (like, like),
        ).fetchall()
        hits = []
        for row in rows:
            symbol = dict(zip(("sid", "id", "path", "qualname", "name", "fqname", "kind", "line", "end_line", "signature", "doc", "parent"), row))
            parts = {_stem(p.lower()) for p in re.split(r"[_/.]|(?<=[a-z0-9])(?=[A-Z])", symbol["name"] + "/" + symbol["path"]) if p}
            if word in parts:
                hits.append(symbol)
        if not hits:
            continue
        weights[word] = math.log(total / (1 + len(hits)))
        if weights[word] <= 0.5:
            continue  # matches a large share of the code base: not informative
        for symbol in hits:
            rows_by_id[symbol["id"]] = symbol
            matched.setdefault(symbol["id"], set()).add(word)
    scored = []
    for symbol_id, found in matched.items():
        score = sum(weights[word] for word in found)
        scored.append((score, symbol_id, sorted(found)))
    scored.sort(key=lambda item: (-item[0], rows_by_id[item[1]]["path"], rows_by_id[item[1]]["line"]))
    per_file: Dict[str, int] = {}
    results: List[Tuple[dict, List[str], int]] = []
    for score, symbol_id, found in scored:
        symbol = rows_by_id[symbol_id]
        if per_file.get(symbol["path"], 0) >= _KEYWORD_PER_FILE:
            continue
        per_file[symbol["path"]] = per_file.get(symbol["path"], 0) + 1
        results.append((symbol, found, min(_KEYWORD_MAX_POINTS, max(1, int(round(score * 2))))))
        if len(results) >= _KEYWORD_MAX_SYMBOLS:
            break
    return results


def _literal_hits(graph: Graph, phrases: Sequence[str]) -> List[Tuple[str, int, str]]:
    """Exact (case-insensitive) phrase hits in indexed ``src/`` files.

    Reads the files at query time; nothing is stored. A phrase matching more
    than ``_MAX_LITERAL_HITS`` places is too generic to be evidence and is
    dropped.
    """
    lowered = [phrase.lower() for phrase in phrases]
    hits: Dict[str, List[Tuple[str, int, str]]] = {phrase: [] for phrase in phrases}
    root = graph.index.scope.root
    for (path,) in graph.db.execute("SELECT path FROM files WHERE status='ok' AND path LIKE 'src/%' ORDER BY path"):
        try:
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        haystack = text.lower()
        for phrase, needle in zip(phrases, lowered):
            start = haystack.find(needle)
            while start != -1 and len(hits[phrase]) <= _MAX_LITERAL_HITS:
                line = haystack.count("\n", 0, start) + 1
                hits[phrase].append((path, line, phrase))
                start = haystack.find(needle, start + 1)
    found: List[Tuple[str, int, str]] = []
    for phrase in phrases:
        if 0 < len(hits[phrase]) <= _MAX_LITERAL_HITS:
            found.extend(hits[phrase])
    return found


def _next_steps(candidates, test_ids, native, exception, repo_frames) -> List[str]:
    steps: List[str] = []
    if candidates:
        steps.append(f"get_context(reference={candidates[0]['id']!r}) for source, callers and tests")
        if len(candidates) > 1:
            steps.append(f"compare with get_context(reference={candidates[1]['id']!r})")
    for test_id in list(dict.fromkeys(test_ids))[:1]:
        path = test_id.split("::", 1)[0]
        if path.startswith("tests/"):
            steps.append(f"reproduce: python -m pytest -q {test_id}")
    if native:
        steps.append("reproduce with `sshpilot --fatal-warnings` (backtrace at the first GTK/GLib warning) or `--diagnostics`; see docs/diagnostics.md")
    if not repo_frames and not native and not test_ids:
        steps.append("capture a Python traceback (`sshpilot --diagnostics`, then the frontend/daemon logs and crash.log)")
    if exception and exception["type"].endswith(("TimeoutError", "Timeout")):
        steps.append("timeouts are often environmental: rerun pinned to fewer cores to separate a race from a hang")
    if candidates:
        steps.append("inspect_impact(paths=[...]) on the suspected file to see dependents and tests before editing")
    return steps
