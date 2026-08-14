# SSH Pilot connection identity persistence design

Status: Phase 1 filesystem implementation complete. This document does not authorize repository/daemon UUID wiring, public API identity migration, secret migration, or UI work.

## 1. Scope and baseline

This design is based on \`bc2b3ab28e72f1fc3935283221750305d27d8125\`. The accepted reconciliation contract is:

1. exact alias;
2. trusted static destination;
3. unique explicit User plus comparable IdentityFile evidence;
4. unique explicit User;
5. unique 1:1 destination remainder; and
6. otherwise \`AMBIGUOUS\`.

Declaration order is diagnostic ordering only; it is never identity evidence. Ambiguous candidates are excluded from both \`created\` and \`deleted\`, and \`apply_reconciliation()\` rejects an unresolved result.

The v2 domain model remains isolated from the repository, daemon, API, GTK,
SSH subprocess, network, and secret store. Production filesystem entrypoints
are implemented in \`core/connections/state_file.py\`; the existing v1
entrypoints remain unchanged for current repository callers.

## 2. Current production architecture

\`\`\`text
~/.ssh/config and Includes
        |
        v
ssh_config_loader -> ConnectionRecord(id/nickname = Host alias)
        |                         |
        |                         +-- private static-evidence fields
        v
ConnectionRepository -> ConnectionService -> alias-keyed snapshots
        |
        +-- SshConfigStore (SSH writes, revisions, rollback)
        +-- connections.json v1 (groups, root order, safe metadata)
        +-- daemon/config_reload.py (watch/reload/diff)
                              |
                              v
                       typed API DTOs and current clients
\`\`\`

The current sidecar stores group membership, root order, and metadata by
alias. \`ConnectionRepository._publish_state_locked()\` reconstructs the
service from \`record.data\`; that launch/public mapping does not carry
loader-private identity evidence. The future UUID layer must receive loader
records or projections before this boundary, not recover evidence from API DTOs.

### Current mutation and reload paths

| Path | Current owner and writes | Current identity behavior | Future change |
|---|---|---|---|
| startup | repository loads SSH, then v1 sidecar/legacy state | aliases decorate records; stale refs are dropped | load/migrate v2, then reconcile persisted projections |
| daemon reload | watcher calls \`repository.reload()\` | external alias rename is delete/create; raw rename heuristic is not used | one durable reconciliation path |
| raw editor save | \`SshConfigStore.replace_text()\`, then repository state handling | broad raw-record signature may migrate unique alias refs | replace heuristic with UUID transaction/reconcile |
| typed create/update/delete | store writes SSH, repository persists sidecar | aliases are mutated in both resources | explicit UUID continuity for known operations |
| group/metadata mutation | repository writes v1 sidecar | alias references | UUID references after v2 |
| restart | SSH config and sidecar are read independently | no durable UUID projection today | compare persisted projection to newly loaded projection |
| secrets | secret provider uses alias/host/user candidate keys | independent of sidecar metadata | separate credential policy; no automatic UUID transfer |

## 3. Alias assumption inventory

This is the migration inventory; it is intentionally not implemented here.

| Area | Current assumption | Classification | Risk |
|---|---|---|---|
| \`ConnectionRecord.id\`, \`nickname\`, \`host\` | Host alias is record identity and launch selector | A/B boundary | high |
| loader, config document/store, formatter | Host token is SSH syntax and source provenance | A: remains alias | low/medium |
| repository CRUD and service | ConnectionId identifies an app record and is also alias | B | very high |
| groups, root order, safe metadata | references are alias strings | B | high |
| API summary/group/metadata IDs | wire identity is alias-shaped | C | very high |
| daemon reload diffs/events | created/deleted/updated keys are aliases | C/B | high |
| terminal/SFTP/transfer/runtime controllers | aliases are passed to launch/runtime operations | A or D | medium/high |
| raw editor rename heuristic | raw signature transfers alias-keyed app state | B | high; retire |
| secret provider/credential candidates | host/user/alias identify credential lookup | D: separate policy | security |
| plugin protocol-local records | plugin IDs may not be SSH identity | D/E | preserve |
| comments/source paths/formatting | not durable identity | E | must not become identity |

The safe migration boundary is an internal resolver with both \`ssh_alias\` and
UUID available. Loader-private evidence must not become a public wire field.

## 4. Ownership model

### SSH-owned

The SSH configuration remains authoritative for connectivity:

\`\`\`text
Host alias, HostName, User, Port, IdentityFile, ProxyJump,
forwarding, authentication options, Match/Include semantics, and other
OpenSSH directives.
\`\`\`

An alias is a current launch selector, not an immutable application identity.

### SSH Pilot identity-owned

The sidecar owns:

\`\`\`text
UUIDv4, identity lifecycle, current projection snapshot, last trusted
reconciliation evidence, DisplayName, and tombstone/ambiguity diagnostics.
\`\`\`

A projection records what was last reconciled; it is not a second launch
configuration source.

### SSH Pilot metadata-owned

The sidecar owns safe app decoration:

\`\`\`text
groups, tags, pinned state, notes, root ordering, and other existing
non-secret per-connection metadata.
\`\`\`

Only fields actually present in v1 are migrated; this prototype invents none.

### Secret-store-owned

Passwords, key passphrases, backend credentials, and credential lookup policy
remain outside this sidecar. UUID continuity does not imply credential
continuity.

## 5. Proposed v2 schema

The prototype uses a UUID-keyed identity map because lookup and duplicate
detection are central invariants. Arrays are retained where user order matters.

\`\`\`json
{
  "version": 2,
  "sidecar_generation": 12,
  "last_reconciled_ssh_revision": "sha256:...",
  "observed_ssh_revision": "sha256:...",
  "identities": {
    "11111111-1111-4111-8111-111111111111": {
      "display_name": "Production Server",
      "projection": {
        "alias": "prod",
        "hostname": "server.example",
        "port": 22,
        "username": "deploy",
        "username_literal": "deploy",
        "username_is_explicit": true,
        "identity_files": [],
        "identity_file_evidence_mode": "unspecified",
        "identity_file_evidence_status": "unavailable",
        "identity_file_evidence_values": [],
        "identity_file_evidence_reason": "no_identityfile_directive",
        "destination_evidence_status": "trustworthy",
        "destination_evidence_reason": "explicit_static",
        "declaration_order": 0,
        "source": ".ssh/config"
      },
      "tombstone": false
    }
  },
  "groups": [
    {
      "id": "work",
      "name": "Work",
      "order": 0,
      "color": "",
      "members": [
        {"kind": "ssh_uuid", "id": "11111111-1111-4111-8111-111111111111"}
      ]
    }
  ],
  "root_connections": [
    {"kind": "ssh_uuid", "id": "11111111-1111-4111-8111-111111111111"}
  ],
  "metadata": {
    "11111111-1111-4111-8111-111111111111": {"pinned": true, "tags": ["prod"]}
  },
  "non_ssh_connections": [],
  "non_ssh_metadata": {},
  "legacy_orphans": [],
  "pending_ambiguities": []
}
\`\`\`

### Schema decisions

* \`identities\` is keyed by canonical lowercase hyphenated UUIDv4. UUIDs are
  generated once and never derived from alias, HostName, or order.
* \`projection\` persists the last loader projection and evidence status used
  for reconciliation. This is required for stopped-daemon alias rename.
  \`source\` and declaration order are provenance/diagnostics only.
* Destination evidence persists status/reason and valid hostname/port. An
  unavailable anchor is represented explicitly, never by a guessed default.
* IdentityFile evidence persists \`UNSPECIFIED\`, \`EXPLICIT_NONE\`,
  \`EXPLICIT_FILES\`, or \`DYNAMIC\`. Ordered static literals are tie-break
  evidence only. No secret material or key contents is stored.
* DisplayName is identity-owned and is not replaced by a later alias.
* Groups and root order use typed references. Non-SSH IDs remain outside the
  SSH UUID namespace.
* Placement preserves current service semantics: an active connection is in
  one or more groups or appears exactly once in root order, never both. A
  tombstone appears in neither. Multiple group membership remains allowed.
* Metadata is keyed by UUID for SSH records. Non-SSH metadata remains keyed by
  its protocol-local ID until a separate protocol-neutral migration. Metadata
  may remain attached to tombstones for historical identity safety.
* \`legacy_orphans\` quarantines stale v1 references rather than silently
  dropping them or attaching them to a future alias.
* \`pending_ambiguities\` contains only the unresolved candidate sets for
  \`observed_ssh_revision\`; it is not an ambiguity-history store and never
  assigns UUIDs.
* \`sidecar_generation\` counts durable sidecar changes. It is not repository
  or API snapshot generation. SSH revision is the loader source-tree revision.

## 6. v1 to v2 migration

Migration is a pure conversion:

\`\`\`text
read v1 + current loader projections
    -> allocate one UUIDv4 per current concrete SSH projection
    -> resolve alias references to typed UUID/non-SSH references
    -> quarantine stale refs and deduplicate repeated refs
    -> validate complete v2 state
    -> caller atomically writes v2
\`\`\`

The prototype implements this in \`migrate_v1_state()\` and returns a report.
Allocation order is deterministic for a supplied projection sequence; UUID
values are random UUIDv4 in production. It does not mutate v1.

Policy:

* Every current concrete SSH projection gets exactly one UUID. Duplicate
  aliases in loader input are rejected.
* Existing safe metadata with a non-empty string \`display_name\` bootstraps
  DisplayName; otherwise DisplayName is exactly the current alias. No pretty
  name is inferred from HostName.
* Group/root aliases resolve only when the legacy string belongs to exactly one
  namespace. If it is both an SSH alias and a non-SSH ID, the reference is
  quarantined as a namespace collision. Duplicate refs are retained once with
  diagnostics.
* A stale group, root, or metadata alias becomes a \`legacy_orphans\` record.
  It is not silently dropped and never resurrects a later identity. Group/root
  orphans retain their original index, group ID, and source container length so
  a future repair can restore ordering.
  The persisted forms are canonical: group members require group ID and valid
  index/length coordinates; root orphans require valid coordinates and no group
  ID; metadata orphans carry no placement coordinates.
* Group membership wins a v1 group/root conflict. Any active record absent from
  both group and root is appended to root in current SSH projection order,
  followed by existing non-SSH order.
* Non-SSH records and safe metadata preserve their existing IDs.
* A second startup sees v2 and does not rerun allocation. UUID collision or
  malformed input fails before a v2 write.
* The caller writes v2 only after conversion and validation succeeds, using the
  existing hardened same-directory atomic writer. Failures before
  \`os.replace()\` preserve the v1 bytes exactly; a failure after replacement
  while fsyncing the parent reports durability unknown.

## 7. Lifecycle, deletion, and alias reuse

For a safe external rename:

\`\`\`text
U1 / DisplayName Production Server / old-prod / server.example
    -> trusted reconcile ->
U1 / DisplayName Production Server / prod / server.example
\`\`\`

Groups, tags, pinned state, notes, and root UUID order do not change.

Explicit typed deletion retires the UUID. Tombstones are retained indefinitely
by default until a future explicit maintenance/purge operation, excluded from
automatic reconciliation and active placement, and never eligible for UUID
reuse. No age-based purge occurs. Historical metadata may remain attached.

An external disappearance may become a tombstone only after the adapter has
confirmed a normal deletion. Ambiguous old candidates remain active/pending.
A reused alias pointing to another destination never resurrects the tombstone.

## 8. Ambiguity behavior

The pure result is not a state transition when it contains ambiguity:

\`\`\`text
ambiguous old UUID candidates: retained, not deleted
ambiguous new projections: accepted for SSH launch, not UUID-assigned
metadata: not transferred
\`\`\`

Recommended production behavior is to accept the newly parsed SSH projection
for launch while freezing UUID ownership. The watcher/repository keeps the
last committed UUID registry and records a stable pending ambiguity diagnostic.
This keeps valid SSH configuration usable without attaching metadata arbitrarily.

An unresolved alias may appear in an alias-compatible runtime/API snapshot without
being a UUID-owned app connection internally. A future explicit operation can
supply continuity, deletion, or creation intent. Unchanged retries must produce
the same candidate set. Never turn ambiguity into create/delete automatically.

### Future backend resolution contract

The future backend operation is conceptually
\`ResolveConnectionIdentityAmbiguity\`. It is not a public API or UI operation
in this phase. The isolated \`AmbiguityResolution\` validator models its
guarded input:

\`\`\`text
expected_ssh_revision
expected_sidecar_generation
old_to_new: one-to-one (old UUID, new alias) mappings
explicit_creates: unresolved aliases intentionally made new
explicit_deletes: old UUIDs intentionally retired
\`\`\`

The request must account for every old UUID and every new alias in exactly one
of those sets. Every old/new reference must belong to the same pending
ambiguity; no UUID or alias may be reused. Revision or generation mismatch is
a stable stale-state failure. A successful production adapter would apply the
explicit choices, preserve DisplayName/metadata only for mapped UUIDs, clear
that pending ambiguity, increment the sidecar generation, and never touch
secrets. Ordering is never consulted.

Pending ambiguity is revision-scoped. If a complete SSH configuration changes
before resolution, the old diagnostic is not reused. The adapter reconciles the
last committed UUID registry against the newest projections and replaces or
clears pending diagnostics from that result. A stale ambiguity cannot block a
later unambiguous configuration.

## 9. Startup, live reload, and raw editor

### Startup while stopped

1. Read v2 with strict size, symlink, JSON, UUID, evidence, reference, and
   metadata validation.
2. If only v1 exists, load SSH projections, run migration, and atomically write
   v2. Preserve v1 until the v2 write is committed.
3. Parse current SSH configuration and obtain its source-tree revision.
4. Reconcile persisted active projections against current projections.
5. Safe result: update projections/metadata and commit a sidecar generation.
   Ambiguity: retain UUID ownership, record pending candidates and observed
   revision. Parse failure: retain last-known-good identity state and report
   configuration error.
6. Persist only after the validated result and source revision are known.

Missing v2 with no v1 starts empty under explicit current-configuration policy.
A corrupt/unsupported v2 must not be replaced by an empty file: preserve it,
load SSH for launch if safe, and report identity state unavailable pending repair.
The stable backend state is conceptually \`SSH_STATE=AVAILABLE\` and
\`IDENTITY_STATE=UNAVAILABLE_CORRUPT\`; UUID-owned metadata remains degraded
until the sidecar is repaired.

### External edit while running

The existing watcher debounce and transient-write protections remain. Once a
complete source tree is available, it passes loader projections to the same
UUID adapter used at startup. Safe results commit; ambiguity changes diagnostics
only; parse failure keeps last-known-good identity projection.

### Raw editor

The future path is:

\`\`\`text
parse candidate tree -> loader projections -> one UUID reconcile operation
safe -> commit SSH + sidecar transaction
ambiguous -> keep config launchable, freeze ownership, diagnose
\`\`\`

\`_raw_record_signature()\` is old behavior, not a second identity algorithm. It
should be retired after the shared path and rollback tests are productionized.

### Typed in-app mutation

Known typed rename/split/duplicate/delete operations should supply explicit
continuity, validate the expected SSH revision, and persist the same UUID. They
must not infer their own operation through Rule 2.

## 10. Crash consistency and recovery

The two files cannot be atomically renamed together. Existing atomic writers
protect each file, but production UUID persistence needs a small sidecar intent.

Recommended dual-resource protocol:

1. record \`base_ssh_revision\` and sidecar generation;
2. write/fsync a pending intent containing transaction ID, base revision, target
   revision, base sidecar generation, operation label, and a complete validated
   target state;
3. write SSH with the existing expected-revision/atomic writer;
4. fsync and commit sidecar target state with the new SSH revision;
5. fsync parent and clear the pending intent atomically.

Recovery:

| Config | Sidecar/intent | Recovery |
|---|---|---|
| old | old, no intent | normal startup |
| new | old + pending target | finalize only if revision equals target |
| old | old + pending target | abort stale intent |
| unrelated | old + pending target | preserve old state, diagnose, reconcile actual config; never guess |
| new | new, no intent | revisions agree; normal startup |
| old | new, no intent | reconcile actual config against new projection |
| parse-invalid | either | retain last-known-good identity state and report error |

External edits have no application intent: config may arrive first and sidecar
may lag. Startup/reload reconciles the actual source revision. Safe results
update state; ambiguity never transfers ownership; transient empty/missing
Includes use existing retry rather than tombstone resurrection.

The pending transaction is a complete validated target snapshot, not a
replayable mutation journal. Recovery depends on durable desired state plus
revision rather than replaying old commands.

Invariant:

> No active UUID assignment is committed from stale, incomplete, dynamic, or
> ambiguous evidence. SSH config may be newer than sidecar, but restart cannot
> silently attach old metadata to an arbitrary projection.

## 11. Corruption and file safety

Reuse current state-file hardening: reject symlink targets, enforce size and
strict UTF-8, require object JSON, write same-directory temp files with fsync,
atomically replace, preserve safe permissions, and fsync the parent.

Reject without silently resetting identity state:

\`\`\`text
invalid JSON, unsupported version, duplicate/invalid UUID, invalid trusted
projection evidence, unavailable evidence carrying an anchor, duplicate root
refs, unknown group/metadata refs, group cycles, unsafe metadata, malformed
non-SSH records, partial/oversized files, and symlink targets.
\`\`\`

The current v1 fallback to an SSH-only decoration snapshot is legacy
containment. v2 should preserve invalid bytes/recoverable backup and expose
identity state unavailable rather than replacing it with an empty registry.

## 12. Revisions and generations

* loader \`root_revision\`: content/provenance revision of the complete SSH source
  tree; store as \`last_reconciled_ssh_revision\` only after successful identity
  reconciliation;
* \`observed_ssh_revision\`: latest complete revision even when ambiguity is
  pending;
* \`sidecar_generation\`: durable v2 state generation;
* repository/API generation: in-memory/public snapshot sequencing.

The transaction intent compares the SSH revision it was based on with the
revision now on disk. It does not use API generation as a filesystem lock.

## 13. Non-SSH, metadata, and secrets

Non-SSH records remain protocol-local in the initial v2 production phase.
Typed references make the boundary explicit; a later protocol-neutral layer can
unify them after plugin contracts are audited. This is a deliberate phase
boundary, not a blocker for SSH UUID persistence.

| Current v1 field | v2 target | Rule |
|---|---|---|
| group \`connection_ids\` | typed SSH UUID/non-SSH refs | resolve, deduplicate, quarantine stale |
| root list | typed refs | preserve order, reject duplicates |
| metadata alias map | SSH UUID map + non-SSH map | preserve safe values; bootstrap explicit DisplayName |
| group id/name/parent/order/color | same fields | group identity remains group-owned |
| non-SSH records | preserved objects | no protocol migration here |
| tags/pinned/notes/WoL and other existing safe fields | UUID metadata | migrate only fields actually present and safe |

The current secret provider uses alias/host/user-shaped lookup candidates. This
design changes no secret key or lookup. Credential continuity is deferred to a
separate security design and does not block initial v2 persistence.

\`\`\`text
UUID/app metadata continuity != credential continuity
\`\`\`

No password, passphrase, secret handle, or private key material belongs in v2.

## 14. Future public API migration

Public \`ConnectionSummary.id\`, group IDs, metadata IDs, runtime requests, and
frontend IDs remain alias-compatible now. Safe sequence:

1. productionize v2 types, serializer, migration, and recovery;
2. repository owns UUID registry internally while API retains an alias resolver;
3. startup, live reload, raw editor, and typed operations share one path;
4. groups, metadata, and root order become UUID-backed internally;
5. API exposes distinct \`id=UUID\`, \`ssh_alias\`, and \`display_name\`;
6. migrate clients/frontends and contract tests;
7. only then consider DisplayName presentation;
8. design credential continuity separately.

Never give \`id\` dual semantics. Whether a wire version/capability bump is
needed depends on compatibility testing; old clients must never interpret a UUID
as an SSH alias. This is a later API phase and does not block initial internal
UUID persistence. No DTO or codec changes are made here.

## 15. Phase 1 filesystem implementation

The production storage boundary is additive. Existing v1 functions
\`read_connection_state()\`, \`write_connection_state()\`, and
\`read_legacy_connection_state()\` remain unchanged for current repository
callers. New entrypoints are:

The v1 file remains a legacy compatibility format: its historical parser
normalization and tolerance are authoritative and are intentionally different
from the strict canonical v2 format. The version probe delegates recognized
version 1 payloads to that parser and recognized version 2 payloads to
\`IdentityStateV2.from_dict()\`; it does not maintain a parallel schema.

* \`probe_connection_state_file()\` classifies absent, v1, v2, unsupported,
  and corrupt files without treating corruption as empty state;
* \`read_identity_state_v2()\` and \`write_identity_state_v2()\` strictly
  construct/serialize \`IdentityStateV2\` through model invariants;
* \`migrate_connection_state_v1_to_v2()\` accepts authoritative loader
  projections, runs pure migration, and atomically replaces v1 only after
  successful validation;
* \`connections.json.pending\` is the deterministic same-directory intent
  path. Its read/write/clear functions use the same UTF-8, symlink,
  mode, temporary-file, fsync, and generic-error protections as v1;
* \`recover_pending_identity_transaction()\` applies only BASE and TARGET
  decisions. Unrelated, stale, incomplete, corrupt, or unavailable state is
  returned to the higher-level adapter without UUID changes.

The main v1/v2 sidecar has a 16 MiB serialized-byte limit. Pending intents have
an explicit 32 MiB serialized-byte limit because they contain a complete target
sidecar plus an envelope. The nested target is independently required to fit
the 16 MiB sidecar limit before the intent can be written. Both use
deterministic sorted JSON and complete target snapshots. Filesystem persistence
performs no DNS, network, subprocess,
OpenSSH execution, or secret access.

A successfully read pending intent also passes the nested target through the
same 16 MiB sidecar-size check. Therefore a readable intent always contains a
target that the production sidecar writer can represent.

The intent format has its own version, transaction ID, base/target SSH
revisions, base sidecar generation, operation label/kind, and a fully
validated \`target_state\`. Normal operations require the target's
\`last_reconciled_ssh_revision\` to equal the target revision. Explicit
There are two persisted kinds: \`normal\` and \`pending_ambiguity\`.
\`normal\` requires last-reconciled and observed revisions to equal the target,
and contains no pending ambiguities. \`pending_ambiguity\` requires a non-empty
pending set scoped to the target observed revision and permits
last-reconciled to remain behind while UUID ownership is frozen. The old
prototype spelling \`ambiguity_resolution\` is rejected. Explicit ambiguity
resolution is a later sidecar identity decision against an SSH configuration
already on disk; it is not this dual-resource intent kind unless a future
operation also writes SSH configuration. Target generation must equal base
generation plus one.

Recovery classification is:

| Actual complete SSH revision | Current sidecar | Decision |
|---|---|---|
| target | base generation or exact target state | finalize target, then clear intent |
| base | base generation | abort/clear intent, retain sidecar |
| unrelated | base generation | require higher-level reconciliation; retain intent |
| any | newer/conflicting generation | stale intent; never overwrite |
| unavailable/partial | any | deferred; do not clear or apply |

Malformed intent and malformed v2 state are errors, not \`NO_PENDING\`. A
corrupt main sidecar is preserved and cannot be replaced from an intent. An
intent symlink is rejected. External SSH edits create no intent; they remain a
later repository/daemon reconciliation concern.

## 16. Prototype, tests, and policy status

Added:

* \`src/sshpilot/core/connections/identity_state_v2.py\`: UUIDv4 state model,
  typed references, persisted projections, strict v2 invariants, v1
  migration, orphan quarantine, and migration reports;
* \`src/sshpilot/core/connections/state_file.py\`: shared hardened v1/v2
  filesystem primitives, version probe, atomic migration, pending-intent
  storage, and recovery application;
* \`tests/core/test_connection_identity_state_v2.py\`: migration preservation,
  stale/duplicate handling, strict refs, JSON round-trip, factory collision,
  actual-loader restart rename, restart ambiguity, tombstone alias reuse, and
  conservative legacy evidence decoding; pending ambiguity referential
  integrity; tombstone placement; root/group normalization; orphan ordering;
  namespace collisions; strict metadata; and guarded resolution accounting.
* \`tests/core/test_connection_identity_state_storage.py\`: strict v2 disk
  reads/writes, version detection, atomic migration/idempotence, actual-loader
  restart rename and ambiguity, intent serialization, crash-window recovery,
  stale/corrupt/symlink handling, and secret exclusion.

The loader integration tests call \`load_ssh_configuration()\` and
\`ConnectionIdentityProjection.from_record()\` before serializing and
deserializing state; they are not disconnected toy-model tests.

The following policies are resolved for initial sidecar implementation:

1. tombstones are retained indefinitely until explicit purge;
2. pending transactions contain complete validated target snapshots;
3. corrupt v2 is preserved, SSH remains usable where possible, and identity
   state is degraded/unavailable;
4. non-SSH IDs remain protocol-local;
5. credential continuity is separate and deferred;
6. public API UUID migration is a later phase with distinct fields.

The only future consumer-specific interaction is the explicit backend
ambiguity-resolution operation described above; it has a defined revision,
generation, one-to-one, and complete-accounting contract.

## 17. Recommended implementation sequence

\`\`\`text
Phase 1  production v2 types, hardened writer integration, migration and recovery tests
Phase 2  repository internally owns UUID registry; public API remains alias-compatible
Phase 3  startup, live reload, raw editor, and typed operations share one path
Phase 4  groups, metadata, and root order are fully UUID-backed
Phase 5  public API exposes UUID + ssh_alias + display_name as distinct fields
Phase 6  clients/frontends migrate to UUID identity
Phase 7  DisplayName presentation, separately reviewed
Phase 8  credential continuity/security design and any explicit migration
\`\`\`

No phase authorizes UI work in this task.

## Recommendation

The schema, strict invariants, migration, restart evidence, ambiguity boundary,
resolved tombstone/transaction/corruption policies, and deferred phase
boundaries are coherent. Production sidecar implementation may proceed, while
the production adapter must preserve the explicit ambiguity-resolution contract
and remain separate from public API, UI, and secret migration.

**VERDICT: PRODUCTION UUID SIDECAR STORAGE READY — PROCEED TO REPOSITORY UUID INTEGRATION**
