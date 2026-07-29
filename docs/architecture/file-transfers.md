# Daemon-Owned File Transfers (Phase 10)

Uploads and downloads are first-class daemon resources, separate from SFTP
command RPCs but executed through a READY SFTP service.

## Modes

Phase 10 production path uses **daemon-local paths**: GTK supplies a local
filesystem path accessible to the same-user daemon. Binary client streaming
is deferred (API rejects non-`daemon_path` mode).

## Identity

```text
transfer:<UUIDv4>
```

## Lifecycle

States: `queued` → `starting` → `running` → (`cancelling` →) `completed` /
`cancelled` / `failed`. `paused` is reserved; resume-by-offset is deferred.

## Atomicity

- Upload: write remote temporary sibling → rename to destination.
- Download: write local temporary sibling → rename to destination.
- Cancellation cleans temporary files. Partial final files are not left in
  place under the default atomic policy.

## Progress

Coalesced `transfer.progress` events (time/byte delta). Slow observers do not
block I/O. Final completion/failure/cancellation events emit exactly once.

## Conflicts

Explicit policies: fail, overwrite, skip, rename. No silent overwrite.

## Retention

Up to 200 completed transfer summaries. Active transfers are never evicted.
Daemon restart clears all.
