# Daemon-Owned File Transfers (Phase 10)

Uploads and downloads are first-class daemon resources, separate from SFTP
command RPCs but executed through a READY SFTP service.

## Modes

Phase 10 production path uses **daemon-local paths**: GTK supplies a local
filesystem path accessible to the same-user daemon. Binary client streaming
is deferred (API rejects non-`daemon_path` mode).

## Identity

```text
transfer-<n>
```

## Lifecycle

States: `queued` → `starting` → `running` → (`cancelling` →) `completed` /
`cancelled` / `failed`. `paused` is reserved; resume-by-offset is deferred.

## Concurrency

Default limits (daemon transfer runtime):

- at most **4** concurrent transfer worker threads
- at most **32** queued transfers beyond the active set
- additional `transfers.start` requests raise `SERVER_BUSY`

These bounds are configurable on `TransferRuntime` construction. Completed
worker threads are removed; shutdown joins workers within the configured
deadline.

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

## Phase 10.1 validation (exercised)

Real OpenSSH (Alpine container) + ephemeral `DaemonServer` coverage includes:

- zero / small / binary / multi-MiB upload and download with SHA-256 match
- overwrite reject and accept
- mid-transfer cancel cleans remote and local temporary files
- recursive directory upload and download (empty dirs, nesting, per-file
  conflict policy, cumulative byte progress)
- queue + `SERVER_BUSY` when over the concurrency limit
- SFTP close while a transfer is active fails or cancels the transfer
- cancel-vs-complete races (5×)

Not yet claimed as production-validated: resume, binary client streaming,
headed GTK transfer-queue rediscovery UI, or 100 MiB throughput benchmarks.
