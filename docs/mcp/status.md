# MCP Status

Current handoff for SSH Pilot MCP work. Git is the historical record; stale
entries are rewritten, not accumulated.

## What exists now

- The SSH Pilot application is mature: `sshpilotd` daemon, frontend-neutral
  typed API (`src/sshpilot/api/**`, `DaemonClient`), generated API artifacts
  (`docs/api/generated/schema.json`), and a large test suite.
- **MCP foundation (step 1) complete**: `RepoScope` (root discovery +
  read-only path confinement), `sshpilot-dev-mcp` stdio server backed by the
  official `mcp` SDK, root launcher + console script, `mcp` optional extra.
- **Dev MCP repository intelligence (step 2) complete** — the server now
  exposes typed tools:
  - `repo_info`, `read_text_file`, `list_directory`
  - `search_source` (regex, path-prefix scoping, cap, skips binary/artifacts)
  - `find_symbol` (AST-based class/function/method lookup, partial/exact/kind)
  - `git_status`, `git_log`, `git_diff` (strict read-only whitelist
    `status/log/diff/rev-parse`, argv-only, no shell, 512 KiB cap)
  - `find_tests` (pytest `test_*.py` / `*_test.py` discovery)
  - `inspect_api`, `list_api_methods`, `trace_api_method`,
    `check_api_drift` (API intelligence from
    `docs/api/generated/schema.json` plus AST-scanned client signatures,
    wire-method mapping, daemon dispatch handlers, and drift detection)
  - Implementation: `src/sshpilot/mcp/_scope.py`,
    `dev/{search,symbols,test_discovery,_git,api_surface,server}.py`.
- **Architecture classification**: `src/sshpilot/mcp/**` is an internal
  (non-GTK, non-service) layer; the MCP boundary is guarded by
  `tests/architecture/test_mcp_boundary.py` (GTK/service-free imports, only
  `dev/_git.py` may use subprocess, read-only git whitelist is authoritative,
  typed tools only).
- **Tests**: `tests/mcp/` (42 files-level + real stdio smoke) and
  `tests/architecture/test_mcp_boundary.py` all pass; `tests/architecture`,
  `tests/api`, `tests/core`, ruff, and `generate_api_artifacts --check` are
  green.

## What is being worked on

- Step 3 (API intelligence) is complete; architecture intelligence is the
  next slice and nothing is in progress yet.

## What is next

1. Architecture intelligence — frontend-neutrality checks, public API review,
   interaction-scope tracing, commit/regression review helpers.
2. Controlled local execution — selected pytest, API artifact validation,
   lint; never arbitrary shell.
3. Runtime MCP prototype — `DaemonClient` only, capability discovery, READ
   tools first.
4. Runtime OPERATE support — explicit opt-in, sessions, SFTP, operation and
   interaction observation.
5. Runtime MUTATE support — explicit opt-in, strong policy boundary, human
   confirmation where appropriate.
6. Integration/dogfooding — real bug reproduction, OpenSSH fixtures,
   FIDO/sk-dummy scenarios.

## Important issues for the next agent

- No changes were made to the wire protocol, capabilities, or API; no
  generated artifacts were modified.
- The `runtime` subpackage (`src/sshpilot/mcp/runtime/`) does not exist yet;
  create it at step 4 above.
- The MCP boundary test allows `mcp` modules to import only stdlib, `mcp`,
  `sshpilot.mcp`, and `sshpilot.api`. The runtime server must consume
  `DaemonClient` via `sshpilot.api` — do not widen the allowlist for `core`/
  `daemon` imports.
