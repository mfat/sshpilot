#!/usr/bin/env python3
"""Tool-level benchmark: V2 dev-intelligence tools vs repository-search baselines.

By default every historical task runs against the checkout *immediately
before its fixing commit* (``git archive <fix>^``, extracted to a temporary
directory, indexed from scratch), so the fix's own code and names cannot leak
into the answer. ``--checkout head`` reproduces the earlier, leaky run on the
current tree for comparison.

Per task (deterministic, no model involved):

* ``v2``          one ``diagnose_bug`` / ``plan_feature`` call, default budget;
* ``search_all``  the original dev MCP ``search_source`` tool run for every
                  term of the same text (one call per term) — a mechanical
                  upper bound on search output;
* ``selective``   an emulation of an efficient searcher: probe every term for
                  matching *file names only* (like ``rg -l -i``), keep the three
                  rarest terms that hit 1–30 files, read their match lines, rank
                  files by distinct kept terms, then occurrences;
* hit@k (any gold file in the top k) and recall@k (share of gold files),
  reply bytes and calls.

Also: cold index build per snapshot and warm freshness check.

Not measured: an agent/model solving the task end to end, model tokens, task
success. Those need a pinned agent and are reported as "not measured".

Usage::

    python scripts/benchmark_dev_mcp.py [--checkout prefix|head] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import io
import shutil
import statistics
import subprocess
import sys
import tarfile
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


def selective(scope: RepoScope, text: str, corpus: dict, keep: int = 3, max_files: int = 30) -> dict:
    started = time.perf_counter()
    total_bytes = 0
    calls = 0
    probes = {}
    for term in baseline_terms(text):
        needle = term.lower()
        files = sorted(path for path, body in corpus.items() if needle in body)
        probes[term] = files
        calls += 1
        total_bytes += len("\n".join(files).encode()) + 1  # what `rg -l -i` prints
    chosen = sorted((t for t, f in probes.items() if 1 <= len(f) <= max_files), key=lambda t: (len(probes[t]), t))[:keep]
    score_map: dict = {}
    for term in chosen:
        result = search.search_files(scope, re.escape(term), path_prefix="src", ignore_case=True, max_results=100)
        calls += 1
        total_bytes += len(json.dumps(result).encode())
        for match in result["matches"]:
            entry = score_map.setdefault(match["file"], [set(), 0])
            entry[0].add(term)
            entry[1] += 1
    ranked = sorted(score_map, key=lambda path: (-len(score_map[path][0]), -score_map[path][1], path))
    return {"files": ranked, "bytes": total_bytes, "calls": calls, "seconds": time.perf_counter() - started, "terms": chosen}


def corpus_for(scope: RepoScope) -> dict:
    return {str(rel): body.lower() for rel, body in scope.iter_text_within(prefix="src")}


def score(files, gold, k):
    if not gold:
        return None, None
    top = files[:k]
    hits = [g for g in gold if any(f == g or (g.endswith("/") and f.startswith(g)) for f in top)]
    return bool(hits), len(hits) / len(gold)


LABELS = ("v2", "search_all", "selective")


def snapshot(commit: str, into: Path) -> Path:
    """Extract the tree of *commit* (read-only: ``git archive``)."""
    data = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", commit],
        capture_output=True,
        check=True,
    ).stdout
    target = into / commit.replace("^", "-parent")
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(target, filter="data")
    return target


def run_task(service, scope, corpus, task) -> dict:
    results = {
        "v2": v2(service, task),
        "search_all": baseline(scope, task["symptoms"]),
        "selective": selective(scope, task["symptoms"], corpus),
    }
    row = {"id": task["id"], "set": task.get("set", "design"), "category": task["category"], "tool": task["tool"], "gold": task["gold_files"]}
    for label, data in results.items():
        for k in (3, 5):
            hit, recall = score(data["files"], task["gold_files"], k)
            row[f"{label}_hit@{k}"] = hit
            row[f"{label}_recall@{k}"] = recall
        row[f"{label}_bytes"] = data["bytes"]
        row[f"{label}_calls"] = data["calls"]
        row[f"{label}_seconds"] = round(data["seconds"], 3)
        row[f"{label}_top5"] = data["files"][:5]
    row["selective_terms"] = results["selective"]["terms"]
    row["v2_confidence"] = results["v2"]["confidence"]
    if not task["gold_files"]:
        row["misleading_confidence"] = results["v2"]["confidence"] in ("high", "medium")
    return row


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--checkout", choices=("prefix", "head"), default="prefix",
                        help="prefix: each task on the commit before its fix (default); head: current tree")
    parser.add_argument("--json", help="write raw results here")
    parser.add_argument("--task", action="append", dest="only", help="run only these task ids")
    args = parser.parse_args(argv)

    tasks = json.loads(TASKS.read_text())["tasks"]
    if args.only:
        tasks = [task for task in tasks if task["id"] in args.only]
    work = Path(tempfile.mkdtemp(prefix="sshpilot-bench-"))
    groups: dict = {}
    for task in tasks:
        commit = f"{task['fix_commit']}^" if args.checkout == "prefix" and task.get("fix_commit") else "HEAD"
        groups.setdefault(commit, []).append(task)
    rows, builds = [], []
    try:
        for commit, group in groups.items():
            root = ROOT if commit == "HEAD" and args.checkout == "head" else snapshot(commit, work / "trees")
            scope = RepoScope(root)
            service = DevIntelligence(scope, index_dir=work / "index" / commit.replace("^", "-parent"))
            try:
                cold = service.index.refresh()
                warm = statistics.median(service.index.refresh()["duration_ms"] for _ in range(3))
                builds.append({"checkout": commit, "files": cold["scanned"], "cold_build_ms": cold["duration_ms"],
                               "warm_check_ms": warm, "index_bytes": service.index.path.stat().st_size})
                corpus = corpus_for(scope)
                for task in group:
                    row = run_task(service, scope, corpus, task)
                    row["checkout"] = commit
                    rows.append(row)
                    print(f"  done {task['id']} on {commit}", file=sys.stderr)
            finally:
                service.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    order = {task["id"]: n for n, task in enumerate(tasks)}
    rows.sort(key=lambda row: order[row["id"]])
    report = {
        "commit": _head(),
        "checkout_mode": args.checkout,
        "index_builds": builds,
        "tasks": rows,
        "summary": {
            "all": _summary(rows),
            "design": _summary([r for r in rows if r["set"] == "design"]),
            "held_out": _summary([r for r in rows if r["set"] == "held-out"]),
        },
        "not_measured": ["end-to-end agent task success", "model tokens", "agent wall-clock time"],
    }
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    _print(report)
    return 0


def _summary(rows):
    graded = [row for row in rows if row["gold"]]
    out = {"graded_tasks": len(graded)}
    if not rows:
        return out
    for label in LABELS:
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
    print(f"commit {report['commit']}  checkout mode: {report['checkout_mode']}")
    for build in report["index_builds"]:
        print(f"  index {build['checkout']}: {build['files']} files, cold {build['cold_build_ms'] / 1000:.1f}s, "
              f"warm {build['warm_check_ms']:.0f}ms, {build['index_bytes'] / 1e6:.1f}MB")
    head = f"{'set':9}{'task':38}" + "".join(f"{label + ' @5':>15}{'bytes':>9}" for label in LABELS) + f"{'v2 conf':>9}"
    print(head)
    for row in report["tasks"]:
        cells = "".join(f"{row[f'{label}_hit@5']!s:>15}{row[f'{label}_bytes']:>9}" for label in LABELS)
        print(f"{row['set']:9}{row['id']:38}{cells}{row['v2_confidence']:>9}")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    sys.exit(main())
