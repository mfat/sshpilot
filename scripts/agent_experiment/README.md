# End-to-end agent experiment

This experiment asks one question: does giving a coding agent the dev-intelligence engine change whether it fixes real SSH Pilot bugs, and at what cost? Results are in `docs/mcp/dev-intelligence.md` (section "End-to-end agent experiment") and `results-2026-11-05.json`.

## Protocol

1. **Pick tasks that can be graded fairly** (`validate.py FIX...`). For each fix commit, the test files that commit added are the hidden tests. A task qualifies only if:
   - those tests fail on `FIX^` by an assertion (not by an import or attribute error on a name the fix introduced);
   - they pass on `FIX`.
2. **Build the workspaces** (`setup.py`). There is one workspace per task and arm, each holding a `git archive` of `FIX^`:
   - there is no `.git`, so the agent cannot see history or future commits;
   - each workspace has a `run_tests.sh` that isolates HOME, XDG, SSH and the display (Xvfb);
   - the `devintel` arm also gets a `devintel` CLI wrapper, its index pre-built and run from a copy of only `sshpilot/mcp`, so the agent cannot reach today's fixed code.
3. **Run the agents.** Same model and same prompt. The prompt contains only the frozen v2 symptom text, `run_tests.sh` and the access rules; the `devintel` arm adds one paragraph describing the CLI. All runs start at the same time, so both arms share the machine load.
4. **Grade** (`record.py KEY TOKENS TOOLS MS` → `grade.py`):
   - copy the agent's tree;
   - overwrite or add the fix commit's test files;
   - run them isolated;
   - a run counts as resolved when every discriminating test passes and no other test in those files regresses.
   
   Positive control: grading `FIX` itself must resolve every task. Negative control: grading the untouched `FIX^` must resolve none.
5. **Mechanism-neutral re-grade** (`adapted.py TASK TREE...`). Some hidden tests turned out to check *how* the fix was built: a private constant name, exact log wording, or a test double that cannot call a new helper method. The `adapted/` tests keep every behavioural assertion and drop only that coupling. Each one fails on `FIX^` and passes on `FIX` before it is used, and every tree is judged by the same test.
6. **Audit** (`audit.py`). It parses each agent's JSONL transcript with code: per-request token usage, tool calls, `devintel` invocations, elapsed time, and forbidden access (the main checkout, `git`, MCP tools, the web, grading files).

Environment: `SSHPILOT_EXPERIMENT_DIR` (required, outside the checkout), `SSHPILOT_AGENT_TRANSCRIPTS` (for `audit.py`), `SSHPILOT_REPO_PATH` (the path the audit flags), and `SSHPILOT_EXPERIMENT_PYTHON` (optional).

## Known limits

- **CLI, not MCP.** The `devintel` arm uses the engine through its CLI. Subagents in this harness cannot be given an MCP server. The engine and the JSON output are the same as the MCP tools.
- **Access rules are instructed, not enforced.** Agents run without a sandbox; the transcript audit is the check.
- **Small sample.** One run per task and arm.
