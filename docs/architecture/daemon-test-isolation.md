# Daemon test isolation

Daemon tests must never connect to or interfere with a live user daemon.

## Strategy

`tests/daemon/conftest.py` installs an **autouse** fixture that redirects:

* `XDG_RUNTIME_DIR`
* `XDG_STATE_HOME`
* `XDG_CACHE_HOME`
* `XDG_CONFIG_HOME`
* `HOME`

into a per-test temporary tree and clears `SSHPILOT_DAEMON_SOCKET`.

`daemon_factory` always binds an explicit socket under the test `tmp_path`.

## Regression coverage

`tests/daemon/test_daemon_isolation.py` proves:

1. A decoy user-daemon socket is ignored / overridden
2. Two isolated daemons can run concurrently
3. Stale non-socket paths fail deterministically; stale sockets are unlinked
4. Stale metadata alone does not grant socket authority
5. Managers/clients do not cross-discover sessions across fixtures
6. Shutdown removes the socket
7. Failed setup still cleans up
8. Shutdown versus new-client races do not deadlock

## Parallelism

Per-test temporary roots make pytest-xdist safe when installed. Explicit
`socket_path=` arguments keep workers from colliding.

Manual confirmation: Phase 13.1 smoke step 40 runs
`tests/daemon/test_daemon_isolation.py` while a user daemon may be present and
asserts the suite stays green without touching `/run/user/$UID/sshpilot/sshpilotd.sock`
sessions.

Phase 13.2 smoke step 40 still runs `tests/daemon/test_daemon_isolation.py` while a user daemon may be present.
