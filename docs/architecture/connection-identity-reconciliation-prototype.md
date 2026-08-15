# Connection identity reconciliation prototype

Backend/core investigation only. No UI or frontend files were changed.

## 1. Baseline

- Branch: `dev`.
- Exact `dev` base SHA for this correction: `6c7251dbfafd2825fe55b3f7f8c626737553eec8`.
- The preceding implementation commit is `6c7251dbfafd2825fe55b3f7f8c626737553eec8`; the
  worktree was clean before this correction.
- `git fetch` could not update `.git/FETCH_HEAD` because this checkout exposes
  `.git` read-only. `git ls-remote origin refs/heads/dev` independently
  returned the same SHA. The worktree was clean before changes.
- The supplied `7ed939f088ab9447913857fdc9f702eacd2efe2a` was not the current
  `dev` HEAD in this checkout.

Current identity is alias-shaped: `ConnectionRecord.id == nickname == Host`
alias. The loader materializes concrete Host tokens separately and resolves
recursive Includes. Wildcard/negated Host blocks are rules. `connections.json`
v1 stores groups, root order, and safe metadata keyed by aliases.

Before changes:

```text
pytest -q tests/core/test_connection_repository.py \
  tests/core/test_connection_repository_crud.py \
  tests/core/test_connection_repository_groups.py \
  tests/core/test_connection_repository_metadata.py \
  tests/core/test_ssh_config_text_editor.py \
  tests/daemon/test_config_reload.py \
  tests/test_connection_edit_host_tokens.py \
  tests/test_ssh_config_edit_preserves_groups.py
150 passed, 1 skipped
```

`ConnectionRepository._reconcile_raw_ssh_state_locked()` is called by the raw
editor save path only. Its `_raw_record_signature()` consists of
`record.source` plus every parsed data field except `id`, `nickname`, `host`,
`aliases`, `__host_tokens`, and `source`, represented as sorted key/repr pairs.
Thus source moves and any retained directive change can break a rename; it is
not a consuming collision matcher. `ConnectionRepository.reload()` does not
call it.

## 2. Actual code impact map

| File / symbol | Current assumption | Future ownership | Risk |
|---|---|---|---|
| `core/connections/models.py:ConnectionRecord` | ID/nickname are Host alias | alias stays projection; UUID becomes app identity | High |
| `core/connections/ssh_config_loader.py` | concrete tokens become records; rules/Includes are static loader output | source of concrete projections | Medium |
| `core/connections/ssh_config_store.py` | owns SSH writes, revisions, rollback, atomic safety | remains projection writer | Low/medium |
| `core/connections/repository.py` | CRUD, groups, roots, metadata, reload use alias IDs | UUID registry/reconciliation owner | Very high |
| `core/connections/state_file.py` | v1 sidecar is alias-keyed | migration target for UUID state | High |
| `core/connection_application_service.py` | service accepts alias-shaped ConnectionId | future UUID/alias resolver | Very high |
| `api/models/connections.py` | `ConnectionSummary.id` is current opaque alias ID | eventual UUID plus alias/display metadata | Very high |
| `api/models/connection_store.py` | group/metadata references are aliases | UUID-owned app state | High |
| `api/connection_identity.py`, `api/transport/codec.py`, `api/daemon_client.py` | typed wire IDs assume current contract | eventual compatibility migration | High |
| `daemon/config_reload.py` | watcher invokes repository reload and reports alias diffs | UUID-aware reload result | High |
| `ssh_config_document.py`, `ssh_config_formatter.py` | preserve ordinary SSH syntax | remain SSH projection machinery | Medium |
| `effective_config_check.py` | separate effective-config diagnostic/oracle | informs parser compatibility | Medium |
| `daemon/connection_secret_provider.py`, `credential_model.py` | passwords use host/user candidates | separate UUID credential policy | High/security |
| terminal/SFTP/CLI/plugin controllers | runtime IDs and nickname args are aliases | eventual resolver boundary | High |

