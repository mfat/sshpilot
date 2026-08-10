# MCP Architecture Decisions

Durable architectural decisions for SSH Pilot MCP support. Do not reopen these
without updating this file.

## D001 — Dev MCP and runtime MCP stay separate

Status: Accepted

Decision: `sshpilot-dev-mcp` (repository intelligence) and `sshpilot-runtime-mcp`
(daemon/API integration) are two separate servers with different security
boundaries. They are never merged into one omnipotent server.

Reason: repository inspection and runtime SSH control have incompatible trust
profiles. A contributor-safe server must remain safe for anyone to load; the
runtime server is explicitly opt-in.

## D002 — Runtime MCP consumes DaemonClient

Status: Accepted

Decision: The runtime MCP server talks to `sshpilotd` only through
`DaemonClient` / `SshPilotClient`. It never calls daemon internals directly,
never spawns parallel `ssh`/`sftp`/`ssh-copy-id` implementations, and never
bypasses daemon limits, lifecycle, capabilities, or interaction handling.

Reason: the daemon owns authoritative state, processes, PTYs, secrets, keys,
and interactions. Missing daemon functionality is an API gap to be fixed in the
API, not bypassed from MCP.

## D003 — Secret exposure policy

Status: Accepted

Decision: MCP does not mechanically expose every `SshPilotClient` method.
Secrets (passwords, passphrases, raw secret frames, private key contents,
password-manager credentials, plugin secrets) are not exposed by default.
Authentication flows follow the model: MCP starts the operation, the daemon
reports an authentication interaction, a trusted human-facing frontend handles
password/PIN/passphrase, and MCP only observes operation/interactions state.

Reason: raw secrets are never placed in the model's context unless a policy
decision explicitly allows it. Capability is not authorization.

## D004 — Schemas are derived mechanically

Status: Accepted

Decision: MCP tool schemas derive from the authoritative API surface
(`SshPilotClient`, request/result DTOs, generated API artifacts). No second
hand-written API is maintained. Where manual mapping is unavoidable, tests
detect drift between the API and the MCP surface.

Reason: one authoritative SSH Pilot API, least duplication, and drift detection
over hand-maintained schemas.

## D005 — Runtime authorization model

Status: Accepted

Decision: Runtime MCP uses three conceptual permission levels — READ, OPERATE,
MUTATE. READ may be enabled broadly; OPERATE requires explicit opt-in; MUTATE is
disabled by default unless explicitly allowed. MUTATE operations include remote
file writes/deletes, authorized-key changes, connection edits, forward
modification, and side-effectful transfers. Daemon capabilities decide whether
an operation is technically possible; MCP policy decides whether an AI model may
invoke it.

Reason: capability is not authorization; least privilege for model-driven
operations.

## D006 — Package layout and dependency strategy

Status: Accepted

Decision: MCP servers live under `src/sshpilot/mcp/` as separate subpackages
(`dev`, later `runtime`), stay GTK/GI-free, and ship as setuptools console
scripts (`sshpilot-mcp-dev`). The official `mcp` SDK (which requires
Python 3.10+) is an optional extra (`pip install 'sshpilot[mcp]'`); the core
application keeps its Python 3.9 floor. The repo `_scope` module stays pure
stdlib so path-safety tests run without the SDK.

Reason: mirrors the existing `core`/`api`/`daemon` subpackage layout, keeps the
base install lean, and treats MCP support as opt-in.

## D007 — MCP is an internal layer, governed by its own boundary guard

Status: Accepted

Decision: `src/sshpilot/mcp/**` is an internal layer in the architecture
boundary scans: it is neither a GTK frontend nor a daemon-managed SSH service,
so it is exempted from the GTK frontend closure scans (added to
`_INTERNAL`/`INTERNAL` in `tests/architecture`). In exchange, a dedicated
`tests/architecture/test_mcp_boundary.py` enforces the MCP invariants: modules
import only stdlib, `mcp`, `sshpilot.mcp`, and `sshpilot.api` (GTK/service-free);
only `dev/_git.py` may use `subprocess`; the Git whitelist is exactly
`status/log/diff/rev-parse` (no mutation, no shell); tools are typed.

Reason: keeping the MCP servers out of the GTK-frontend scans would be a hole
unless the specific MCP rules are guarded elsewhere. This classifies the new
layer and preserves least-privilege enforcement with a stricter, narrower test.
