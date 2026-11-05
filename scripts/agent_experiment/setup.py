"""Create one isolated workspace per (task, arm). Usage: setup.py"""
import json, os, shutil, stat, subprocess
from common import *

TASKS = {  # fix commit -> frozen v2 task id
    "ccc26c9a": "auth-saved-passphrase-retry",
    "0ec819c6": "daemon-results-behind-redraw",
    "edf664e8": "sftp-short-read-slow",
    "4b3b8781": "sidebar-deferred-rows-failure",
    "cc04b976": "closed-transport-reported-on-quit",
    "3a01b1af": "stale-windows-daemon-endpoint",
}
dataset = {t["id"]: t for t in json.loads((REPO / "tests/mcp/benchmark/dev_intelligence_tasks.json").read_text())["tasks"]}
runs = EXP / "runs"
tool = EXP / "devintel-src"  # current engine only: sshpilot/__init__.py + sshpilot/mcp
if not tool.exists():
    (tool / "sshpilot").mkdir(parents=True)
    shutil.copy(REPO / "src/sshpilot/__init__.py", tool / "sshpilot/__init__.py")
    shutil.copytree(REPO / "src/sshpilot/mcp", tool / "sshpilot/mcp", ignore=shutil.ignore_patterns("__pycache__", "runtime"))
    (tool / "sshpilot/mcp/runtime").mkdir()
    (tool / "sshpilot/mcp/runtime/__init__.py").write_text("")

def write_exec(path, text):
    path.write_text(text); path.chmod(path.stat().st_mode | stat.S_IXUSR)

manifest = {}
for fix, task_id in TASKS.items():
    for arm in ("plain", "devintel"):
        ws = runs / f"{task_id}--{arm}"
        if ws.exists():
            shutil.rmtree(ws)
        repo = snapshot(f"{fix}^", ws / "repo")
        pristine = snapshot(f"{fix}^", EXP / "pristine" / task_id) if not (EXP / "pristine" / task_id).exists() else None
        home = ws / ".sandbox-home"
        env_lines = "\n".join(f'export {k}="{v}"' for k, v in safe_env(home, repo).items() if k != "PATH")
        write_exec(ws / "run_tests.sh", f"""#!/bin/bash
# Runs pytest for this workspace with an isolated HOME/XDG/SSH dir (never the real user's).
cd "{repo}"
{env_lines}
export PATH="{os.environ['PATH']}"
exec xvfb-run -a "{PY}" -m pytest -n0 -p no:cacheprovider -p no:hypothesispytest "$@"
""")
        if arm == "devintel":
            write_exec(ws / "devintel", f"""#!/bin/bash
# SSH Pilot dev-intelligence CLI for this workspace (JSON output). See: devintel --help
exec env PYTHONPATH="{tool}" SSHPILOT_MCP_INDEX_WORKERS=8 "{PY}" -m sshpilot.mcp.dev.cli --root "{repo}" --index-dir "{ws / '.devintel-index'}" "$@"
""")
            # Pre-build the index (both arms start without waiting; cold build excluded from timings).
            subprocess.run([str(ws / "devintel"), "--compact", "index-status"], check=True, capture_output=True)
        manifest[f"{task_id}--{arm}"] = {"fix": fix, "task": task_id, "arm": arm, "ws": str(ws), "symptoms": dataset[task_id]["symptoms"]}
(EXP / "manifest.json").write_text(json.dumps(manifest, indent=1))
print(len(manifest), "workspaces")
