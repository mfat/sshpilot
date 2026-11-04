"""``verify_change``: static/contract checks plus optional focused tests.

Every check reports its own ``state`` — ``passed``, ``failed``, ``skipped``,
``not_run`` or ``unknown`` — with ``executed`` telling whether anything ran.
Three separate answers, never one blurred "verified":

* ``checks_passed`` — every check that executed passed and at least one
  selected test ran and passed;
* ``coverage_confirmed`` — always ``False``: no coverage is collected, so
  nothing here proves the changed lines were exercised (a test that calls a
  changed function may still miss the changed branch);
* ``verification_gaps`` — every reason the run falls short: skipped tests
  (even one, alongside passes), changed symbols without a test that calls
  them, recommended suites not run, timeouts, incomplete impact analysis.

``overall`` is ``checks-passed`` only when ``checks_passed`` and there are no
gaps; otherwise ``failed``, ``incomplete`` or (static mode) ``static-only``.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from .. import api_surface, architecture, execution
from .diagnosis import redact
from .impact import inspect_impact
from .graph import Graph
from .models import EngineError, envelope, fit_to_budget, normalize_budget

MODES = ("static", "focused")


def _subprocess_state(result: dict) -> str:
    if result.get("timed_out"):
        return "unknown"
    return "passed" if result.get("success") else "failed"


def _excerpt(result: dict, limit: int = 600) -> str:
    text = (result.get("stdout") or "")[-limit:] + "\n" + (result.get("stderr") or "")[-limit:]
    return redact(text.strip(), limit=limit)


def _tests_check(result: dict) -> dict:
    outcomes = result.get("outcomes")
    check = {
        "name": "focused_tests",
        "kind": "tests",
        "executed": True,
        "command": result.get("command"),
        "exit_code": result.get("returncode"),
        "duration_seconds": result.get("duration_seconds"),
        "timed_out": result.get("timed_out", False),
    }
    if result.get("timed_out"):
        check["state"] = "unknown"
        check["reason"] = "timed out before pytest finished"
        check["output_excerpt"] = _excerpt(result)
        return check
    if outcomes is None:
        # No report: collection error, crash, or pytest missing.
        check["state"] = "failed" if result.get("returncode") not in (0, 5) else "not_run"
        check["reason"] = "pytest produced no per-test report"
        check["output_excerpt"] = _excerpt(result)
        return check
    counts = outcomes["counts"]
    check["counts"] = counts
    if outcomes["failures"]:
        check["failures"] = [
            {**failure, "message": redact(failure["message"], limit=300)} for failure in outcomes["failures"]
        ]
    if outcomes["skipped"]:
        check["skipped_tests"] = outcomes["skipped"]
    if counts["failed"] or counts["errors"]:
        check["state"] = "failed"
    elif result.get("returncode") not in (0,):
        check["state"] = "failed" if result.get("returncode") != 5 else "not_run"
        check["output_excerpt"] = _excerpt(result)
    elif counts["passed"] == 0 and counts["skipped"]:
        check["state"] = "skipped"
    elif counts["passed"] == 0:
        check["state"] = "not_run"
    else:
        check["state"] = "passed"
    return check


def verify_change(
    graph: Graph,
    freshness_fn: Callable[[], dict],
    base: str = "HEAD",
    mode: str = "static",
    suites: Optional[Sequence[str]] = None,
    max_test_files: int = 8,
    budget: Optional[int] = None,
    test_timeout: float = execution.EXECUTION_TIMEOUT_SECONDS,
) -> dict:
    budget = normalize_budget(budget)
    if mode not in MODES:
        raise EngineError(f"mode must be one of {', '.join(MODES)}")
    if not isinstance(max_test_files, int) or not 1 <= max_test_files <= execution.MAX_FOCUSED_FILES:
        raise EngineError(f"max_test_files must be between 1 and {execution.MAX_FOCUSED_FILES}")
    requested_suites = list(dict.fromkeys(suites or []))
    for suite in requested_suites:
        if suite not in execution.ALLOWED_TEST_SUITES:
            raise EngineError(f"not an allowed suite: {suite!r}")
    if requested_suites and mode != "focused":
        raise EngineError("suites run only in mode='focused'")

    impact = inspect_impact(graph, freshness_fn, base=base, depth=1, budget=64 * 1024)
    scope = graph.index.scope
    changed_paths = sorted(
        {path for paths in impact["layers"].values() for path in paths}
    )
    checks: List[dict] = []
    gaps: List[str] = []

    # -- static: syntax of changed Python files (from the index) --------------
    broken = [entry for entry in graph.index.parse_errors(changed_paths) if entry["status"] == "syntax_error"]
    checks.append(
        {
            "name": "python_syntax",
            "kind": "static",
            "executed": True,
            "state": "failed" if broken else "passed",
            **({"failures": broken[:10]} if broken else {}),
        }
    )

    # -- static: frontend neutrality ------------------------------------------
    neutrality = architecture.check_frontend_neutrality(scope)
    checks.append(
        {
            "name": "frontend_neutrality",
            "kind": "static",
            "executed": True,
            "state": {"valid": "passed", "invalid": "failed"}.get(neutrality["status"], "unknown"),
            **({"violations": neutrality["violations"][:10]} if neutrality["violations"] else {}),
            **({"unscanned": neutrality["unscanned"][:10]} if neutrality["unscanned"] else {}),
        }
    )

    # -- contract: API drift + generated artifacts when relevant --------------
    api_relevant = bool(impact.get("wire_surfaces")) or any(
        risk["kind"] in ("api-registration-source", "generated-api-schema", "generated-api-artifact", "wire-protocol-surface")
        for risk in impact["risks"]
    ) or any(path.startswith(("src/sshpilot/api/", "docs/api/generated/")) for path in changed_paths)
    if api_relevant:
        try:
            drift = api_surface.check_drift(scope)
            checks.append(
                {
                    "name": "api_drift",
                    "kind": "static",
                    "executed": True,
                    "state": "passed" if drift["clean"] else "failed",
                    **({"categories": drift["categories"]} if not drift["clean"] else {}),
                }
            )
        except (ValueError, OSError) as error:
            checks.append({"name": "api_drift", "kind": "static", "executed": True, "state": "unknown", "reason": str(error)})
        try:
            artifacts = execution.check_api_artifacts(scope)
            checks.append(
                {
                    "name": "api_artifacts",
                    "kind": "subprocess",
                    "executed": True,
                    "state": _subprocess_state(artifacts),
                    "command": artifacts["command"],
                    "exit_code": artifacts["returncode"],
                    "duration_seconds": artifacts.get("duration_seconds"),
                    **({"output_excerpt": _excerpt(artifacts)} if not artifacts["success"] else {}),
                }
            )
        except execution.ExecutionError as error:
            checks.append({"name": "api_artifacts", "kind": "subprocess", "executed": False, "state": "unknown", "reason": str(error)})
    else:
        for name in ("api_drift", "api_artifacts"):
            checks.append({"name": name, "kind": "static", "executed": False, "state": "not_run", "reason": "no API, wire-protocol or generated-artifact change"})

    # -- tests -------------------------------------------------------------------
    plan = impact["tests"]
    selected = plan["focused_files"][:max_test_files]
    selection = {
        "selected_files": selected,
        "evidence": [
            {k: entry[k] for k in ("test", "evidence", "target")} for entry in plan["direct"][:10]
        ],
        "suites_fallback": plan["suites_fallback"],
        "broad_run_recommended": plan["broad_run_recommended"],
        "unmapped_changed_symbols": plan["unmapped_changed_symbols"][:10],
    }
    if mode == "static":
        checks.append(
            {
                "name": "focused_tests",
                "kind": "tests",
                "executed": False,
                "state": "not_run",
                "reason": "mode='static' never runs tests; use mode='focused'",
            }
        )
        gaps.append("tests were not run (static mode)")
    else:
        if selected:
            try:
                result = execution.run_test_files(scope, selected, timeout=test_timeout)
                tests_check = _tests_check(result)
                checks.append(tests_check)
                skipped = tests_check.get("counts", {}).get("skipped", 0)
                if skipped and tests_check["state"] == "passed":
                    # Passing tests next to skipped ones: the skipped ones may be
                    # exactly the ones that exercise the change.
                    gaps.append(f"{skipped} selected test(s) were skipped; the run is not a full pass")
            except execution.ExecutionError as error:
                checks.append({"name": "focused_tests", "kind": "tests", "executed": False, "state": "unknown", "reason": str(error)})
        else:
            checks.append(
                {
                    "name": "focused_tests",
                    "kind": "tests",
                    "executed": False,
                    "state": "not_run",
                    "reason": "no test file has static evidence for the changed code",
                }
            )
            gaps.append("no focused tests were selected")
        for suite in requested_suites:
            try:
                result = execution.run_tests(scope, suite)
                checks.append(
                    {
                        "name": f"suite:{suite}",
                        "kind": "tests",
                        "executed": True,
                        "state": _subprocess_state(result),
                        "command": result["command"],
                        "exit_code": result["returncode"],
                        "duration_seconds": result.get("duration_seconds"),
                        "timed_out": result["timed_out"],
                        **({"output_excerpt": _excerpt(result)} if not result["success"] else {}),
                    }
                )
            except execution.ExecutionError as error:
                checks.append({"name": f"suite:{suite}", "kind": "tests", "executed": False, "state": "unknown", "reason": str(error)})
        if plan["unmapped_changed_symbols"]:
            gaps.append(
                f"{len(plan['unmapped_changed_symbols'])} changed symbol(s) have no direct test evidence "
                "(no test calls or references them; an import alone does not count)"
            )
        missing_suites = [s for s in plan["suites_fallback"] if s not in requested_suites]
        if plan["broad_run_recommended"] and missing_suites:
            gaps.append(
                "impact analysis recommends the broader suite(s) "
                + ", ".join(missing_suites)
                + " (unmapped or wire-protocol changes); pass suites=[...] to run them"
            )

    if not impact["analysis_complete"]:
        gaps.append("impact analysis incomplete: " + "; ".join(impact["warnings"][:3]))

    states = [check["state"] for check in checks]
    if "failed" in states:
        overall = "failed"
    elif mode == "static":
        overall = "static-only" if all(s in ("passed", "not_run") for s in states) else "incomplete"
    else:
        tests_ran = any(c["kind"] == "tests" and c["state"] == "passed" for c in checks)
        if tests_ran and not gaps and set(states) <= {"passed", "not_run"}:
            overall = "checks-passed"
        else:
            overall = "incomplete"
            if not tests_ran:
                gaps.append("no test run passed; the change is not verified")
            if "unknown" in states:
                gaps.append("at least one check finished in an unknown state (timeout or missing tool)")
            if "skipped" in states:
                gaps.append("selected tests were all skipped")

    result = envelope(
        "verify_change",
        freshness_fn(),
        status="ok",
        input={"base": base, "mode": mode, "suites": requested_suites},
        overall=overall,
        checks_passed=(
            mode == "focused"
            and "failed" not in states
            and "unknown" not in states
            and any(c["kind"] == "tests" and c["state"] == "passed" for c in checks)
        ),
        coverage_confirmed=False,
        coverage_note="No coverage is collected: passing selected tests do not prove the changed lines ran.",
        verification_gaps=list(dict.fromkeys(gaps)),
        checks=checks,
        test_selection=selection,
        changed_paths=changed_paths[:40],
    )
    return fit_to_budget(
        result,
        budget,
        # Checks are never trimmed: a dropped failure would read as a pass.
        # Neither are verification gaps: a dropped gap would overstate the result.
        ["changed_paths", "test_selection.evidence", "test_selection.unmapped_changed_symbols"],
        min_keep=3,
    )
