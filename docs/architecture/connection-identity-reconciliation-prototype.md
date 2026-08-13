# Connection identity reconciliation prototype

Backend/core investigation only. No UI or frontend files were changed.

## 1. Baseline

- Branch: `dev`.
- Exact local and remote `dev` SHA: `bfc9cfe045e0f3d0a26e9543021429e0ba5169db`.
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
port/user/IdentityFile evidence for this prototype.

## 4. Matching algorithm implemented

1. Exclude tombstoned old entries.
2. Validate active old and new aliases are unique.
3. Match exact aliases first; consume both sides; reason `EXACT_ALIAS`.
4. For remaining entries, use literal `(HostName, normalized Port)`. Missing
   HostName, invalid/out-of-range ports, and non-static destinations have no
   anchor. Only Port is normalized; 22 is the default.
5. Within each anchor, consume exact `(User, ordered IdentityFiles)`
   partitions (`DESTINATION_USER_IDENTITY`), then User partitions
   (`DESTINATION_USER`), then all remaining pairs by declaration/projection
   order (`DESTINATION_ORDER_FALLBACK`).
6. Remaining old entries are deletes. Remaining new entries receive injected
   UUIDs and are creates. Tombstones never participate.
7. Results expose matched/created/deleted/ambiguous and match reasons.

Known in-app operations are a separate future path and should supply
`EXPLICIT_IN_APP_CONTINUITY`; they should not be inferred after a typed rename.

## 5. Match invariants

- Exact alias cannot be stolen by a collision group.
- Matching is one-to-one; no old UUID or new projection is consumed twice.
- Every new projection is matched, created, or explicitly ambiguous.
- Every unmatched active old identity is deleted; tombstones are excluded.
- Source, unrelated directives, comments, formatting, and network state do not
  affect matching.
- Hostname comparison is literal; only Port is normalized.
- Missing/explicit 22 are equivalent; invalid ports are not trustworthy.
- IdentityFiles remain an ordered tie-break sequence.
- DisplayName follows UUID continuity and is not cleared by SSH directive edits.
- Applying and serializing a result then reloading unchanged data is idempotent.
- UUIDs are injected in tests and never derived from aliases.

## 6. Edge-case matrix

| Scenario | Actual prototype | Status / test |
|---|---|---|
| same alias, no change; HostName/Port/User/IdentityFile/unrelated change | exact alias preserves UUID | RESOLVED: exact-alias test |
| same alias moved Include/source | exact alias; source ignored | RESOLVED: nested Include test |
| alias rename, same destination | destination match | RESOLVED: anchor test |
| rename + User changed | order fallback preserves UUID | RESOLVED + credential policy |
| rename + identities changed | User/order pass preserves UUID | RESOLVED |
| alias and destination changed | create/delete, no guess | RESOLVED |
| rename with no HostName | anchor unavailable; create/delete | EXPLICIT POLICY |
| missing Port vs 22; 022; non-default Port | 22 normalized; 022 parses as 22; other port literal | RESOLVED |
| invalid/out-of-range Port | no trustworthy anchor; loader public field still falls back to 22 | EXISTING LOADER LIMITATION |
| 1:1, 1:N, N:1, N:N collisions | deterministic consuming one-to-one passes | RESOLVED: parameter matrix |
| same/different User and ordered identities | Pass A, B, C; no scoring | RESOLVED |
| identical collision candidates | declaration order fallback; deterministic, not truth | OPEN DECISION |
| exact alias surviving collision | exact alias consumes first | RESOLVED |
| declaration reorder | declaration order only fallback evidence | EXPLICIT POLICY |
| multi-token partial/complete rename | tokens are separate projections | RESOLVED: loader test |
| wildcard/negated Host rules | rules, no saved UUIDs | RESOLVED |
| repeated concrete alias | loader merges using its own semantics | EXISTING LOADER LIMITATION |
| nested Includes, block move, Include order | source not evidence | RESOLVED / policy |
| Host `*` defaults | not fully applied by loader | EXISTING LOADER LIMITATION |
| Match user/host/originalhost/exec/localnetwork/canonical/final | retained as rules; no commands execute | EXPLICIT POLICY |
| multiple IdentityFiles/order/none | literal ordered sequence; `none` is empty | RESOLVED |
| raw vs expanded IdentityFile | literal evidence captured privately; expanded launch values remain separate | OPEN DECISION |
| case/trailing dot/IPv6/%h hostname forms | literal comparison | EXPLICIT POLICY |
| raw editor rename | existing broad heuristic can migrate unique signature | EXISTING BEHAVIOR |
| external rename while daemon runs | current path reports delete/create and loses visible alias metadata | BUG FOUND |
| external rename while stopped | serialized prototype registry preserves UUID by destination | RESOLVED PROTOTYPE |
| restart unchanged / Include move / collision rename | UUID persists in prototype registry | RESOLVED PROTOTYPE |
| delete then later same destination | tombstone excluded; new UUID | RESOLVED |
| missing sidecar / malformed sidecar | missing = empty registry; malformed raises | OPEN PRODUCTION POLICY |
| invalid config / atomic save / transient empty root | existing rollback and bounded retry remain in force | RESOLVED EXISTING PATH |
| idempotence, insertion-order determinism, no duplicate UUID/consumption | enforced by matcher/registry tests | RESOLVED |
| network calls | matcher has none; no network identity | RESOLVED |

