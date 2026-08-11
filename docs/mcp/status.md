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
  - `check_frontend_neutrality`, `review_public_api`,
    `trace_interaction_scope`, `review_commit` (architecture/regression
    intelligence)
  - `run_pytest`, `run_lint`, `validate_api_artifacts` (controlled local
    execution; argv-only allowlists, never arbitrary shell)
  - Implementation: `src/sshpilot/mcp/_scope.py`,
    `dev/{search,symbols,test_discovery,_git,api_surface,architecture,execution,server}.py`.
- **Runtime MCP prototype (step 3-5) complete** —
  `src/sshpilot/mcp/runtime/` (`policy.py`, `jsonable.py`, `server.py`,
  `__init__.py`, `__main__.py`):
  - Consumes `DaemonClient` via `sshpilot.api` only; never calls daemon
    internals, never spawns SSH binaries, never reads secrets into the model's
    context.
  - READ / OPERATE / MUTATE authorization model from environment opt-ins
    (`SSHPILOT_MCP_READ`/`_OPERATE`/`_MUTATE`); MUTATE disabled by default
    and every MUTATE tool additionally requires `confirm=True`.
  - Typed tools only: 19 READ (capabilities, status, connections, sessions,
    SFTP metadata, transfers, forwards, operations, interactions), OPERATE
    (open/close session, open/attach/detach/close SFTP, cancel operation,
    claim/release/cancel interaction), MUTATE (SFTP create/mkdir/rmdir/remove/
    rename/chmod/symlink, cancel transfer) — all routed through
    `RuntimeHandle` → `DaemonClient`.
  - `tests/mcp/test_runtime_policy.py`: headless policy/handle tests that
    run in the minimal environment (no ``mcp`` SDK).
  - `tests/mcp/test_runtime_server_smoke.py`: in-process protocol smoke test
    (handshake, typed-tool enumeration, READ round-trip, MUTATE
    confirmation/policy refusals) driven over the official SDK's memory
    streams.
  - `tests/mcp/test_runtime_daemon_integration.py`: real-daemon integration
    — boots an ephemeral ``DaemonServer`` (headless core), connects a real
    ``DaemonClient``, and drives the runtime MCP server over the SDK streams
    through the full ``MCP -> DaemonClient -> daemon -> core`` path
    (capabilities, daemon status, connection listing, and a direct-client
    sanity check).
- **Architecture classification**: `src/sshpilot/mcp/**` is an internal
  (non-GTK, non-service) layer; the MCP boundary is guarded by
  `tests/architecture/test_mcp_boundary.py` (GTK/service-free imports, only
  `dev/_git.py` and `dev/execution.py` may use subprocess, read-only git
  whitelist is authoritative, controlled-execution allowlists authoritative,
  typed tools only).
- **Tests**: `tests/mcp/` and `tests/architecture/test_mcp_boundary.py` pass
  (the stdio smoke test needs the optional `mcp` SDK); `tests/api`,
  `tests/core`, ruff, and `generate_api_artifacts --check` are green.

## What is being worked on

- Nothing in progress; steps 1-5 are complete.

## What is next

1. Runtime integration already exercised over the SDK memory streams; next
   is a full stdio round-trip: boot ``sshpilot-runtime-mcp`` as a subprocess
   (console script / ``python -m``) against a live daemon and invoke tools
   through ``ClientSession`` the way a real MCP client would. Also verify
   OPERATE round-trips (open/close session, SFTP service lifecycle) against
   the headless daemon or real OpenSSH fixtures.
2. Runtime MCP review — decide the exact MUTATE tool set and confirmation UX,
   and whether `confirm` should stay a tool argument or move to a separate
   authorization step.
3. Runtime security review — secret handling on the path from daemon result
   to model context (base64 file reads, SSH config text), and capability-driven
   tool visibility.
4. Integration/dogfooding with real bug reproduction, OpenSSH fixtures,
   FIDO/sk-dummy scenarios.

## Important issues for the next agent

- No changes were made to the wire protocol, capabilities, or API; no
  generated artifacts were modified.
- The MCP boundary test allows `mcp` modules to import only stdlib, `mcp`,
  `sshpilot.mcp`, and `sshpilot.api`. The runtime server consumes
  `DaemonClient` via `sshpilot.api` — do not widen the allowlist for `core`/
  `daemon` imports.
- The optional `mcp` SDK is not installed in the current environment, so
  `tests/mcp/test_dev_server_smoke.py` cannot be collected here. Install
  `pip install 'sshpilot[mcp]'` before running the stdio smoke tests.
