"""record.py KEY TOKENS TOOLS MS -> grade the run and store the harness-reported usage."""
import json, os, sys, subprocess
from pathlib import Path
os.chdir(os.environ["SSHPILOT_EXPERIMENT_DIR"]); (Path("grades")).mkdir(exist_ok=True)
GRADE = str(Path(__file__).resolve().parent / "grade.py")
key, tokens, tools, ms = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
n = json.loads(Path("notified.json").read_text()) if Path("notified.json").exists() else {}
n[key] = {"subagent_tokens": tokens, "tool_uses": tools, "duration_ms": ms}
Path("notified.json").write_text(json.dumps(n, indent=1))
out = subprocess.run([sys.executable, GRADE, key], capture_output=True, text=True, check=True).stdout
Path(f"grades/{key}.json").write_text(out)
g = json.loads(out)[0]
print(key, "resolved" if g["resolved"] else "NOT resolved", g["discriminating_passed"], "regressions:", g["regressions_in_hidden_files"], "files:", g["changed_files"])
