"""End-to-end regression test for GH #1166.

On the macOS PyInstaller build, sys.executable is the application binary
itself, not a Python interpreter. PtySessionProcessRunner.start() spawned
the PTY exec helper as (sys.executable, "_pty_child.py", ...) — which, run
against a real frozen binary, just relaunches the GUI (a second instance of
the single-instance app, forwarded to the primary and exited) instead of
ever calling os.execvpe on the real ssh command. The session then "exits
cleanly" with zero PTY output about a second later, and the tab silently
closes with no error — the exact symptom reported in the issue.

This test does not mock subprocess.Popen and does not stub out run.py's
dispatch: it spawns a real process, using a minimal stand-in for a frozen
binary (tests/helpers/fake_frozen_launcher.py) that reproduces the one
property the bug depends on — sys.executable is a self-dispatching launcher,
not a plain interpreter — and delegates straight into the real run.py. If
PtySessionProcessRunner ever regresses to unconditionally building
(sys.executable, "<path to _pty_child.py>", ...), this test fails exactly
the way the real bug did: the marker command never actually runs, so its
output never arrives. (Verified by temporarily reverting the pty_runner.py
fix locally: this test fails with exit_code=0 and zero captured output —
the exact signature from the reporter's log.)
"""

from __future__ import annotations

import os
import sys
import time

from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.daemon.pty_runner import PtySessionProcessRunner
from sshpilot.daemon.session_runtime import SessionLaunchSpec
from tests.helpers.fake_frozen_launcher import write_fake_frozen_launcher

_MARKER = b"SSHPILOT_PTY_OK"


def test_frozen_pty_spawn_execs_real_command_end_to_end(tmp_path, monkeypatch):
    launcher = write_fake_frozen_launcher(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(launcher), raising=False)

    output = bytearray()
    exit_info: list = []

    # Stands in for the real ssh argv the daemon would normally build; a
    # frozen-build regression here never runs this at all.
    runner = PtySessionProcessRunner(
        lambda _spec: (
            ("/bin/sh", "-c", f"printf {_MARKER.decode()}"),
            {"PATH": os.environ.get("PATH", "")},
        )
    )
    spec = SessionLaunchSpec(
        session_id=SessionId("session-1"),
        connection_id=ConnectionId("conn-1"),
        protocol="ssh",
        hostname="example.test",
        username="alice",
        port=22,
    )
    _handle = runner.start(
        spec,
        lambda info: exit_info.append(info),
        output.extend,
        lambda: None,
    )
    try:
        deadline = time.monotonic() + 10
        while _MARKER not in output and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _MARKER in output, (
            f"never received the marker over the PTY; captured={bytes(output)!r} "
            f"exit_info={exit_info!r}"
        )
    finally:
        runner.close()
