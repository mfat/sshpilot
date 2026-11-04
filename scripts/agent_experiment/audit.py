"""Parse subagent transcripts (JSONL) into metrics without printing their content."""
import json, re
from datetime import datetime
from pathlib import Path
import os
TASKS = Path(os.environ["SSHPILOT_AGENT_TRANSCRIPTS"])  # directory of <agent-id>.output JSONL transcripts
DEVINTEL_CMD = re.compile(r"/devintel\"?\s+(?:--?[\w-]+(?:\s+\S+)?\s+)*(diagnose-bug|get-context|inspect-impact|plan-feature|index-status|verify-change|--help|-h)\b")
FORBIDDEN = [re.compile(p) for p in (re.escape(os.environ.get("SSHPILOT_REPO_PATH", "/nonexistent-repo-path")), r"(^|[\s;&|(])git\s", r"\.git/", r"/exp/(manifest|validation|grade|prompts|pristine|control|common|setup|agents|audit)")]

def audit(agent_id):
    path = TASKS / f"{agent_id}.output"
    if not path.exists():
        return None
    usage = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    seen_msg = set(); tools = {}; devintel = 0; violations = []; times = []; calls = 0
    for line in path.read_text(errors="replace").splitlines():
        try: rec = json.loads(line)
        except ValueError: continue
        ts = rec.get("timestamp")
        if ts: times.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
        msg = rec.get("message") or {}
        if rec.get("type") == "assistant" and isinstance(msg, dict):
            mid = msg.get("id")
            u = msg.get("usage")
            if u and mid not in seen_msg:  # one usage per API response (content blocks repeat it)
                seen_msg.add(mid); calls += 1
                usage["input"] += u.get("input_tokens", 0); usage["output"] += u.get("output_tokens", 0)
                usage["cache_write"] += u.get("cache_creation_input_tokens", 0); usage["cache_read"] += u.get("cache_read_input_tokens", 0)
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "?"); tools[name] = tools.get(name, 0) + 1
                    blob = json.dumps(block.get("input", {}))
                    devintel += len(DEVINTEL_CMD.findall(blob))
                    if name.startswith("mcp__") or name in ("WebFetch", "WebSearch"):
                        violations.append(name)
                    for pat in FORBIDDEN:
                        if pat.search(blob): violations.append(f"{name}:{pat.pattern}")
    return {"api_calls": calls, "tokens": usage, "total_tokens": sum(usage.values()),
            "tool_calls": sum(tools.values()), "tools": tools, "devintel_calls": devintel,
            "elapsed_s": round((max(times) - min(times)).total_seconds()) if times else None,
            "violations": sorted(set(violations))}

if __name__ == "__main__":
    os.chdir(os.environ["SSHPILOT_EXPERIMENT_DIR"])
    agents = json.loads(Path("agents.json").read_text())
    res = {k: audit(a) for k, a in agents.items()}
    json.dump(res, open("audit.json", "w"), indent=1)  # in the experiment dir
    for k, r in res.items():
        print(k, "missing" if r is None else {x: r[x] for x in ("api_calls", "total_tokens", "tool_calls", "devintel_calls", "elapsed_s", "violations")})
