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
  - `tests/mcp/test_runtime_stdio_roundtrip.py`: full stdio round-trip —
    launches ``python -m sshpilot.mcp.runtime`` as a subprocess with
    ``SSHPILOT_MCP_SOCKET``/policy opt-ins against an ephemeral daemon, and
    drives ``ClientSession`` over stdio the way a real MCP client would
    (integration-marked; deselected by the default ``pytest`` filter).
  - `tests/mcp/test_runtime_operate_stdio.py`: OPERATE/MUTATE round-trips
    over real OpenSSH — Phase 13 stack (ephemeral daemon + Alpine sshd) with
    ``python -m sshpilot.mcp.runtime`` as a subprocess; opens and closes a
    session and an SFTP service, and exercises the MUTATE confirmation flow
    (mkdir refused without ``confirm``, created + listed + removed with
    ``confirm=True``) through the MCP stdio path using password
    authentication with an auto-answering helper (no secret crosses the MCP
    conversation); also drives the READ surface (stat, read_file, and
    list_directory) and the remaining MUTATE tools (create_file, rename,
    chmod, symlink, remove) — each refused without ``confirm=True``.
  - `tests/mcp/test_runtime_hostkey_stdio.py`: host-key TOFU dogfood — mounts
    a connection config with ``StrictHostKeyChecking ask`` and an empty
    ``UserKnownHostsFile`` so the Alpine sshd key is genuinely unknown, then
    drives the real ``HOST_KEY_CONFIRMATION`` interaction over the runtime MCP
    stdio path. MCP observes (``list_interactions``/``get_interaction``),
    claims, and releases the interaction (claim nonce stays ``<redacted>``);
    the trusted frontend attaches to the session and accepts, so the session
     reaches RUNNING and MCP closes it. Proves the interaction is attributable
     to the session and decided outside the model (D003).
   - `tests/mcp/test_runtime_fido_stdio.py`: FIDO/sk-dummy dogfood — generates
     a real `ed25519-sk` key with OpenSSH's installed `sk-dummy.so`, installs its
     public key in the Alpine sshd fixture, configures `SecurityKeyProvider`,
     and opens/closes a session through runtime MCP stdio. Proves the daemon
     reaches RUNNING through real security-key authentication without hardware;
     the dummy provider completes presence internally, so no
     `security_key_presence` askpass interaction is emitted.
  - Capability-driven tool visibility: `create_server` queries the daemon
    capabilities up front and removes tools whose required daemon capability
    is missing (`TOOL_CLIENT_METHOD` maps tools to client methods;
    `UNSUPPORTED_CLIENT_METHOD_CAPABILITIES` is authoritative), so the MCP
    surface reflects what the connected daemon can actually do. Capability is
    still rechecked at call time by `DaemonClient`, and drift between the
    tool map and the real client surface is caught by headless tests.
  - Secret handling: `sshpilot.mcp.runtime.jsonable` honors the daemon's
    `field(repr=False)` marker, so DTO content (remote file reads, SSH config
    text, interaction claim nonces, plugin result values) is emitted as
    `<redacted>` by default and only restored with the explicit
    `SSHPILOT_MCP_CONTENT=1` opt-in. This is the runtime security review's
    headline item (D010).
- **Architecture classification**: `src/sshpilot/mcp/**` is an internal
  (non-GTK, non-service) layer; the MCP boundary is guarded by
  `tests/architecture/test_mcp_boundary.py` (GTK/service-free imports, only
  `dev/_git.py` and `dev/execution.py` may use subprocess, read-only git
  whitelist is authoritative, controlled-execution allowlists authoritative,
  typed tools only).
- **Tests**: `tests/mcp/` and `tests/architecture/test_mcp_boundary.py` pass
  (the stdio/integration smoke tests need the optional `mcp` SDK installed in
  the venv and integration tests are deselected by default); `tests/api`,
  `tests/core`, ruff, and `generate_api_artifacts --check` are green.

## What is being worked on

- Nothing in progress; steps 1-5, the stdio round-trip, capability-driven tool
  visibility, and the interaction dogfood are complete.

## What is next

1. Runtime security review follow-up complete: audited the runtime MCP result
   surface. `OperationSummary.result` was the free-form smuggling path because
   broadcast output can be flattened into its dict after losing nested
   `repr=False` markers; it is now daemon-declared `repr=False`, documented as
   sensitive, and redacted wholesale unless content opt-in is enabled. Typed
   DTO fields remain covered by the same marker.
2. Integration/dogfooding follow-up: real-provider FIDO presence scenarios.
   The sk-dummy authentication dogfood is complete
   (`test_runtime_fido_stdio.py`); a physical-key/provider round-trip is still
   needed to exercise the interactive `security_key_presence` notification.
   The daemon's own tests deliberately avoid requiring physical FIDO hardware.

## Important issues for the next agent

- No changes were made to the wire protocol or capabilities. Generated API
  artifacts were regenerated to document the new sensitive result field; the
  wire shape is unchanged.
- Confirmation UX decision (D009): MUTATE uses a per-tool ``confirm=True``
  argument — settled, do not reopen without updating `docs/mcp/decisions.md`.
- Secret handling (D010): DTO fields marked ``field(repr=False)`` are redacted
  to ``<redacted>`` in MCP results unless ``SSHPILOT_MCP_CONTENT=1`` is set;
  opaque operation result payloads use the same marker — settled, do not
  reopen without updating `docs/mcp/decisions.md`.
- The MCP boundary test allows `mcp` modules to import only stdlib, `mcp`,
  `sshpilot.mcp`, and `sshpilot.api`. The runtime server consumes
  `DaemonClient` via `sshpilot.api` — do not widen the allowlist for `core`/
  `daemon` imports.
- The optional `mcp` SDK (`pip install 'sshpilot[mcp]'` / `mcp>=2.0.0`) is
  installed in the local `.venv`; `tests/mcp/test_dev_server_smoke.py` and the
  runtime smoke tests run here with it. The integration-marked tests
  (`test_runtime_stdio_roundtrip.py`, `test_runtime_operate_stdio.py`) need
  `-m integration` explicitly.