Search classification: parser/store/formatter uses are true SSH alias
semantics (A); repository, groups, metadata, API IDs, runtime references and
secret requests are app identity (B); tags/pinning/recent/WoL are app-owned
decoration (C); session/transfer/SFTP/client/attachment IDs are unrelated
opaque identities (D); plugins, backups/imports, and credentials are uncertain
compatibility surfaces (E). No UI consumer was modified.

## 3. Prototype architecture

`identity_reconciliation.py` defines:

```text
IdentityRegistryEntry.uuid
        owns
display_name + app metadata
        points to
ConnectionIdentityProjection
        alias, literal hostname, normalized port, user,
        ordered identity files, declaration order, provenance
```

The matcher has no filesystem, GTK/GI, daemon RPC, subprocess, DNS, socket,
or network dependency. `IdentityRegistry` provides JSON-compatible
serialization and restart tests, but production v1 state is deliberately not
modified. `ConnectionRecord`/loader retain private, non-serialized literal
port/user/IdentityFile evidence plus explicit evidence status/reasons for this
prototype. Ordinary launch-oriented `hostname`, `port`, and resolved `username`
fields are never treated as proof by the matcher.

## Static evidence trust model

The loader now emits an explicit destination-evidence status and reason. Rule 2
only sees a `(HostName, normalized Port)` anchor when the conservative analyzer
proves a single static concrete Host block with valid literal values. Rule 1
exact-alias continuity remains valid even when every other evidence field is
unavailable.

| SSH construct | Classification | Implemented policy |
|---|---|---|
| explicit literal HostName in one concrete block | SAFE FOR RULE 2 | trusted only without the limitations below |
| missing HostName | SAFE ONLY FOR RULE 1 | alias fallback-to-destination is not evidence |
| explicit valid literal Port | SAFE FOR RULE 2 | decimal text is validated and 22 is normalized |
| missing/default Port | SAFE FOR RULE 2 only in a proven direct block | implicit 22 is rejected when inheritance is possible |
| Host `*` / wildcard / negated inheritance | SAFE ONLY FOR RULE 1 | disables Rule 2 for the affected loader snapshot |
| repeated concrete Host | SAFE ONLY FOR RULE 1 | disables Rule 2 because merge semantics are not proven |
| Include-derived values and Include order | SAFE ONLY FOR RULE 1 | current loader loses positional semantics, so Rule 2 is disabled |
| Match user/host/originalhost | DYNAMIC / RULE 2 DISABLED | retained as rules; never evaluated by identity code |
| Match exec/localnetwork/canonical/final | DYNAMIC / RULE 2 DISABLED | no command or environment evaluation occurs |
| HostName percent tokens, including `%h` | DYNAMIC / RULE 2 DISABLED | host-dependent/runtime-dependent values are unavailable |
| safe literal IdentityFile sequence | SAFE AS COLLISION TIE-BREAKER | raw ordered values only; never a destination anchor |
| IdentityFile percent tokens or environment variables | DYNAMIC / RULE 2 DISABLED for Pass A | excluded from identity-file equality; User Pass B may remain available |
| omitted User | SAFE ONLY FOR RULE 1 | local OS username is not durable evidence; omitted users reach only the unique remainder rule |
| explicit literal User | SAFE AS COLLISION TIE-BREAKER | used only in Pass A/B inside an already trusted destination group |
| loader-expanded launch values | PARSER LIMITATION | retained for launching, never promoted to identity evidence |
| HostName case, trailing dot, IPv6 spelling, DNS, `/etc/hosts` | OPEN DECISION | comparison remains literal; no network normalization |

This is intentionally not an OpenSSH evaluator. The current safe subset is
small; when the loader cannot prove effective semantics, it reports unavailable
evidence instead of manufacturing a default anchor. `ssh -G -F` tests are local
characterization oracles only and are not a production matcher dependency.

