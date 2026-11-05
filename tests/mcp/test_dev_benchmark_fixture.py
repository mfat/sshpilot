"""The benchmark answer keys stay valid as the code moves."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "tests" / "mcp" / "benchmark" / "dev_intelligence_tasks.json"


def _harness():
    spec = importlib.util.spec_from_file_location("benchmark_dev_mcp", ROOT / "scripts" / "benchmark_dev_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tasks_are_well_formed_and_gold_files_exist():
    data = json.loads(TASKS.read_text())
    assert data["format"] == "sshpilot-dev-intel-benchmark/1"
    ids = [task["id"] for task in data["tasks"]]
    assert len(ids) == len(set(ids)) >= 6
    categories = {task["category"] for task in data["tasks"]}
    assert {"daemon-bug", "typed-api-method-addition", "gtk-controller-to-daemon", "ssh-authentication-lifecycle", "config-state", "negative-insufficient-evidence"} <= categories
    for task in data["tasks"]:
        assert task["tool"] in ("diagnose_bug", "plan_feature")
        assert task["set"] in ("design", "held-out")
        for path in task["gold_files"]:
            assert (ROOT / path).exists(), f"{task['id']}: gold file moved: {path}"


def test_harness_scoring():
    harness = _harness()
    assert harness.score(["a.py", "b.py"], ["b.py"], 1) == (False, 0.0)
    assert harness.score(["a.py", "b.py"], ["b.py", "c.py"], 5) == (True, 0.5)
    assert harness.score(["src/x/models/y.py"], ["src/x/models/"], 5) == (True, 1.0)
    assert harness.score([], [], 5) == (None, None)
    assert harness.baseline_terms('It says "could not be checked" when quitting')[0] == "could not be checked"


def test_dataset_is_frozen_and_pre_fix_commits_exist():
    data = json.loads(TASKS.read_text())
    assert data["version"] == 2 and data["frozen"]
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    for task in data["tasks"]:
        if not task["fix_commit"]:
            continue
        probe = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"{task['fix_commit']}^{{commit}}"],
            capture_output=True,
        )
        if probe.returncode != 0:
            pytest.skip("shallow clone: historical commits unavailable")
        parent = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"{task['fix_commit']}^"], capture_output=True
        )
        assert parent.returncode == 0, task["id"]
