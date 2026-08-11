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

## D008 — Tool visibility derived from daemon capabilities

Status: Accepted

Decision: `create_server` queries the daemon's supported capabilities up front
and removes tools whose required capability the daemon lacks. Tool-to-method
mapping (`TOOL_CLIENT_METHOD`) and the authoritative method-capability map
(`UNSUPPORTED_CLIENT_METHOD_CAPABILITIES`) drive the filter; capability is
still rechecked at call time by `DaemonClient`. If capabilities cannot be
queried, every tool stays visible and the call-time gate remains the guard.

Reason: the MCP surface should advertise only what the connected daemon can do.
Capability filtering complements (does not replace) the policy/`confirm` MUTATE
gate, since daemon capability is not authorization.

## D009 — MUTATE confirmation is a per-tool argument

Status: Accepted

Decision: MUTATE tools require `confirm=True` as a per-tool argument rather
than a separate authorization step. `RuntimeHandle._mutate` refuses any MUTATE
call without it; the `SSHPILOT_MCP_MUTATE` environment opt-in gates whether the
level is permitted at all, and `confirm=True` is the second gate.

Reason: MCP is model-driven with no direct human-facing channel on the
server side (requests arrive as tool calls, responses go back to the model).
A separate authorization step would be invisible to the standard MCP protocol,
so the explicit `confirm` argument is what forces a human-approved call to
surface in the model's own input. A future human-review layer can sit in front
of the MCP client without changing the server contract.

## D010 — Daemon-declared sensitive fields are redacted by default

Status: Accepted

Decision: The runtime MCP boundary honors the daemon's own "not for
unstructured output" marker (`field(repr=False)`) by default: those field
values (`SftpReadFileResult.content`, `SshConfigText.text`,
`InteractionClaim.nonce`, `OperationSummary.result`, plugin result values) are
emitted as `<redacted>` rather than their real value, so remote file content,
SSH config text, claim nonces, and opaque operation payloads never reach model
context implicitly. An explicit opt-in `SSHPILOT_MCP_CONTENT=1` restores the
live values.

Reason: MCP output goes to a model context, which is a stronger exposure than
the reference CLI's terminal output, and the daemon already declares which DTO
fields are unsuitable for unstructured output. Reusing that marker instead of
maintaining a second secret-field list keeps redaction mechanically derived
(D004). Free-form operation payloads are treated as opaque because they may
contain content that has lost a nested DTO's marker. Content is restored only
by an explicit, named operator decision.