## Authentication evidence model

`IdentityFile` evidence is semantic rather than an empty/non-empty path list:

| Mode | Meaning | Pass A |
|---|---|---|
| `UNSPECIFIED` | no explicit `IdentityFile` directive | never comparable; falls to User/order |
| `EXPLICIT_NONE` | explicit `IdentityFile none` | comparable only to `EXPLICIT_NONE` |
| `EXPLICIT_FILES` | one or more ordered static literal expressions | comparable only to the same ordered values |
| `DYNAMIC` | `$` expansion, host/runtime percent token, or otherwise unsafe expression | never comparable; falls to User/order |

The mode is observable on `IdentityFileEvidence.mode` and serialized as
`identity_file_evidence_mode`. Pass A requires explicit User evidence plus an
exact match of both semantic mode and ordered values. `UNSPECIFIED` and
`DYNAMIC` are deliberately not equal to one another or to `EXPLICIT_NONE`, so
absence of a directive cannot masquerade as `IdentityFile none`. IdentityFile
never establishes destination continuity by itself.

The prototype adopts the config-intent interpretation of `~`: a raw value such
as `~/.ssh/id_a` is `EXPLICIT_FILES` because reconciliation compares whether
the configuration retained the same authentication intent, not whether the
physical path resolves to the same key under another home directory. Expanded
launch values remain separate. `$HOME` and every percent token except escaped
`%%` are dynamic; `%%` is a literal percent in config intent.

Prototype JSON from the preceding commit stored only status and values. When
that legacy payload has an empty safe tuple, deserialization chooses the weaker
`UNSPECIFIED` mode because it cannot safely invent `EXPLICIT_NONE`. Non-empty
safe values become `EXPLICIT_FILES`, and dynamic status remains `DYNAMIC`.

For the regression collision, the old empty-tuple implementation paired by
declaration order: `old-a(EXPLICIT_NONE)` could receive `new-b(UNSPECIFIED)`
and `old-b(UNSPECIFIED)` could receive `new-a(EXPLICIT_NONE)`. With semantic
modes, explicit-none candidates are compared only to explicit-none candidates;
unspecified candidates do not strengthen Pass A. If a remaining User partition
has multiple members, it is reported as one ambiguity rather than being zipped
by declaration order.

## 4. Matching algorithm implemented

1. Exclude tombstoned old entries.
2. Validate active old and new aliases are unique.
3. Match exact aliases first; consume both sides; reason `EXACT_ALIAS`.
4. For remaining entries, use literal `(HostName, normalized Port)` only when
   both projections carry `TRUSTWORTHY` static destination evidence. Missing
   HostName, inherited/default values whose provenance is uncertain,
   invalid/out-of-range ports, dynamic tokens, Include/Match uncertainty, and
   repeated blocks have no Rule-2 anchor. Only Port is normalized; 22 is the
   default.
5. Within each anchor, inspect exact `(User, IdentityFile mode, ordered
   values)` partitions (`DESTINATION_USER_IDENTITY`) only for explicit static
   modes, then explicit User partitions (`DESTINATION_USER`). A partition is
   consumed as a match only when it contains exactly one old and one new
   member. A partition with multiple plausible members is reserved as one
   `AMBIGUOUS` group and cannot fall through to a weaker pass.
6. After stronger partitions are consumed, a destination with exactly one old
   and one new remaining member is matched as
   `DESTINATION_UNIQUE_REMAINDER`. Declaration order is not used to establish
   that mapping. Multiple old/new remainders become one ambiguity group;
   one-sided leftovers remain ordinary deletes or creates.
7. Ambiguous old and new candidates are excluded from `deleted` and `created`.
   Ordinary unmatched old entries are deletes; ordinary unmatched new entries
   receive injected UUIDs and are creates. Tombstones never participate.
8. Results expose matched/created/deleted/ambiguous and match reasons.

