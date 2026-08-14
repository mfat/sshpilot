# SSH Pilot connection identity persistence

Status: Internal UUID identity and DisplayName presentation are integrated.
The public SSH API ID intentionally remains the OpenSSH alias.

## Ownership

```text
~/.ssh/config       SSH-owned connectivity and Host alias
connections.json   SSH Pilot-owned UUID identity and app metadata
API id              current alias-compatible selector
display_name       UUID-owned human-readable presentation label
secret provider     passwords, passphrases, and credential policy
```

The UUID is never written into SSH config, derived from an alias, or exposed
as the current public `ConnectionSummary.id`. Non-SSH records retain their
protocol-local IDs.

## Reconciliation contract

The frozen automatic matcher is, in order:

1. exact active alias;
2. trusted static `(HostName, normalized Port)`;
3. unique explicit User plus comparable ordered IdentityFile evidence;
4. unique explicit User;
5. unique remaining 1:1 destination candidate; and
6. otherwise `AMBIGUOUS`.

Declaration order is diagnostic ordering only. It never transfers identity.
Ambiguous candidates are neither created nor deleted and never receive old
metadata or placement. Tombstones never participate in matching.

## Production repository boundary

`ConnectionRepository` loads SSH first, captures
`ConnectionIdentityProjection.from_record()` before service reconstruction,
then loads or migrates the v2 sidecar. It owns one `IdentityStateV2`; the
alias-shaped `ConnectionService` snapshot is derived from it and current SSH
loader records. The sidecar projection is never used as SSH configuration.

The shared adapter is used by startup, live reload, raw-editor saves, and the
remainder of managed SSH operations. Safe transitions preserve UUID,
DisplayName, metadata, group membership, and root UUID order. New SSH records
get a fresh UUID and alias-default DisplayName. Confirmed deletion tombstones
the UUID and removes active placement. Alias reuse cannot resurrect it.

When reconciliation is ambiguous, the current SSH aliases remain visible and
launchable through the compatibility projection, but no app-owned identity is
assigned to those new aliases. `pending_ambiguities` records the candidate set
for the current observed revision while `last_reconciled_ssh_revision` remains
the last fully reconciled revision. A later complete revision recomputes the
result; stale ambiguity is never reused.

## v2 sidecar

The canonical sidecar is strict and UUID-owned:

```json
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
        "username_literal": "deploy",
        "username_is_explicit": true,
        "identity_file_evidence_mode": "explicit_files",
        "destination_evidence_status": "trustworthy"
      },
      "tombstone": false
    }
  },
  "groups": [],
  "root_connections": [
    {"kind": "ssh_uuid", "id": "11111111-1111-4111-8111-111111111111"}
  ],
  "metadata": {
    "11111111-1111-4111-8111-111111111111": {"pinned": true}
  },
  "non_ssh_connections": [],
  "non_ssh_metadata": {},
  "legacy_orphans": [],
  "pending_ambiguities": []
}
```

Groups/root use typed `SSH_UUID` and `NON_SSH_ID` references. SSH metadata is
keyed by UUID; historical metadata may remain on tombstones. Tombstones are
retained indefinitely until an explicit future purge, are excluded from active
placement and reconciliation, and UUIDs are never reused.

## v1 migration

The production path is:

```text
load authoritative SSH projections and revision
  -> read v1 or legacy config.json decorations
  -> migrate_v1_state()
  -> validate complete IdentityStateV2
  -> atomically replace connections.json with v2
```

Every current concrete SSH projection gets one UUID. DisplayName initially
equals the alias, unless a suitable existing app-owned name is present. Group,
root, metadata, non-SSH records, and order are preserved using typed references.
Stale references are canonical `legacy_orphans`; ordered group/root orphans
retain alias, group/index/container-length coordinates. Namespace collisions
between an SSH alias and non-SSH ID are quarantined rather than guessed.
Group membership wins a group/root conflict; unplaced active records are
appended deterministically to root. The migration is idempotent and never
modifies legacy `config.json`.

