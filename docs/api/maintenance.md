# API maintenance

Public models and tested client behaviour are the source of truth. Documentation
and generated artifacts are mandatory review surfaces, not alternate
implementations.

## Required workflow

1. Decide whether the responsibility belongs to core, frontend, or transport.
2. Update deliberate public DTOs; never expose persistence, GTK/GObject, PTY,
   subprocess, provider, or raw secret objects.
3. Update capability definitions when clients need optional feature discovery.
4. Update `SshPilotClient` methods and their synchronous/threading semantics.
5. Add or update event types, typed payloads, ordering, and delivery rules.
6. Add structured error codes and safe details where needed.
7. Implement core behaviour while preserving the single native SSH/auth path.
8. Update `InProcessClient`.
9. Update both `InProcessClient` and `DaemonClient`, or keep the operation
   schema-only and return `unsupported_capability` consistently.
10. Add reusable contract tests, not implementation-only assertions.
11. Update state-transition documentation and transition tests.
12. Update the method/model/event/error/capability references.
13. Update [CHANGELOG.md](CHANGELOG.md).
14. Review compatibility and protocol-version impact.
15. Regenerate and review the structural artifacts.

## API change checklist

- [ ] Core/frontend/transport ownership is defined
- [ ] Public DTOs are updated
- [ ] No internal persistence models are exposed
- [ ] Sensitive fields and examples are reviewed
- [ ] Capability definitions are updated
- [ ] Unsupported clients fail predictably
- [ ] Structured errors are updated
- [ ] Events are added or updated
- [ ] Ordering, delivery, cancellation, timeout, and threading are defined
- [ ] State transitions are documented
- [ ] `InProcessClient` is updated
- [ ] `DaemonClient` is updated or a backlog item is recorded
- [ ] Contract tests are updated
- [ ] API documentation is updated
- [ ] API changelog is updated
- [ ] Compatibility impact is reviewed
- [ ] Protocol-version impact is reviewed
- [ ] Generated artifacts were deliberately refreshed

## Commands

After reviewing the public change:

```bash
python3 scripts/generate_api_artifacts.py
python3 scripts/generate_api_artifacts.py --check
pytest -q tests/api/test_api_documentation.py
pytest -q tests/api/test_public_api_snapshot.py
pytest -q tests/daemon
pytest -q tests/daemon/test_event_forwarding.py tests/daemon/test_event_backpressure.py
pytest -q tests/api/test_client_factory.py tests/daemon/test_launcher.py
pytest -q tests/test_gtk_client_bridge.py
ruff check src/ tests/ scripts/generate_api_artifacts.py
pytest
```

For real-GTK daemon responsiveness coverage:

```bash
SSHPILOT_GUI_TESTS=1 DISPLAY=:1 pytest -q -m gui tests/test_gui_smoke.py
```

Daemon mode is a process-local development selection, not configuration state:

```bash
SSHPILOT_CLIENT_MODE=daemon python3 run.py
```

The generator writes:

- `docs/api/generated/schema.json`
- `docs/api/generated/model-index.md`
- `tests/api/snapshots/public_api.json`

It derives only from frontend-neutral API modules. The JSON catalog is not
OpenAPI and does not imply HTTP. Synthetic examples never read live objects or
stored data and replace sensitive fields with an omission marker. The reviewed
snapshot records method parameter/return shapes as well as names, so changing a
schema-only operation signature also requires deliberate snapshot approval.
Transport envelopes are included as a separate convenience-export surface, and
the explicit daemon method/capability registry is included so drift cannot add
an undocumented callable wire method.

Connection event changes additionally require codec round trips, shared client
contracts, daemon-global sequence and multi-client assertions, response
interleaving, bounded overflow isolation, transport-failure wakeups, and GTK
coalescing/responsiveness coverage. Do not infer replay from a sequence field.

Connection mutation changes additionally require the same create/update/delete
contract against both clients, strict request/result codecs, exactly one event
on success and none on failure, ambiguity/no-auto-retry tests, secret-exclusion
checks, and total outbound-byte backpressure coverage.

Connection identity changes additionally require config reload tests,
alias-preserving update tests, alias rename tests proving delete-plus-create
semantics, group/reference updates, duplicate-alias handling, and GTK
snapshot reconciliation. Tests must use isolated temporary configuration
roots and never a developer's real SSH configuration.

