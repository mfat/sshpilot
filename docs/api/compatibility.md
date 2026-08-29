# Compatibility and versioning

SSH Pilot uses a deliberately simple policy:

## Frontend compatibility imports

The obsolete stateful `sshpilot.connection_manager.ConnectionManager` backend
is retired. `sshpilot.connection_manager` remains for one documented v1
compatibility window as a minimal import shim exporting only the ephemeral
`Connection` and `ConnectionState` models. It performs no persistence, SSH
configuration, known-hosts, secret, or process I/O. New code must use the typed
`SshPilotClient` API for saved connections and protected operations. The shim
is scheduled for removal at the next incompatible application/plugin API
window, with a deprecation release note before then.

## Phase 11: Daemon lifecycle and management

Phase 11 adds daemon lifecycle states, idle shutdown, management RPCs, client
reconnect policy, and packaging for `sshpilot-daemon`. Older clients ignore the
new capabilities, methods, and `daemon.state_changed` events. Newer clients
require `daemon.status` before reading diagnostics and `daemon.control` before
stop/restart. Reconnect helpers never restore live sessions or transfers across
restart. `API_IMPLEMENTATION_VERSION` is `0.11`.

## Phase 10: Additive SFTP / transfer / forward surface

Phase 10 is an additive Protocol v1 extension. Older clients ignore the new
capabilities, methods, events, and error codes. Newer clients require the
narrow `sftp.*`, `transfers.*`, and `forwards.*` capabilities before calling
the matching operations. Coarse legacy `sftp` and `port_forwarding`
identifiers remain in the schema and are never advertised.
`API_IMPLEMENTATION_VERSION` is `0.10`.

## Phase 9 / 9.1: Default Behavior Change

Phase 9 introduced daemon-backed internal SSH routing. Phase 9.1 hardened
routing so readiness never silently selects a frontend SSH process:

- **Default change**: `terminal.daemon_backed_ssh` now defaults to `True` (Stage C rollout)
- **Internal route**: SSH terminals use the daemon; external terminals remain a
  separate presentation choice
- **Route vs readiness**: `resolve_ssh_terminal_route` is pure policy;
  `resolve_daemon_terminal_readiness` never changes the selected route
- **No silent fallback**: Missing daemon, bridge, protocol, binary transport, or
  capabilities shows a clear error and does not launch local internal SSH

- Backward-compatible additive evolution remains Protocol v1.
- Incompatible public semantic changes require Protocol v2.
- Capabilities discover optional feature groups.
- `API_IMPLEMENTATION_VERSION` tracks the Python implementation separately.
- Every public surface or semantic change is recorded in
  [CHANGELOG.md](CHANGELOG.md).

The current `PROTOCOL_VERSION` string is `1.0`. The daemon selects exact
supported version `1.0` during handshake and rejects unsupported versions.
Application versions are not compatibility signals. A later minor-negotiation
policy must be documented and tested before changing this rule.

`API_IMPLEMENTATION_VERSION` is currently `0.50`. Version 0.50 replaces native
SCP transfer failures and public-key deployment operation failures with strict
`ScpFailure` and `IdentityFailure` values. A 0.49 peer expects the generic
`{code, message}` object in those fields, while a 0.50 peer requires the
matching discriminated object, so mismatched implementations are rejected
during handshake before ordinary requests. The generic `ServiceFailure`
contract remains unchanged for authorized-key removal, forwards, broadcast,
and every other non-selected consumer. Version 0.49 introduced strict
`SftpFailure`; the 0.48 plugin editor, 0.47 backup/import, 0.46 secret-status,
and 0.45 secret-prompt contracts remain unchanged.

The earlier 0.40 compatibility boundary remains in force: clients never
downgrade to plaintext secret transport or select a frontend secret backend.
A daemon with live resources is not killed implicitly; the existing explicit
restart policy remains responsible for that decision.

## Non-breaking changes within v1

Subject to review, these can remain Protocol v1:

- Add an optional response field with a safe default or absence-safe meaning.
- Add an event old clients may ignore.
- Add a capability and the operations guarded by it.
- Add an error code for a condition previously represented by a generic error.
- Add a method that old clients never call.
- Add optional enum context only after codecs and clients demonstrably handle
  unknown values safely.
- Tighten documentation without changing tested behaviour.

The Host-alias connection-ID model remains Protocol v1 compatible because
`ConnectionId` was documented and modeled as opaque. Existing wire fields and
method shapes are unchanged. New responses always contain the stable form;
deprecated nickname-hash IDs remain accepted as lookup aliases during the
bounded v1 window. A capability is unnecessary because correct clients do not
branch on ID syntax.

The daemon session-lifecycle foundation is also an additive Protocol v1
extension. Older clients do not advertise or call the new methods and may
ignore the new event types; newer clients require `sessions.read`,
`sessions.write`, and `sessions.events` before using them. `SessionId` is
daemon-lifetime opaque identity and resets across daemon restart. No terminal
byte, PTY, prompt, replay, or persistent-session compatibility is implied.