Known in-app operations are a separate future path and should supply
`EXPLICIT_IN_APP_CONTINUITY`; they should not be inferred after a typed rename.

## 5. Match invariants

- Exact alias cannot be stolen by a collision group.
- Matching is one-to-one; no old UUID or new projection is consumed twice.
- Every new projection is matched, created, or explicitly ambiguous.
- Ambiguous old identities are not deleted and ambiguous new projections are
  not created; the result is intentionally not an automatic state transition.
- Every other unmatched active old identity is deleted; tombstones are excluded.
- Source, unrelated directives, comments, formatting, and network state do not
  affect matching.
- Hostname comparison is literal; only Port is normalized.
- Missing/explicit 22 are equivalent; invalid ports are not trustworthy.
- IdentityFiles remain an ordered tie-break sequence.
- `UNSPECIFIED`, `EXPLICIT_NONE`, `EXPLICIT_FILES`, and `DYNAMIC` are distinct
  semantic evidence modes; only comparable explicit modes enter Pass A.
- Persisted trusted destination anchors and IdentityFile evidence are rejected
  when their dataclass invariants are inconsistent.
- DisplayName follows UUID continuity and is not cleared by SSH directive edits.
- Applying and serializing a result then reloading unchanged data is idempotent.
- UUIDs are injected in tests and never derived from aliases.
- Declaration/projection order may stabilize ambiguity presentation but never
  maps one old UUID to one new projection.
- `apply_reconciliation()` rejects results containing unresolved ambiguity
  rather than silently dropping old identities or inventing new UUIDs.

## 6. Edge-case matrix

| Scenario | Actual prototype | Status / test |
|---|---|---|
| same alias, no change; HostName/Port/User/IdentityFile/unrelated change | exact alias preserves UUID | RESOLVED: exact-alias test |
| same alias moved Include/source | exact alias; source ignored | RESOLVED: nested Include test |
| alias rename, same destination | destination match | RESOLVED: anchor test |
| rename + User changed | User pass or unique remainder preserves UUID when unambiguous | RESOLVED + credential policy |
| rename + identities changed | User pass or unique remainder preserves UUID when unambiguous | RESOLVED |
| alias and destination changed | create/delete, no guess | RESOLVED |
| rename with no HostName | anchor unavailable; create/delete | EXPLICIT POLICY: alias-as-destination is ambiguous |
| missing Port vs 22; 022; non-default Port | 22 normalized; 022 parses as 22; other port literal | RESOLVED |
| invalid/out-of-range Port | no trustworthy anchor; loader public field still falls back to 22 | EXISTING LOADER LIMITATION |
| 1:1 destination remainder | one possible mapping; `DESTINATION_UNIQUE_REMAINDER` | RESOLVED |
| 1:N, N:1, N:N destination remainder | unresolved candidate set; no create/delete | RESOLVED: cardinality matrix |
| same/different User and ordered identities | unique partitions match; duplicate partitions are reserved ambiguous | RESOLVED |
| identical collision candidates | ambiguity group; no UUID transfer | RESOLVED: ambiguity policy |
| duplicate User partition | one candidate set, not ordered member matches | RESOLVED |
| duplicate User+Identity partition | one candidate set, not ordered member matches | RESOLVED |
| strong unique match plus ambiguous remainder | strong match consumed; only remainder ambiguous | RESOLVED |
| exact alias surviving collision | exact alias consumes first | RESOLVED |
| declaration reorder | presentation order only; never identity evidence | RESOLVED: explicit ambiguity policy |
| multi-token partial/complete rename | tokens are separate projections | RESOLVED: loader test |
| wildcard/negated Host rules | rules, no saved UUIDs | RESOLVED |
| repeated concrete alias | loader merges using its own semantics | EXISTING LOADER LIMITATION |
| nested Includes, block move, Include order | exact alias works; positional Include evidence disables Rule 2 | EXISTING LOADER LIMITATION |
| Host `*` HostName/Port/User defaults | no static destination or explicit User evidence | EXISTING LOADER LIMITATION |
| Match user/host/originalhost/exec/localnetwork/canonical/final | retained as rules; no commands execute; Rule 2 disabled | DYNAMIC / RULE 2 DISABLED |
| no IdentityFile directive | `UNSPECIFIED`; cannot strengthen Pass A | RESOLVED |
| `IdentityFile none` | `EXPLICIT_NONE`; distinct from unspecified and comparable only to explicit none | RESOLVED |
| multiple IdentityFiles/order | `EXPLICIT_FILES`; ordered sequence, reversed order differs | RESOLVED |
| raw vs expanded IdentityFile | raw literals are semantic config intent; expanded launch values remain separate | EXPLICIT POLICY: `~` accepted |
| `%h` HostName and host-dependent IdentityFile | destination unavailable; dynamic identity files excluded from Pass A | RESOLVED |
| `$HOME` and percent-token IdentityFiles | `DYNAMIC`; excluded from Pass A | RESOLVED |
| persisted evidence with invalid trusted anchor or invalid IdentityFile mode | deserialization rejects it | RESOLVED |
| omitted User vs explicit local username; both omitted | no local OS username evidence; only a unique remainder may match | RESOLVED |
| case/trailing dot/IPv6/%h hostname forms | literal comparison | EXPLICIT POLICY |
| raw editor rename | existing broad heuristic can migrate unique signature | EXISTING BEHAVIOR |
| external rename while daemon runs | current path reports delete/create and loses visible alias metadata | BUG FOUND |
| external rename while stopped | loader → registry JSON → loader → reconcile preserves UUID for trusted direct blocks | RESOLVED PROTOTYPE |
| restart unchanged / Include move / collision rename | UUID persists in prototype registry | RESOLVED PROTOTYPE |
| delete then later same destination | tombstone excluded; new UUID | RESOLVED |
| missing sidecar / malformed sidecar | missing = empty registry; malformed raises | OPEN PRODUCTION POLICY |
| invalid config / atomic save / transient empty root | existing rollback and bounded retry remain in force | RESOLVED EXISTING PATH |
| idempotence, insertion-order determinism, no duplicate UUID/consumption | enforced by matcher/registry tests | RESOLVED |
| network calls | matcher has none; no network identity | RESOLVED |

