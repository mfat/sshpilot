# Stable connection identity

Every persisted connection has one immutable UUID. Public APIs expose that
identity as an opaque `ConnectionId`; nicknames, SSH aliases, hostnames, users,
ports, and protocols remain mutable attributes rather than identity.

## Stored and public forms

The persisted field is named `uuid` and contains a canonical lowercase,
hyphenated, non-nil UUID. New records use UUID version 4. JSON-backed
connections store the primitive string directly. SSH-config-backed connections
store an sshPilot-owned comment immediately after the concrete `Host` line:

```text
Host example
    # sshpilot:ConnectionUUID example 550e8400-e29b-41d4-a716-446655440000
```

The host token makes identity unambiguous when one SSH config block contains
several concrete aliases. The marker does not alter OpenSSH behaviour.

The public form is:

```text
connection:550e8400-e29b-41d4-a716-446655440000
```

Formatting and parsing are centralized in `connection_identity.py`. Parsing is
bounded and strict: the prefix, complete standard hyphenated UUID text, and
non-nil value are required; valid uppercase input is canonicalized. API
consumers must continue treating the value as opaque.

## Immutability

`Connection` assigns an identity once. Creates and ordinary SSH-config imports
generate a fresh UUID before persistence. Update, rename, host/user/port
changes, serialization, reload, DTO mapping, and events retain it. Ordinary API
mutation DTOs do not contain a UUID field and cannot replace identity.
Duplicating or copying a connection creates a new record and therefore a new
UUID.

The manager indexes loaded connections by UUID and reuses the same live
`Connection` object when a reload changes its nickname. This preserves GTK row
identity, selection, and existing object-keyed terminal maps without moving
terminal ownership into the daemon.

## Upgrade migration

The authoritative `ConnectionManager` performs migration while loading:

1. acquire the per-config migration lock;
2. validate all existing identities;
3. preserve the first valid occurrence of each UUID;
4. assign UUIDv4 values to missing, malformed, nil, or later duplicate values;
5. construct complete updated SSH and JSON state;
6. atomically persist each changed file;
7. publish the migrated in-memory connection set only after persistence
   succeeds.

Uppercase but otherwise valid UUID text is canonicalized. For duplicate UUIDs,
the first record in persistence order keeps the value and later records receive
new UUIDs. Migration emits no connection lifecycle events.

Each individual file write uses a same-directory mode-0600 temporary file,
flush and `fsync`, atomic `os.replace`, and a directory `fsync` where supported.
The original primary file remains intact until replacement. A one-shot `.bak`
copy preserves the first pre-migration state and is not overwritten on later
launches. Temporary files are removed after failure. Symlink primary JSON
paths are refused.

SSH config and application JSON are separate files, so the migration is
restartable rather than a single cross-file transaction. If a later file fails,
already replaced files contain valid durable identities, no partially migrated
in-memory set is exposed, and the next launch resumes idempotently. An
unmigrated connection is never assigned an ephemeral public UUID ID.

## References

Group membership, root ordering, per-connection metadata, and new saved-layout
records use UUIDs internally. Existing nickname references are converted during
the same load migration:

- a unique current nickname maps to its connection UUID;
- already valid UUID references are retained;
- unresolved group references are discarded rather than rebound arbitrarily;
- one connection may remain in several groups and list order is preserved;
- deletion removes its UUID from group membership;
- rename requires no group-reference rewrite.

Saved layouts retain `nickname` as downgrade/display text and add a stable
`connection_id`; restore prefers the stable ID and falls back to nickname for
legacy records. SSH aliases, hostnames, historical text, and display labels
remain text because they are not identity references. Active terminal maps
remain object-based for now, with reload object reuse providing stability.

## Process ownership

In in-process mode, GTK's authoritative manager migrates during normal load.
In daemon mode, `sshpilotd` first acquires the secure socket endpoint, then
constructs its authoritative manager and migrates before it reports readiness.
The experimental GTK process loads its local compatibility manager with
migration disabled until daemon selection finishes, preventing both processes
from migrating the same files. If daemon selection falls back, GTK enables and
performs migration itself.

The secure per-config migration lock serializes multiple authoritative manager
loads. Daemon socket exclusivity prevents two daemon owners. Migration failure
prevents daemon readiness.

## Transitional IDs

Protocol v1 previously emitted `connection:v1:<hash>` values derived from
protocol and the current nickname. New responses and events never emit them.
During the Protocol v1 compatibility window, get/update/delete accept a
syntactically valid transitional value only when it matches the connection's
current protocol and nickname.

There is intentionally no permanent alias database. A pre-rename transitional
ID may stop resolving after rename. Malformed aliases fail as
`connection_not_found`. Canonical UUID-backed IDs are stable across rename,
metadata changes, reload, and daemon restart.

Transitional lookup is deprecated and is scheduled for removal with Protocol
v2, after external consumers have had one complete Protocol v1 release window
to refresh snapshots.

## Downgrade and recovery

OpenSSH and older sshPilot versions ignore the UUID marker comment, while the
existing serializers preserve unknown connection fields and comments. The
one-shot backups provide a recovery point. Older versions do not understand
UUID-based group membership, so downgrading may lose group presentation even
though primary connection UUIDs remain stored; back up the configuration
before downgrading.

UUIDs are identifiers, not secrets. Migration never derives them from
connection or credential material, never logs raw records, and does not widen
file permissions.

Daemon authentication uses the same stable connection identity when calling
the existing canonical password-storage API. Rename therefore cannot redirect
a pending typed interaction or its remember-after-success intent.
Private-key passphrases retain the existing exact identity-file association.

Daemon session records link to this stable `ConnectionId`; they do not copy or
derive identity from a nickname. Session IDs are a separate daemon-lifetime
namespace and are not persisted across daemon restarts.

After readiness, the daemon continues to own identity repair for external
configuration edits. GTK keeps migration disabled. A newly added host without a
marker is assigned and durably written a UUID before the daemon publishes it.
A single-host block whose UUID marker still names its pre-rename token retains
that UUID and has the marker rewritten. Reload migration emits no event storm:
only the stable-ID semantic diff is published.
