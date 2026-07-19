# Manual test harnesses

These scripts exercise the SFTP file manager against a **live SSH host** and are
run by hand, not by pytest:

```
python3 tests/manual/test_file_manager.py --connection NICKNAME [--remote-dir PATH]
python3 tests/manual/test_sftp_comprehensive.py --connection NICKNAME [--remote-dir PATH]
```

They require a reachable server configured in `~/.ssh/config` and will create
and delete files under the chosen remote directory.

## Live GUI smoke test

`live_test.py` boots the real app against a throwaway sandbox (its config,
data, state and `~/.ssh` are redirected to a temp dir — your real setup is
untouched) and drives it through the accessibility bus + D-Bus GActions:
create/connect/duplicate a connection, create/edit/move a group, trigger the
edit-while-connected reconnect prompt, and quit. Prints PASS/FAIL/SKIP per step
and exits non-zero on any failure.

```
python3 tests/manual/live_test.py                 # connect to localhost
python3 tests/manual/live_test.py --host h --user u
python3 tests/manual/live_test.py --no-connect    # skip SSH-dependent steps
python3 tests/manual/live_test.py --keep          # leave the app open to poke at
```

Needs PyGObject with the Atspi typelib and a desktop session with the
accessibility bus on (GNOME has it by default). The connect/reconnect steps
need an SSH server reachable with key auth and auto-SKIP otherwise.
`atspi_driver.py` is the reusable AT-SPI/D-Bus wrapper it's built on, and also
works standalone for poking at an already-running app (`python3
tests/manual/atspi_driver.py tree`).

This directory is ignored by pytest, ruff, and the type checker, so nothing
here can run in — or break — CI.