## 7. Open decisions

### A. Totally indistinguishable collision — resolved policy

Declaration order is not identity evidence. A 1:1 remainder is safe because
there is only one possible mapping; any 1:N, N:1, or N:N candidate set is
reported as `AMBIGUOUS`. Declaration/projection order may sort members for
stable diagnostics, but it must never transfer a UUID. `apply_reconciliation()`
rejects unresolved ambiguity. This is now an explicit product/architecture
decision, not an open choice in the prototype.

### B. IdentityFile representation — decided for this prototype

Use raw ordered literal expressions as config-intent evidence and keep expanded
values only for SSH launching. `~/.ssh/id_a` is therefore `EXPLICIT_FILES`,
while `$HOME`, `%h`, `%r`, `%p`, `%u`, `%d`, `%i`, `%l`, `%L`, `%C`, `%j`, and
`%k` are `DYNAMIC`. This avoids environment-dependent physical-path identity
while preserving meaningful repeated configuration intent. A future sidecar
must persist the mode and ordered raw values, not only an expanded path list.

### C. Dynamic Match-derived destination

The loader does not evaluate `exec`, local-network, canonical, or final
conditions. Recommendation: dynamic Match-derived values must not participate
in Rule 2; exact alias continuity may still apply.

### D. Case/format normalization

No HostName normalization beyond Port was implemented. Keep literal comparison
unless a later decision defines case, trailing-dot, Unicode, IPv6, or DNS
semantics. No network equivalence should be introduced.

### E. Tombstones

No long-lived tombstone participates in matching. The current bounded watcher
and retry path does not require tombstone resurrection. Recommendation:
tombstones remain diagnostic-only.