The repository never returns to normal v1 writes after successful migration.
Corrupt or unsupported v2 is preserved; SSH remains readable/launchable where
possible, while UUID-owned mutations are degraded rather than applied to an
invented empty registry.

## Managed SSH transactions and recovery

Managed create/update/delete/duplicate/split uses the prepared SSH mutation
boundary:

```text
prepare exact SSH bytes and target loader projection (no writes)
  -> write complete validated connections.json.pending
  -> commit SSH with revision/bytes/symlink guards
  -> write target v2 sidecar
  -> clear pending intent and publish snapshot
```

The pending intent is a complete target snapshot, not a replayable journal. It
uses `normal` for a fully reconciled target and `pending_ambiguity` when SSH
Pilot committed a target SSH revision whose UUID ownership remains frozen.
`ambiguity_resolution` is not a valid persisted kind; explicit ambiguity
resolution is a later sidecar decision against already-written SSH config.

The main sidecar limit is 16 MiB serialized bytes. The same-directory pending
intent limit is 32 MiB, and its nested target must independently fit 16 MiB.
Both readers and writers enforce these closure rules.

Recovery is conservative:

| Actual SSH revision | Sidecar state | Action |
|---|---|---|
| target | base generation or exact target | finalize target and clear intent |
| base | base generation | clear intent and retain base |
| unrelated | base generation | require reconciliation; do not guess |
| any | newer/conflicting sidecar | stale intent; never overwrite |
| unavailable/partial | any | defer; do not apply or clear |

Pre-replace failures leave the old target byte-for-byte unchanged. A failure
after `os.replace()` while syncing the parent reports durability unknown.
External edits create no application intent and use normal repository reload
reconciliation.

## DisplayName and current API

`display_name` is an additive field on connection summaries and mutation
results. `ConnectionSummary.id`, group IDs, metadata IDs, runtime request IDs,
and frontend request handles retain current alias semantics. Internal UUIDs are
not serialized in those public fields. The API implementation version is 0.32
because the additive field is part of the generated public contract; the wire
protocol remains 1.0.

DisplayName accepts ordinary human text including spaces, Unicode, punctuation,
slashes, apostrophes, and `#`. It rejects empty/whitespace-only or oversized
values and does not use SSH Host-token validation. A DisplayName update writes
only the sidecar, preserves UUID/groups/metadata/credentials, and leaves SSH
config bytes unchanged. Editing the technical `nickname`/Host alias remains a
separate prepared SSH mutation with explicit UUID continuity.

Frontend connection objects continue using alias IDs for selection and daemon
requests. Sidebar, search, chooser, tabs, session restoration, and connection
editor presentation prefer DisplayName; SSH alias remains the technical label
and tooltip where useful. Existing migrated connections start with
`DisplayName == alias`, so appearance is unchanged until a user chooses a name.

## Secrets and remaining work

UUID/app-metadata continuity is not credential continuity. This implementation
does not re-key, copy, or store passwords, key passphrases, secret handles, or
private key material in the sidecar or pending intent. Existing secret lookup
behavior remains separate for a future security design.

The remaining optional project is a separately compatibility-reviewed public
UUID API migration. It is not required for internal UUID ownership or
DisplayName. A future explicit ambiguity-resolution backend operation must be
revision/generation guarded, one-to-one, account for every candidate, and must
never infer intent from ordering.

## Validation and status

The repository integration suite covers v1/v2 startup, direct legacy migration,
restart stability, stopped/live/raw-editor safe rename, 2:2 ambiguity,
metadata/group/root UUID ownership, alias reuse, DisplayName-only mutation,
prepared mutation side effects, and intent crash windows. API artifacts are
generated and checked after the additive `display_name` field.

**VERDICT: INTERNAL UUID IDENTITY + DISPLAYNAME MIGRATION COMPLETE — PUBLIC API IDS REMAIN ALIAS-BASED**