API implementation 0.6 deliberately replaces the former schema-only session
state vocabulary and removes caller-supplied client IDs from open/attach
requests. This is a Python source-contract change for consumers that adopted
the speculative models before runtime support. It is not a Protocol v1 wire
break because no session wire method previously existed. Handshaken transport
identity is now authoritative; accepting the old fields would create an
impersonation-prone contract.

Additive does not automatically mean safe. For example, adding an event can
break clients that treat unknown events as fatal; that behaviour must be fixed
and contract-tested before relying on additive compatibility.

## Potentially breaking changes

These require explicit compatibility review and normally Protocol v2:

- Rename or remove a field, method, event, error, capability, or enum value.
- Change a field type or required/optional status.
- Change ID generation, stability, scope, or alias semantics.
- Change event ordering, delivery, replay, coalescing, or loss guarantees.
- Change an error code's meaning or safe-details shape incompatibly.
- Remove an advertised capability.
- Change valid state transitions or terminal-state meaning.
- Change terminal `bytes` into text or assume an encoding.
- Expose new secret-bearing data or weaken redaction.
- Change method side effects, cancellation races, thread ownership, or
  synchronous calling convention.
- Map legacy terminal state into connection health without defining the new
  semantics.

## Application versus protocol versions

The SSH Pilot application version changes for product releases. It does not
prove API compatibility. `API_IMPLEMENTATION_VERSION` identifies revisions of
the Python implementation and may change without a protocol-major change.
`PROTOCOL_VERSION` changes only for the compatibility policy above.

## Capability negotiation

Feature additions should normally introduce or extend a meaningful capability.
Providers advertise a capability only when its runtime operations and contract
tests pass. A schema alone is not support. Clients:

1. call `get_capabilities()`;
2. verify `compatibility.compatible`;
3. enable optional features only when the required capability is present;
4. still handle `unsupported_capability`.

## Version combinations

- **Old frontend, new compatible core:** unknown optional capabilities/events/
  fields must be safely ignored according to the eventual codec rules. Existing
  behaviour remains stable.
- **New frontend, old core:** the frontend checks capabilities and hides or
  degrades unavailable functionality. Required missing features fail with
  `unsupported_capability`.
- **Incompatible protocol versions:** the daemon handshake rejects the pairing
  before ordinary commands with `protocol_version_unsupported`.

- **Incompatible implementation revisions:** the daemon handshake rejects a
  mismatched `API_IMPLEMENTATION_VERSION` with `api_version_mismatch`; the UI
  must offer restart/recovery and retain user input where possible.

## `DaemonClient` compatibility

The reusable connection contract suite exercises the daemon transport and
direct core service composition separately. It compares:

- connection reads and writes, capabilities, mutation/not-found errors, and DTO values;
- secret exclusion and Host-alias connection identity;
- unsupported schema-only method errors;
- close/disconnect behavior.

Daemon-only handshake, framing, correlation, timeout, socket security,
lifecycle, event multiplexing, bounded event/byte backpressure, mutation
ambiguity, and multi-client sequence rules have focused transport tests.
Connection events are daemon-owned and covered under `connections.events`.
Typed authentication/trust interactions are daemon-only and capability-gated in API
0.9; unrestricted prompt parity remains out of scope. Terminal byte/replay
contracts are daemon-only and capability-gated in API 0.8. Session lifecycle is
daemon-only; daemon integration contracts cover lifecycle,
attachment, multi-client event, shutdown, and process-ownership semantics.
API 0.8 adds optional terminal DTO fields and a negotiated binary frame while
retaining Protocol v1 control compatibility. Old clients do not advertise
`binary-terminal-v2`, never receive terminal frames, and can continue
connection/session control. API 0.7 changed daemon execution ownership without
changing Protocol v1 request/response shapes: open/close remain synchronous
`DaemonClient` calls while the daemon
completes them from a bounded worker path. The open response is deliberately
the `starting` acceptance snapshot. Clients were already required to reconcile
later state through events or session reads, so Protocol remains `1.0`.
API 0.9 adds typed interaction DTOs, narrow capabilities, JSON metadata
methods/events, and the separately negotiated `binary-secret-v2` one-use
response frame. Old clients do not claim that frame type, are never selected as
responders, receive no secret frame, and retain all connection/session/
terminal behaviour. Protocol remains `1.0` because the extension is additive
and capability/frame negotiated.
The [public API snapshot](../../tests/api/snapshots/public_api.json) is a review
aid for structural changes, not proof of semantic compatibility.

## Review rule

When the snapshot changes:

1. explain the compatibility impact;
2. update all affected reference pages;
3. update [CHANGELOG.md](CHANGELOG.md);
4. add or update contract tests;
5. deliberately regenerate artifacts with
   `python3 scripts/generate_api_artifacts.py`;
6. increment Protocol v1 only if this policy has first been revised, or create
   Protocol v2 for an incompatible contract.

The checked-in `tests/api/snapshots/versions/0.39.json` is historical and must
not be regenerated in place. Current changes are recorded in the 0.40 snapshot.

Daemon-owned external reload does not change Protocol v1. It uses the existing
connection DTOs, opaque IDs, connection event names, and
`connections.read`/`connections.events` capabilities. Existing clients already
reconcile correctly by refreshing their snapshot after an event.
