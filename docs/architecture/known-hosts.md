# Known hosts

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

`KnownHostsEditorWindow` is a GTK view that loads and removes through the
daemon-backed client via `sshpilot.gtk.known_hosts_controller`
(`KnownHostsController`). Rows store entry IDs (so duplicate lines stay
distinguishable), removals are staged and applied in one batched,
revision-checked call, and the editor never performs local file I/O.