Session lifecycle changes additionally require the complete transition matrix,
strict session-ID and request codecs, runner startup/failure/exit races,
bounded exact-process termination, attachment idempotency and disconnect
cleanup, closed-record retention, response/event interleaving, multi-client
event delivery, mutation-ambiguity behaviour, and daemon shutdown with no
owned process leaks. Terminal byte methods must remain unsupported until their
own transport and backpressure contract is implemented.

Potentially blocking session command changes additionally require a bounded
executor and completion queue, explicit immediate/deferred method policy,
per-session ordering, stable peer-token correlation, queue-full behaviour,
disconnect/late-completion tests, and repeated blocked start/close/shutdown
isolation runs. No runner start/terminate/kill/wait call may execute on the
selector thread.

Interaction changes additionally require strict typed metadata, complete state
transition/race coverage, responder eligibility and takeover tests, bounded
deadlines/retention, one-use nonce-bound secret transport tests, raw prompt and
secret exclusion from JSON/events/replay/logs, existing-backend integration,
remember-only-after-authentication-success checks, and daemon/GTK/helper
shutdown coverage. Never add secret values to ordinary method DTOs merely to
avoid implementing the dedicated response path.

## Example: connection health monitoring

Ownership and flow should be:

```text
core health monitor
    ↓
connection.health_changed
    ↓
SshPilotClient
    ↓
GTK / Tauri / CLI
```

Define health semantics and cadence first, add a typed event payload and
capability if monitoring is optional, implement it in the core, adapt both
clients, then migrate GTK. Do not derive health only inside a GTK row or reuse
terminal session state as host reachability.

## Example: SFTP search

1. Decide whether it extends `sftp` or justifies a coherent `sftp.search`
   capability; do not add a capability for a trivial parameter.
2. Add deliberate request/result models that omit provider and process objects.
3. Define not-found, permission, cancellation, and timeout errors.
4. Define progress/result events only if the operation is asynchronous, with
   ordering and loss policy.
5. Implement through the existing core OpenSSH SFTP path and shared SSH/auth
   resolver.
6. Add the operation to client implementations and reusable contract tests.
7. Document it, update state semantics if any, add a changelog entry, review
   compatibility, and regenerate artifacts.

## Review principles

- A capability is advertised only after real behaviour and contract tests.
- A protocol method is not “implemented” because a stub exists.
- New SSH operations extend existing builders/auth resolution; they do not
  assemble parallel commands or secret environments.
- Human messages are not machine contracts.
- Schema-only elements remain visibly labelled until runtime support ships.
- Snapshot approval is deliberate: regeneration never substitutes for
  compatibility review.

For persistence changes, run the external-reload regression suite. New
authoritative files must join the daemon watch set, be strictly validated
before commit, participate in last-known-good rollback, and have atomic
replacement plus self-write no-op coverage. GTK must not gain a daemon-mode
migration bypass.

## Phase 9 / 9.1 / 9.3 notes

Production GTK SSH terminals are daemon-backed (`terminal.daemon_backed_ssh`).
Phase 9.1 keeps route selection (`SshTerminalRoute`) separate from daemon readiness;
readiness failures must never start a frontend SSH process.
When regenerating API artifacts after claim/release or multi-attachment changes, run
`python3 scripts/generate_api_artifacts.py` and keep `docs/api/methods.md` /
`docs/api/errors.md` markers in sync with `SshPilotClient` and `DAEMON_METHOD_CAPABILITIES`.

### Phase 9.3 testing notes

- GUI tests (`SSHPILOT_GUI_TESTS=1`) isolate `HOME`/`XDG_*` **and**
  `XDG_RUNTIME_DIR`, and force `SSHPILOT_CLIENT_MODE=in_process`. They must
  never attach to the developer user socket under `/run/user/$UID/sshpilot/`.
- Daemon-specific GUI cases start an owned `DaemonServer` on a unique temp
  socket, assert `server_instance_id` / `threads_alive()`, and tear down
  bridge → client → server (with `wait_stopped`) before removing temps.
- After updating daemon code, restart `sshpilotd` before manual testing.
  Compare handshake `daemon_version` / `api_implementation_version` /
  optional `SSHPILOT_DEV_REVISION` via logs or `DaemonClient.build_mismatch()`.
- Transport timeouts emit structured safe diagnostics; do not widen the default
  five-second control RPC timeout to paper over stalls.
- PTY autofill is local/legacy only. Daemon SSH uses interaction dialogs and
  must not feed a local VTE child.
