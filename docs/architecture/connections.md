# Connection Store Ownership

M3 makes the daemon the sole owner of saved connection state. SSH identity is
the saved `Host` alias, including aliases loaded from included fragments.

The daemon owns the selected SSH configuration tree and the dedicated
`connections.json` file. The latter contains non-SSH connections, groups,
ordering, and safe metadata. Legacy `config.json` connection values are read
once and never rewritten. GTK receives complete immutable snapshots and
coherent `connection_store.changed` events; group expansion is frontend-only.

Repository mutations use one global generation, per-connection generations for
stale editors, and atomic rollback across SSH configuration and state-file
writes. Client-provided paths never select an authority.
