"""Test-only stand-in for a PyInstaller-frozen SSHPilot binary.

GH #1166: on the macOS frozen build, sys.executable is the application
binary itself, not a Python interpreter. Any subprocess spawn that assumed
otherwise — (sys.executable, "<helper>.py", ...) — silently ran the wrong
thing instead of raising: the OS just relaunched the GUI (the single-
instance app forwarding to its already-running primary and exiting), never
reaching the helper logic at all.

The one property that matters for exercising that regression in tests is:
sys.executable is a *self-dispatching launcher*, not a plain interpreter, so
re-invoking (sys.executable, "--internal-*", ...) re-enters the same
dispatch — exactly like the real bootloader does for --daemon/--internal-*.
This launcher reproduces exactly that property and delegates straight into
the real run.py dispatch logic, so tests using it exercise production code,
not a reimplementation of it.
"""

from __future__ import annotations

import stat
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_PY = _REPO_ROOT / "run.py"


def write_fake_frozen_launcher(tmp_path: Path) -> Path:
    launcher = tmp_path / "fake_sshpilot_frozen.py"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import os, runpy, sys\n"
        "sys.frozen = True\n"
        "sys.executable = os.path.abspath(__file__)\n"
        f"runpy.run_path({str(RUN_PY)!r}, run_name='__main__')\n"
    )
    mode = launcher.stat().st_mode
    launcher.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return launcher
