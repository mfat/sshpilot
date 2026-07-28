# Local daemon transport

Phase 1 adds a Linux per-user daemon boundary for connection reads without
changing GTK's default composition.

Linux is the supported platform for this phase. Unix-socket primitives may be
present elsewhere, but macOS lifecycle integration and Windows named pipes are
explicitly deferred.

```text
DaemonClient
    -> one persistent AF_UNIX socket
    -> sshpilot.daemon selector loop
    -> explicit RequestDispatcher
    -> InProcessClient
    -> existing ConnectionManager
```

## Ownership and threading

`DaemonServer` constructs its injected core client on the same thread that runs
the selector and dispatches requests. This preserves `InProcessClient` and
GObject manager owner-thread rules. One selector loop handles multiple client
sockets without a thread per client, a thread per request, `asyncio.run()`, or a
per-call event loop.

`DaemonClient` is synchronous. One lock serializes calls over one blocking
socket, and every call has a finite timeout (five seconds by default). A timeout
closes the transport, making any late response unambiguous. Closing the client
shuts down its socket resources. GTK remains composed with `InProcessClient`;
no GTK main-loop behavior changes in this phase.

## Core construction

`python -m sshpilot.daemon` lazily constructs `Config`, `ConnectionManager`,
`GroupManager`, and one `InProcessClient`. Transport and envelope modules do not
import those managers or PyGObject. Tests inject a headless manager through the
same server factory and use a real Unix socket.

Daemon handlers never read persistence directly. Connection ordering, DTO
mapping, transitional identifiers, safe errors, and secret exclusion continue
to come from `InProcessClient`.

## Socket security

The default endpoint is:

1. `$XDG_RUNTIME_DIR/sshpilot/sshpilotd.sock`; or
2. a development fallback under the platform temporary directory at
   `sshpilot-<uid>/sshpilotd.sock`.

The immediate socket directory must be a real directory, owned by the current
UID, with mode `0700`. The socket is created with mode `0600`; ownership, file
type, and permissions are verified after bind. Symlinks and non-socket targets
are refused.

An existing socket is probed. A successful connection means another daemon is
active and startup fails. Only a same-inode socket whose connection is refused
is removed as stale. Shutdown unlinks only the socket inode created by that
server instance. No TCP listener, remote authentication, TLS, or cross-user
access exists.

## Lifecycle

The server owns its listener, wakeup pair, active client sockets, dispatcher,
and core client. `shutdown()` wakes the selector, stops accepting work, closes
active peers, closes the core client on its owner thread, and removes its socket.
The module entry point maps SIGINT and SIGTERM to this path. It does not install
or supervise a service.

## Current boundary

Only handshake, capability discovery, connection list, and connection get cross
the daemon. Event envelopes are parsed independently from responses by
`DaemonClient`, but the daemon forwards no runtime events and advertises no
event-dependent capability. Terminal/session runtime, PTYs, secrets, prompts,
SFTP, forwarding, plugins, and binary channels remain in-process and out of
scope.

## Packaging and lifecycle backlog

- add an explicit `sshpilotd` installed launcher after lifecycle policy settles;
- define systemd user activation without making it a runtime dependency;
- add launchd lifecycle integration for macOS;
- add Windows named-pipe transport and ownership checks;
- migrate saved connections to stable persisted UUIDs before freezing IDs;
- define bounded event forwarding and reconnect/resume semantics;
- define a separate binary terminal channel, PTY ownership, and prompt routing;
- add an opt-in GTK integration phase before changing the production default.
