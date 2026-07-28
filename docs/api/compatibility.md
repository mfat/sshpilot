# Compatibility and versioning

SSH Pilot uses a deliberately simple policy:

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

The UUID-backed connection-ID migration remains Protocol v1 compatible because
`ConnectionId` was documented and modeled as opaque. Existing wire fields and
method shapes are unchanged. New responses always contain the stable form;
deprecated nickname-hash IDs remain accepted as lookup aliases during the
bounded v1 window. A capability is unnecessary because correct clients do not
branch on ID syntax.

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

## `DaemonClient` compatibility

The reusable connection contract suite runs against both `DaemonClient` and
`InProcessClient`. It compares:

- connection reads and writes, capabilities, mutation/not-found errors, and DTO values;
- secret exclusion, stable UUID identity, and deprecated transitional lookup;
- unsupported schema-only method errors;
- close/disconnect behavior.

Daemon-only handshake, framing, correlation, timeout, socket security,
lifecycle, event multiplexing, bounded event/byte backpressure, mutation
ambiguity, and multi-client sequence rules have focused transport tests.
Connection event parity is covered under `connections.events`. Terminal-byte,
replay, prompt, and cancellation parity
remain out of scope because the daemon does not advertise those capabilities.
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
