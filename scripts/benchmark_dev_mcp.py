#!/usr/bin/env python3
"""Tool-level benchmark: V2 dev-intelligence tools vs a repository-search baseline.

What it measures (deterministic, no model involved):

* cold index build and warm freshness check (wall-clock, this machine);
* for each task in tests/mcp/benchmark/dev_intelligence_tasks.json:
  - V2: one ``diagnose_bug`` / ``plan_feature`` call at the default budget —
    files in ranked order, reply bytes, latency;
  - baseline: what an agent gets from the existing ``search_source`` tool when
    it greps every identifier/word of the same text (one call per term) and
    ranks files by distinct terms matched — files, total reply bytes, calls;
  - hit@k (any gold file in the top k) and recall@k (share of gold files).

What it does **not** measure: an agent/model solving the task end to end,
model tokens, or task success. Those need a pinned agent and are reported as
"not measured".

Usage::

    python scripts/benchmark_dev_mcp.py [--json out.json] [--index-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sshpilot.mcp._scope import RepoScope  # noqa: E402
from sshpilot.mcp.dev import search  # noqa: E402
from sshpilot.mcp.dev.engine import DevIntelligence  # noqa: E402

TASKS = ROOT / "tests" / "mcp" / "benchmark" / "dev_intelligence_tasks.json"
_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_QUOTED_RE = re.compile(r"\"([^\"\n]{12,160})\"")
_STOP = frozenset(
    """after again all also and are because before being between both but can cannot could does doing down each
    else even every from have here into itself just like many more most much must never once only other over same
    seem seems should since some such than that their them then there these they this those through very what when
    where which while will with within would your sure feels bit off""".split()
)


def baseline_terms(text: str):
    """Terms an agent would plausibly grep for: quoted phrases, then words."""
    terms = list(dict.fromkeys(_QUOTED_RE.findall(text)))
    for word in _TERM_RE.findall(text):
        if word.lower() in _STOP or word in terms:
            continue
        terms.append(word)
    return terms


def baseline(scope: RepoScope, text: str) -> dict:
    started = time.perf_counter()
    score: dict = {}
    total_bytes = 0
    calls = 0
    for term in baseline_terms(text):
        result = search.search_files(scope, re.escape(term), path_prefix="src", ignore_case=True, max_results=100)
        calls += 1
        total_bytes += len(json.dumps(result).encode())
        for match in result["matches"]:
            entry = score.setdefault(match["file"], [set(), 0])
            entry[0].add(term)
            entry[1] += 1
    ranked = sorted(score, key=lambda path: (-len(score[path][0]), -score[path][1], path))
    return {"files": ranked, "bytes": total_bytes, "calls": calls, "seconds": time.perf_counter() - started}


def v2(service: DevIntelligence, task: dict) -> dict:
    started = time.perf_counter()
    if task["tool"] == "plan_feature":
        result = service.plan_feature(task["symptoms"])
        paths = []
        for obligation in result["obligations"]:
            paths.extend(obligation.get("paths", []))
        paths.extend(pattern["path"] for pattern in result["patterns"])
        confidence = result["api_involvement"]
    else:
        result = service.diagnose_bug(task["symptoms"])
        paths = [candidate["path"] for candidate in result["candidates"]]
        confidence = result["confidence"]
    files = list(dict.fromkeys(paths))
    return {
        "files": files,
        "bytes": len(json.dumps(result).encode()),
        "calls": 1,
        "seconds": time.perf_counter() - started,
        "confidence": confidence,
    }


def score(files, gold, k):
    if not gold:
        return None, None
    top = files[:k]
    hits = [g for g in gold if any(f == g or (g.endswith("/") and f.startswith(g)) for f in top)]
    return bool(hits), len(hits) / len(gold)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", help="write raw results here")
    parser.add_argument("--index-dir", help="index directory (default: a fresh temporary one, i.e. a cold build)")
    args = parser.parse_args(argv)

    tasks = json.loads(TASKS.read_text())["tasks"]
    scope = RepoScope(ROOT)
    index_dir = Path(args.index_dir) if args.index_dir else Path(tempfile.mkdtemp(prefix="sshpilot-bench-index-"))
    service = DevIntelligence(scope, index_dir=index_dir)
    try:
        cold = service.index.refresh()
        warm_runs = [service.index.refresh()["duration_ms"] for _ in range(5)]
        rows = []
        for task in tasks:
            new = v2(service, task)
            old = baseline(scope, task["symptoms"])
            row = {"id": task["id"], "set": task.get("set", "design"), "category": task["category"], "tool": task["tool"], "gold": task["gold_files"]}
            for label, data in (("v2", new), ("baseline", old)):
                for k in (3, 5):
                    hit, recall = score(data["files"], task["gold_files"], k)
                    row[f"{label}_hit@{k}"] = hit
                    row[f"{label}_recall@{k}"] = recall
                row[f"{label}_bytes"] = data["bytes"]
                row[f"{label}_calls"] = data["calls"]
                row[f"{label}_seconds"] = round(data["seconds"], 3)
                row[f"{label}_top5"] = data["files"][:5]
            row["v2_confidence"] = new["confidence"]
            if not task["gold_files"]:
                row["misleading_confidence"] = new["confidence"] in ("high", "medium")
            rows.append(row)
        report = {
            "commit": _head(),
            "index": {
                "files": cold["scanned"],
                "cold_build_ms": cold["duration_ms"],
                "warm_check_ms_median": statistics.median(warm_runs),
                "index_bytes": service.index.path.stat().st_size,
            },
            "tasks": rows,
            "summary": {
                "all": _summary(rows),
                "design": _summary([r for r in rows if r["set"] == "design"]),
                "held_out": _summary([r for r in rows if r["set"] == "held-out"]),
            },
            "not_measured": ["end-to-end agent task success", "model tokens", "agent wall-clock time"],
        }
    finally:
        service.close()
        if not args.index_dir:
            shutil.rmtree(index_dir, ignore_errors=True)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    _print(report)
    return 0


def _summary(rows):
    graded = [row for row in rows if row["gold"]]
    out = {"graded_tasks": len(graded)}
    if not rows:
        return out
    for label in ("v2", "baseline"):
        for metric in ("hit@3", "hit@5"):
            out[f"{label}_{metric}"] = f"{sum(bool(r[f'{label}_{metric}']) for r in graded)}/{len(graded)}"
        out[f"{label}_mean_recall@5"] = round(statistics.mean(r[f"{label}_recall@5"] for r in graded), 2) if graded else None
        out[f"{label}_median_bytes"] = statistics.median(r[f"{label}_bytes"] for r in rows)
        out[f"{label}_total_calls"] = sum(r[f"{label}_calls"] for r in rows)
    out["misleading_confidence_events"] = sum(1 for r in rows if r.get("misleading_confidence"))
    return out


def _head():
    head = (ROOT / ".git" / "HEAD").read_text().strip() if (ROOT / ".git" / "HEAD").is_file() else ""
    if head.startswith("ref: "):
        ref = ROOT / ".git" / head[5:]
        return ref.read_text().strip()[:12] if ref.is_file() else head
    return head[:12]


def _print(report):
    index = report["index"]
    print(f"commit {report['commit']}  files {index['files']}  cold build {index['cold_build_ms'] / 1000:.1f}s  "
          f"warm check {index['warm_check_ms_median']:.0f}ms  index {index['index_bytes'] / 1e6:.1f}MB")
    print(f"{'set':9}{'task':38} {'v2 hit@5':>8} {'base hit@5':>10} {'v2 bytes':>9} {'base bytes':>10} {'base calls':>10} {'v2 conf':>8}")
    for row in report["tasks"]:
        print(
            f"{row['set']:9}{row['id']:38} {row['v2_hit@5']!s:>8} {row['baseline_hit@5']!s:>10} {row['v2_bytes']:>9} "
            f"{row['baseline_bytes']:>10} {row['baseline_calls']:>10} {row['v2_confidence']:>8}"
        )
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    sys.exit(main())
