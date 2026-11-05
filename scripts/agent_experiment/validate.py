import json, shutil, sys, tempfile
from pathlib import Path
from common import *

out = {}
for fix in sys.argv[1:]:
    work = Path(tempfile.mkdtemp(prefix=f"val-{fix}-"))
    pre = snapshot(f"{fix}^", work / "pre")
    post = snapshot(fix, work / "post")
    tests = fix_test_files(fix)
    for t in tests:  # hidden tests = fix-commit versions, dropped into the pre-fix tree
        (pre / t).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(post / t, pre / t)
    before = run_tests(pre, tests)
    after = run_tests(post, tests)
    fail_before = {k: v for k, v in before["cases"].items() if v[0] in ("failed", "error")}
    ok_after = {k for k, v in after["cases"].items() if v[0] == "passed"}
    out[fix] = {
        "tests": tests,
        "before_rc": before["rc"], "after_rc": after["rc"],
        "n_before": len(before["cases"]), "n_after": len(after["cases"]),
        "fail_before": {k: v[1] for k, v in fail_before.items()},
        "fail_before_that_pass_after": sorted(set(fail_before) & ok_after),
        "after_not_passing": {k: v for k, v in after["cases"].items() if v[0] != "passed"},
        "before_tail": before["tail"][-600:] if not before["cases"] else "",
    }
    shutil.rmtree(work, ignore_errors=True)
    print(fix, len(out[fix]["fail_before_that_pass_after"]), "discriminating", file=sys.stderr)
json.dump(out, open(EXP / "validation.json", "w"), indent=1)