### F. Sidecar schema

Future v2 should own identities and app state by UUID, for example:

```json
{
  "version": 2,
  "identities": {
    "uuid": {
      "display_name": "Production",
      "last_projection": {"alias": "prod-paris", "hostname": "10.0.0.8", "port": 22},
      "tombstone": false
    }
  },
  "groups": {"prod": {"connection_ids": ["uuid"]}},
  "root_connections": ["uuid"],
  "metadata": {"uuid": {"tags": ["prod"]}}
}
```

Migration must preserve v1 groups/tags/order/metadata, generate UUIDs once,
reuse the existing hardened atomic writer, distinguish missing/corrupt state,
and never resurrect a deleted identity due to destination reuse.

### G. Public API migration

Not performed. The blast radius includes `ConnectionSummary.id`, all
`ConnectionId` DTOs/codecs/client methods, groups/root/metadata summaries,
repository CRUD and reload diffs, terminal/SFTP/transfers/sessions,
CLI/plugins, backup/import/export, and alias resolution. Add an internal UUID
registry and resolver before changing the public contract.

## 8. Existing heuristic comparison

| Mutation | Old raw-editor heuristic | Prototype |
|---|---|---|
| unique rename, all directives unchanged | may migrate only if source/signature match | destination evidence |
| rename plus User/Port/ProxyJump/forwarding change | signature breaks | destination still preserves |
| source/include move | source breaks signature | source ignored |
| destination collision | unique broad signature only | consuming Pass A/B/C |
| exact alias plus another rename | no explicit priority | exact alias consumes first |
| external repository reload | heuristic not called | prototype can reconcile, not wired |

## 9. Restart/offline editing

The isolated registry stores UUID, display name, last projection, and
tombstone state. Tests cover unchanged restart, stopped `old -> new` rename,
no-HostName ambiguity, collision restart, Include moves, and delete-then-reuse.
The complete loader → registry JSON → deserialization → alias rename → loader
→ reconcile path remains covered after the IdentityFile mode change.

Production external rename continuity is **not active yet**: the real
repository/daemon path remains alias delete/create. The new characterization
tests demonstrate this through both `ConnectionRepository.reload()` and
`AuthoritativeConfigurationBackend.reload()`.

## 10. Include/Match and effective-config findings

The loader is a static parser, not a complete OpenSSH effective-config
evaluator. It resolves recursive Includes with sorted glob order, materializes
concrete tokens, records wildcard/negated rules, accumulates selected
directives, and retains Match blocks as raw rules. It does not fully model all
OpenSSH first-value-wins/default interactions across repeated blocks, Host `*`,
or dynamic Match conditions. The analyzer therefore marks all concrete records
in snapshots containing wildcard/global/Match/Include uncertainty as unavailable
for Rule 2. Include placement is not retained in the materialized record, so
moving an included block can safely preserve exact aliases but cannot safely
prove a renamed destination. Missing HostName is likewise unavailable. Invalid
public ports currently fall back to 22, but private literal evidence prevents
the prototype from trusting that fallback.

IdentityFile values are expanded by the loader for launch behavior; the
prototype captures raw ordered evidence separately, with explicit semantic
mode, and marks `$`/percent-token values dynamic. The
loader uses local `socket.gethostname()` for the `%l` parser token, but the
reconciliation module does not import or call network APIs. Host-dependent
HostName expressions such as `%h.example.com` are never anchors. Focused
`ssh -G -F <temporary-config> <alias>` tests compare hostname/port/user where
available; `ssh -G` remains a local oracle, not the production identity
mechanism. Include inside a Host context is currently a structural parser
boundary and may produce no materialized connection, which is recorded as an
existing loader limitation rather than inferred identity.

## 11. Persistence/failure policy