## 7. Open decisions

### A. Totally indistinguishable collision

The base algorithm uses declaration order as requested. This is deterministic
bookkeeping, not evidence of truth. Recommendation: retain it only if product
accepts that tradeoff; otherwise emit `AMBIGUOUS` and require confirmation.
Automatic production migration is blocked until this is decided.

### B. IdentityFile representation

The loader currently expands `~`, environment variables, and selected `%`
tokens. Those values can vary across environments. Recommendation: persist
literal ordered values for reconciliation and use expanded values only for SSH
launching. This requires a sidecar schema decision.

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
or dynamic Match conditions. Missing HostName is therefore an unavailable
identity anchor. Invalid public ports currently fall back to 22, but private
literal evidence prevents the prototype from trusting that fallback.

IdentityFile values are expanded by the loader; the prototype captures literal
ordered evidence separately. The loader uses local `socket.gethostname()` for
the `%l` parser token, but the reconciliation module does not import or call
network APIs. `ssh -G -F <temporary-config> <alias>` remains a possible local
oracle for future parser tests, not the production identity mechanism.

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

Added `tests/core/test_connection_identity_reconciliation.py`: **39 passed**.
The existing focused suite remains **150 passed, 1 skipped**.

Final gates:

```text
pytest -q tests/architecture                 61 passed
pytest -q tests/core                          522 passed, 1 skipped
pytest -q tests/api                           650 passed
python3 scripts/generate_api_artifacts.py --check
API artifacts are current.
ruff check src/ tests/ scripts/generate_api_artifacts.py
All checks passed.
```

The complete `pytest -q` run reached **4,708 passed, 29 skipped**, with **22
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

No commits were created: `.git` is read-only in this execution environment.
No GTK, UI, frontend, API wire model, secret-store, or generated file changed.

## 15. Recommendation

The pure backend model is promising and restart-serializable, but external
daemon reload is not wired to it, the production sidecar remains alias-keyed,
the loader is not a complete OpenSSH effective-config evaluator, and the
indistinguishable-collision policy is unresolved. Groups, tags, order, and
display metadata cannot yet safely become UUID-owned in production.

Required blockers are the collision ambiguity decision, UUID sidecar migration
and crash recovery, literal-vs-expanded evidence policy, parser semantic gaps,
and public API compatibility planning. UI remains out of scope.

VERDICT: NOT READY — EXPLICIT DECISIONS REQUIRED
