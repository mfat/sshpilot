"""Run an adapted, mechanism-neutral test against a tree: adapted.py TASK TREE..."""
import shutil, sys, tempfile
from pathlib import Path
from common import *
TESTS = {"closed-transport-reported-on-quit": "adapted_transport.py", "daemon-results-behind-redraw": "adapted_redraw.py", "stale-windows-daemon-endpoint": "adapted_endpoint.py"}
task = sys.argv[1]
for tree_arg in sys.argv[2:]:
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "t"
        shutil.copytree(tree_arg, tree, ignore=shutil.ignore_patterns("__pycache__"))
        dest = tree / "tests" / ("daemon" if "endpoint" in task else "") / ("test_exp_" + TESTS[task])
        shutil.copy(HERE / "adapted" / TESTS[task], dest)
        r = run_tests(tree, [str(dest.relative_to(tree))])
    states = {k.split("::")[-1]: v[0] for k, v in r["cases"].items()}
    print(f"{tree_arg.split('/exp/')[-1]:70} {states or r['tail'][-300:]}")
