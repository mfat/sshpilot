# Daemon-Owned SFTP Services (Phase 10)

SFTP browsing and mutations are daemon-owned resources, independent of
terminal PTY sessions.

## Ownership

```text
GTK file manager
    → typed daemon SFTP API
    → daemon-owned OpenSSH `ssh … -s sftp` process
    → SFTP v3 wire client
    → remote filesystem
```

GTK must not spawn an internal SFTP subprocess on the production daemon path.

## Identity

Service IDs are daemon-lifetime only:

```text
sftp:<UUIDv4>
```

Nil UUIDs are rejected. IDs are never reused and are not persisted across
daemon restart.

## Lifecycle

States: `created` → `starting` → `ready` → `closing` → `closed`, or `failed`.

`sftp.open` acknowledges immediately with a `starting` summary
(`DeferredResult(respond_on_accept=True)`). Authentication continues through
the existing interaction broker. `READY` / `FAILED` arrive as events.

## Backend

OpenSSH subsystem (`ssh -s <host> sftp`) plus the in-tree SFTP v3 client
(`sshpilot.sftp`). No shell interpolation of remote names. Directory listings
use protocol metadata, not scraped `ls` text.

## Operations

Typed methods: list, stat, lstat, realpath, mkdir, rmdir, rename, remove,
chmod, symlink, readlink. Listings are bounded (default 2000) with truncation
metadata.

## Ownership / attachment

The originating client owns mutations and close. Same-user clients may attach
as observers. Client disconnect detaches; it does not close the service.
Panel teardown calls `sftp.detach`; explicit Disconnect calls `sftp.close`.

## Retention

Up to 50 closed service summaries are retained in memory. Daemon restart
clears all.
