# Extended Service Lifecycle (Phase 10)

SFTP services, transfers, and forwards share common lifecycle rules while
remaining independent of terminal PTY transport.

## Independence

A connection may have zero or more of each:

- terminal sessions
- SFTP services
- transfers
- forwards

Closing a terminal tab does not close SFTP or forwards. Connection-wide
“Disconnect all” must explicitly close each service domain.

## GTK restart

On startup the client lists SFTP services, transfers, and forwards, validates
the daemon instance ID, and restores UI references. Panels are not
automatically reopened unless configured. Active transfers and forwards
continue.

## Daemon restart

All service IDs become invalid. Clients must treat prior IDs as stale and
obtain a fresh snapshot after reconnect. No cross-restart persistence.

## Connection metadata changes

Running services keep their launch snapshot. Renames may update display
labels only where safe. Settings changes affect new services only. Deleting
a saved connection does not silently kill active services; the UI labels
orphaned actives.

## Shutdown

1. Reject new services
2. Cancel or finish transfers under a deadline
3. Terminate SFTP and forward processes
4. Clean temporary files
5. Emit final states where possible
6. Close workers and descriptors

Bounded, idempotent, no orphan SSH processes.

## Phase 10.1 race coverage

Ephemeral-daemon integration repeats (5× each, barrier-style waits):

- SFTP open + close during startup
- transfer complete vs cancel
- forward ACTIVE + close (no `ssh -N` leak)
- combined shutdown with SFTP + transfer + dynamic forward (idempotent
  second `shutdown()`, socket removed)
