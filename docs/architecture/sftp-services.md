# Daemon-owned SFTP services

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
sftp-<n>
```

IDs are never reused and are not persisted across daemon restart.

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
chmod, symlink, readlink, and daemon-owned remote copy/move. Listings are
bounded (default **2000** entries)
with truncation metadata; the GTK file manager must treat truncation as a
hard UI bound (no unbounded frame).

## Ownership / attachment

The originating client owns mutations and close. Same-user clients may attach
as observers. Client disconnect detaches; it does not close the service.
Panel teardown calls `sftp.detach`; explicit Disconnect calls `sftp.close`.

## Retention

Up to 50 closed service summaries are retained in memory. Daemon restart
clears all.

## Routing

Production file-manager routing (`sshpilot.extended_service_policy`) selects
daemon ownership when a `DaemonClient` advertises SFTP capabilities. There is
no frontend operational SSH/SFTP fallback. A client without the required
daemon capability receives the typed unsupported-capability result; it does
not start a second SFTP backend. Ordinary SCP UI uses the daemon transfer
route.

## Validation evidence

Real OpenSSH Alpine fixture + ephemeral daemon:

- password auth to `READY`; process cmdline contains `sftp`
- filesystem ops + unusual filenames (spaces, quotes, Unicode, emoji, leading dash, shell metacharacters)
- listing truncation metadata at the configured bound
- attach / detach / close; open+close-during-startup races (5×)
- shutdown with active SFTP leaves no orphan `ssh` process
