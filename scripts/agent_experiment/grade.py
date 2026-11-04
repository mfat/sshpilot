"""Grade one workspace: apply the agent's tree, drop in the hidden tests, run them."""
import json, shutil, sys, tempfile, filecmp
from pathlib import Path
from common import *

def changed_files(pristine: Path, agent: Path):
    out = []
    for p in sorted(agent.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        rel = p.relative_to(agent)
        q = pristine / rel
        if not q.exists() or not filecmp.cmp(p, q, shallow=False):
            out.append(str(rel))
    for q in sorted(pristine.rglob("*")):
        if q.is_file() and not (agent / q.relative_to(pristine)).exists():
            out.append(str(q.relative_to(pristine)) + " (deleted)")
    return out

def grade(key):
    m = json.loads((EXP / "manifest.json").read_text())[key]
    v = json.loads((EXP / "validation.json").read_text())[m["fix"]]
    agent = Path(m["ws"]) / "repo"
    pristine = EXP / "pristine" / m["task"]
    changes = changed_files(pristine, agent)
    with tempfile.TemporaryDirectory(prefix="grade-") as tmp:
        tree = Path(tmp) / "tree"
        shutil.copytree(agent, tree, ignore=shutil.ignore_patterns("__pycache__"))
        hidden = fix_test_files(m["fix"])
        for t in hidden:
            data = git("show", f"{m['fix']}:{t}", binary=True)
            (tree / t).parent.mkdir(parents=True, exist_ok=True)
            (tree / t).write_bytes(data)
        result = run_tests(tree, hidden)
    expected = {k for k in result["cases"]} - set(v["after_not_passing"])
    disc = v["fail_before_that_pass_after"]
    passed = {k for k, s in result["cases"].items() if s[0] == "passed"}
    regress = sorted(k for k in expected if k not in disc and k not in passed)
    disc_pass = [k for k in disc if k in passed]
    return {
        "key": key, "changed_files": changes,
        "discriminating_passed": f"{len(disc_pass)}/{len(disc)}",
        "regressions_in_hidden_files": regress,
        "resolved": len(disc_pass) == len(disc) and not regress,
        "failures": {k: s[1][:160] for k, s in result["cases"].items() if s[0] in ("failed", "error")},
    }

if __name__ == "__main__":
    print(json.dumps([grade(k) for k in sys.argv[1:]], indent=1))