The existing state/config writers already provide atomic same-directory
replacement, fsync, rollback capture, mode preservation, symlink refusal,
strict parsing, and generic errors. No competing writer was added. Future UUID
migration must reuse it and define crash recovery for config-new/state-old and
config-old/state-new combinations.

Current behavior: missing state initializes/migrates safe defaults; malformed
canonical state is rejected and the repository continues with SSH-only state;
invalid raw saves roll back; temporary root unavailability receives bounded
retry while preserving last-known-good state.

## 12. Secret implications

No secret code changed. Passwords are handled by
`DaemonConnectionSecretProvider`, `credential_model.canonical_password_host`,
and `password_host_candidates`: effective hostname, `host`, and nickname are
host candidates combined with username. `previous_hostname`, `previous_host`,
and `previous_username` are cleanup hints for in-app store/delete requests.
Key passphrases are keyed by normalized key path and may track referencing
nicknames in export metadata.

Alias/hostname/username edits can change current password candidates; external
reconciliation does not migrate secrets. UUID continuity must not imply
credential continuity. A future design must decide UUID-owned secrets,
confirmation on user/host changes, shared-destination behavior, and key-path
ownership. No secret values appear in this prototype.

## 13. Test results

Added `tests/core/test_connection_identity_reconciliation.py`; the final
validation count is recorded below. The suite now includes explicit ambiguity
cardinality, partition reservation, input-order, apply-safety, and actual
loader/restart cases.
The existing focused suite remains **150 passed, 1 skipped**.

Final gates:

```text
pytest -q tests/architecture                 61 passed
pytest -q tests/core                          598 passed, 1 skipped
pytest -q tests/api                           650 passed
pytest -q tests/daemon/test_config_reload.py tests/daemon/test_production_composition.py
                                             25 passed
pytest -q <relevant loader/repository/editor/config tests> 138 passed, 1 skipped
python3 scripts/generate_api_artifacts.py --check
API artifacts are current.
ruff check src/ tests/ scripts/generate_api_artifacts.py
All checks passed.
```

The final `pytest -q` run reached **4,782 passed, 29 skipped**, with **24
failures and 22 collection/setup errors**. These were outside the changed
backend path: optional `mcp`/`trio` availability, easyenv plugin daemon
settings, PTY/GTK stubs, daemon lifecycle/Xvfb cleanup, and existing terminal
helper mismatches. The four representative failures were reproduced directly
without the identity tests; no changed-file test failed. No generated API/UI
artifact was changed.

## 14. Files changed and commits

- `src/sshpilot/core/connections/identity_reconciliation.py`
- `src/sshpilot/core/connections/models.py`
- `src/sshpilot/core/connections/ssh_config_loader.py`
- `tests/core/test_connection_identity_reconciliation.py`
- this report

No commit was created in this working session; the requested base remains
`6c7251dbfafd2825fe55b3f7f8c626737553eec8`.
No GTK, UI, frontend, API wire model, secret-store, or generated file changed.

## 15. Recommendation

The pure backend model is restart-serializable and the indistinguishable
collision policy is now explicit: declaration order never transfers UUIDs.
External daemon reload is intentionally not wired in this prototype, the
production sidecar remains alias-keyed, and the loader is not a complete
OpenSSH effective-config evaluator. Those are productionization scope items,
not unresolved member-matching policy. Groups, tags, order, and display
metadata remain outside this task and must be migrated only after the UUID
sidecar design is reviewed. UI remains out of scope.

Remaining decisions are UUID sidecar migration and crash recovery, parser
semantic gaps outside the conservative safe subset, and public API
compatibility planning. The collision ambiguity policy is resolved.

The IdentityFile correctness issue is resolved in the prototype: semantic modes
are distinct, dynamic evidence cannot strengthen Pass A, legacy empty evidence
is decoded conservatively, and persisted evidence invariants are enforced.

VERDICT: RECONCILIATION PROTOTYPE COMPLETE — READY FOR PRODUCTION UUID PERSISTENCE DESIGN
