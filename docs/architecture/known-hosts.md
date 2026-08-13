# Known hosts

The known-hosts file is daemon-owned; the frontend has no filesystem fallback.
The final ownership classification is recorded in the
[frontend closure audit](frontend-closure-audit.md).

`sshpilot.core.known_hosts` owns:

* parsing / filtering
* host match helpers (including hashed-line substring search)
* removal planning
* atomic writes with mode preservation and symlink refusal

The daemon owns the known-hosts **file**. `src/sshpilot/daemon/known_hosts_service.py`
resolves the path, reads exact bytes, parses a lossless document (revision =
SHA-256 of the input bytes; entry IDs deterministic per revision + physical
line), and applies removals with an optimistic revision check. Byte storage is
atomic (`src/sshpilot/core/known_hosts/file_io.py`): symlink refusal, mode
preservation, temp-file `fsync`, atomic replace, and parent-directory `fsync`.

The daemon exposes two RPCs — `known_hosts.list` (`KNOWN_HOSTS_READ`) and
`known_hosts.remove` (`KNOWN_HOSTS_WRITE`) — gated by capability negotiation.
`DaemonClient.list_known_hosts` / `remove_known_host_entries` implement the
client side; removals surface `stale_editor` when the file changed since the
snapshot.

The daemon's byte-level reads and atomic mutation preserve the file exactly:
comments, blank lines, malformed lines, ordering, LF vs CRLF, and the presence
(or absence) of a final newline are all retained. Removal changes only the
selected physical lines.

`KnownHostsEditorWindow` is a GTK view that loads and removes through the
daemon-backed client via `sshpilot.gtk.known_hosts_controller`
(`KnownHostsController`). GTK receives revisioned snapshots only: rows store
entry IDs (so duplicate identical lines stay distinguishable via
occurrence-specific IDs), filtering is local to the rendered entries, removals
are staged and applied in one batched, revision-checked call, and a stale
revision triggers a reload without any automatic retry. There is no frontend
filesystem fallback — the editor never performs local file I/O and never
accepts or stores a known-hosts path.
