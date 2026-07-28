# Local daemon transport

Phase 2 adds an explicitly opt-in GTK path for the Linux per-user connection
read daemon. Production composition remains in-process by default.

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
shuts down its socket resources.

GTK never invokes that blocking API on its main thread. One application-scoped
`GtkClientBridge` owns a single worker. It serializes selection/startup and
connection-list reads, then posts results through `GLib.idle_add`. Per-widget
request tokens suppress stale delivery after refresh or destruction. Closing
the application closes the selected client and shuts down the bridge; closing
one window only invalidates that window's callbacks.

## Experimental GTK selection

Normal startup still selects `InProcessClient`. Development daemon mode is
enabled only for the current process:

```bash
SSHPILOT_CLIENT_MODE=daemon python3 run.py
```

The parser ignores surrounding whitespace and case. Missing or blank values
mean `in_process`; invalid values retain in-process mode and produce the same
safe compatibility warning as a failed daemon selection. The environment value
is not persisted as an application preference.

`api/client_factory.py` is the single selection policy. Widgets neither read
the environment nor construct `DaemonClient`. The current application has one
main window and one application-scoped client; future multi-window composition
must continue sharing that client rather than opening one socket per widget.

## On-demand startup and fallback

When daemon mode is requested, selection runs off the GTK thread:

1. validate the owned mode-0700 parent and any existing mode-0600 socket;
2. attempt a real Protocol v1 handshake with a short bounded probe;
3. require the negotiated `connections.read` capability;
4. if and only if the endpoint is unavailable, launch
   `sys.executable -m sshpilot.daemon --socket <resolved endpoint>`;
5. poll with a monotonic three-second deadline and repeated real handshakes;
6. use the daemon client, or fall back once to the existing in-process client.

There is no restart loop. Protocol incompatibility, malformed framing,
transport closure during handshake, missing capability, and unsafe filesystem
state do not trigger a daemon launch or retry. They still fall back for this
experimental application run so the established local core remains usable.
Logs record only a stable local failure category. The UI shows:

```text
The local SSH Pilot service could not be used. SSH Pilot is running in
compatibility mode.
```

Raw exceptions, socket paths, commands, environment values, and protocol data
are never placed in that notification.

The child is launched with an argument list and `shell=False`; standard streams
are detached to `/dev/null`. Known session-secret environment variables are
removed. A source-tree `src` import path is added so the documented module
command works before installation.

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

An already-running daemon is unowned by GTK. An on-demand child is represented
by the exact `Popen` object that created it; no PID lookup or process-name
matching exists. Failed startup terminates only that exact child. A successfully
started daemon is deliberately left running when GTK exits because Protocol v1
cannot prove that no other clients need it. Tests terminate their own exact
child and verify socket cleanup. An installed `sshpilotd` launcher and bounded
idle exit remain packaging/lifecycle work.

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
- keep daemon mode experimental until extended GTK lifecycle testing is
  complete; production-default selection is a separate decision.
