# Daemon terminal streaming

Phase 7 adds an experimental Unix PTY data plane without changing the default
GTK terminal path. Phase 9 makes this the production GTK terminal path with
VTE integration. The daemon owns each PTY master, child process, process
group, replay buffer, attachment, and terminal sequence. GTK never receives a
file descriptor.

## Phase 9: Production VTE Integration

Phase 9 establishes daemon terminal streaming as the production path for GTK SSH terminals:

- **VTE emulation**: GTK receives daemon output via VTE `feed()` and `commit()` calls
- **Multi-attachment**: Multiple GTK tabs can stream from same daemon session simultaneously
- **Input ownership**: Only one attachment owns input/resize authority per session
- **Session persistence**: Terminal streams survive GTK restarts through reattachment
- **Continuity handling**: Local markers shown for replay buffer gaps, never sent to daemon

## Ownership and launch

`SessionRuntime` creates the session record and its serial command lane.
`PtySessionProcessRunner` allocates an `openpty()` pair, applies the requested
window size, and starts the exact child with the slave as stdin, stdout, and
stderr. The child starts a new process session; termination signals target only
that owned process group. The parent closes the slave immediately and keeps the
non-blocking master until EOF and process exit have both been reconciled.

The daemon reuses the canonical `Connection.native_connect()` /
`build_ssh_connection()` path. Control-only sessions use
`interaction_policy="none"` with `BatchMode=yes` and strict host checking.
Phase 8 interaction-enabled sessions use `interaction_policy="broker"`:
`BatchMode` is disabled only after a typed responder path exists, strict
checking is retained against a session-private exact host-key pin, and
password/passphrase values use the private askpass broker. The environment
remains allow-listed and retains `SSH_AUTH_SOCK`.

One shared PTY I/O thread owns all master reads and writes. It limits each read
to 32 KiB and returns to its selector regularly. Input is queued per session
and capped at 256 KiB. No thread is created per output chunk.

## Three transport classes

The Unix socket multiplexes three independently accounted classes:

1. JSON control responses;
2. JSON lifecycle events;
3. binary terminal frames.

Control and lifecycle traffic take priority. Terminal output has a separate
per-peer 1 MiB limit inside the existing total 4 MiB peer limit, so a noisy or
slow terminal cannot consume the response reserve. Terminal bytes never enter
the CoreEvent queue, deferred completion queue, JSON, or logs.

## Binary frame v2

The existing four-byte unsigned big-endian outer length prefix is retained.
An outer payload beginning with `SPTB` is a terminal frame rather than JSON.
The fixed binary header is:

```text
magic[4] = "SPTB"
stream_version: u8 = 2
kind: u8
flags: u16
session_id: 32 bytes (null-padded UTF-8)
sequence: u64
attachment_id: 32 bytes (null-padded UTF-8, zero for output/status)
data: remaining raw bytes
```

Kinds currently cover output, input, continuity loss, and safe input rejection.
An input-rejection frame carries only a stable `ErrorCode` string and its safe
session/attachment identifiers; it never echoes input. Output flags identify
replay, EOF, and truncation. Each terminal payload is at most 64 KiB. IDs,
flags, lengths, kinds, stream versions, and attachment requirements are
validated before dispatch. Invalid binary input closes only the offending peer.

Handshake metadata advertises `binary-terminal-v1`. The daemon advertises
`terminal.output`, `terminal.input`, `terminal.resize`, and `terminal.replay`
only when its PTY runtime exists and the peer negotiated that frame type.
Control-only Protocol v1 clients therefore never receive an unknown frame.

## Sequence and replay

Each session owns an absolute byte-offset sequence beginning at zero. A chunk
covering `N` bytes at `sequence=S` represents `[S, S+N)`. Chunk boundaries have
no semantic meaning. EOF carries the final sequence. The daemon advances the
sequence before any client-specific queue decision, so lost output is
measurable.

Each session retains a chunked 2 MiB ring. Eviction advances the retained start
without changing the live sequence. `terminal.replay` returns range,
truncation, and EOF metadata in JSON and sends bytes as replay-flagged binary
frames. An offset before the retained start is truncated explicitly; an offset
beyond the live end is rejected.

Output-enabled attach captures replay and enables live delivery under the
runtime lock. Output accepted concurrently may overlap replay, but cannot leave
an unreported gap. `DaemonClient` deduplicates overlap by absolute byte offset
and treats a forward jump as continuity loss.

## Attachments, input, and resize

Only attached peers receive a session's output. The first eligible attachment
becomes the single input owner; later attachments are view-only. Detach or peer
disconnect releases ownership without terminating the session.

Terminal input is a raw binary frame. The daemon validates the handshaken
client, session, attachment, input ownership, running state, and maximum frame
size. Non-blocking partial PTY writes preserve byte order. Input is never
decoded or logged.

`terminal.resize` is a control RPC for the input owner. Rows and columns are
limited to 1–1000. A size supplied at open is applied to the slave before
launch; the latest pre-start resize wins. Running resize uses `TIOCSWINSZ`.

## Slow peers and continuity

PTY reading and replay eviction continue even with no attached reader, keeping
the child from blocking and memory bounded. When one peer exceeds its terminal
queue limit, only that peer/session stops receiving live output. Pending
terminal frames for that session are dropped, a continuity-loss status frame
reports the expected and currently available sequence, and the control channel
remains usable. The client can request replay if the ring still covers the gap;
otherwise truncation is visible. Healthy peers and other sessions continue.

## EOF, exit, close, and shutdown

Child exit and PTY EOF may arrive in either order. The runtime records the exit
but does not publish final lifecycle events until EOF has drained final output.
EOF is emitted once at the final byte sequence, followed by `session.exited`
and `session.closed`. Close stops new input, terminates and then kills only the
exact owned process group against bounded deadlines, and continues bounded PTY
drain.

Daemon shutdown rejects new terminal work, terminates owned sessions, closes
PTY descriptors and queues, stops the shared PTY I/O owner, then continues the
existing executor, peer, watcher, and socket cleanup. Repeated shutdown is
idempotent.

## Client and GTK integration

`DaemonClient` keeps one socket reader for JSON and binary frames. A separate
bounded terminal-dispatch thread prevents slow frontend callbacks from delaying
response correlation. `TerminalSubscription` is frontend-neutral and
idempotent.

`GtkClientBridge` batches terminal data into one GLib drain per binding and
caps pending GTK bytes. The experimental `DaemonTerminalWidget` uses VTE as a
pure emulator through `feed()`; VTE does not own or spawn the child. It sends
commit bytes and dimensions back through the client APIs. The normal VTE and
PyXtermJS rendering/input paths remain frontend-local; the remote child and SSH
launch remain daemon-owned.

Typed host-key/password/passphrase interactions are described in
[interaction broker](interaction-broker.md) and
[secret brokering](secret-brokering.md). Unrestricted keyboard-interactive,
reconnect replay, terminal persistence, remote transport, and Windows ConPTY
remain unsupported.
