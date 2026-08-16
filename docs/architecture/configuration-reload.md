# Daemon-owned external configuration reload

The daemon has one authoritative persistence owner: `sshpilotd`. GTK keeps only
the read-only `ConnectionPresentationStore` projection and never repairs
connection identities or reloads files itself.

## Watch and execution model

The daemon uses a headless polling watcher. Its injectable boundary makes a
native backend possible later without changing reload semantics. The watcher
tracks:

- the root SSH configuration pathname;
- every resolved `Include` file;
- exact missing include pathnames;
- the nearest non-wildcard parent directory for wildcard includes; and
- `config.json`, which stores non-SSH connections and connection/group
  metadata outside GSettings.

Fingerprints include existence, device, inode, file type, size, and nanosecond
modification time. Watching pathnames rather than open file descriptors detects
atomic editor replacement. Include discovery is the same implementation used by
the SSH loader; a committed reload rebuilds the watch set.

Watcher callbacks only mark the configuration dirty. One coordinator uses a
200 ms monotonic debounce. At most one reload is active and at most one
follow-up is represented by a dirty flag. Reload work is submitted without
waiting to the daemon's bounded command executor under one configuration key.
Daemon connection mutations use the same key, so parsing and
CRUD persistence cannot run concurrently. Other executor keys and selector
work remain live.

## Transaction and diff

`ConnectionRepository` has a re-entrant authoritative-state transaction. Reloads
build a new connection collection with object reuse disabled, then publish the
new visible collection only after parsing succeeds. Readers therefore see either
the prior committed collection or the new one. Malformed input preserves the
previous visible collection.

The JSON document is read strictly before it replaces the in-memory document.
Invalid JSON or invalid group container types preserve the prior JSON, groups,
and connection snapshot. Short-lived missing or empty root-file states receive
two bounded stabilization retries; persistent removal is then treated as an
authoritative edit.

Diffing uses stable connection alias IDs and public `ConnectionSummary`
values. Events are emitted after commit in deterministic order:

1. `connection.deleted`
2. `connection.created`
3. `connection.updated`

A rename is one update with the same alias if the editor retains the Host
block. The loader also recognizes a single stale token in a single-host block,
which covers the common external `Host old` to `Host new` edit. A genuinely
new host is created with its SSH Host alias as the connection ID.

Daemon writes are not ignored by a timing window. Their filesystem notices
coalesce into a reload, and semantic equality produces no duplicate event or
loop. External changes made near a daemon write therefore cannot be suppressed.

## Failure, sessions, and shutdown

A failed reload logs only a safe failure category and retains the
last-known-good snapshot. A later filesystem change retries normally. No raw
configuration content, secret value, or event payload is logged.

Existing sessions keep their launch-time connection ID and process state.
Edits affect only later session opens. Deleting a saved connection does not
terminate an active session, while new opens for that ID fail after the commit.

Shutdown first rejects watcher callbacks, cancels debounce work, closes the
watcher, and stops the coordinator. The bounded daemon executor then drains or
cancels remaining commands under the existing daemon deadline. No reload runs
after the core client closes.

## Current limitations

- The production backend is polling-based with a one-second interval.
- Failure to start the injectable watcher fails daemon readiness; the polling
  backend is the dependency-free fallback and requires no display server.
- Group-only changes have no dedicated public event; a connection update is
  emitted only when its public summary (including group references) changes.
- Windows monitoring and service integration are not implemented.
- There is no remote filesystem monitoring or network configuration sync.
