"""Every GUI launcher exits with the status ``main()`` returns.

``main()`` returns the parse error code for bad flags (and
``Gio.Application.run``'s status otherwise); a launcher that drops it makes a
failed ``sshpilot host`` look like a success to the calling shell.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# --verbose and --quiet are mutually exclusive: argparse exits with 2 before
# the daemon or any window is started.
BAD_FLAGS = ["--verbose", "--quiet"]


def _gui_stack_importable() -> bool:
    probe = (
        "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); "
        "from gi.repository import Adw, Gtk"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, check=False, timeout=60
    )
    return result.returncode == 0


needs_gui_stack = pytest.mark.skipif(
    not _gui_stack_importable(), reason="PyGObject with GTK 4 / Adw 1 is not importable"
)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        entry for entry in (str(ROOT / "src"), env.get("PYTHONPATH")) if entry
    )
    env["SSHPILOT_NO_DAEMON_PRELAUNCH"] = "1"
    env["SSHPILOT_NO_STARTUP_PRELOAD"] = "1"
    return env


@needs_gui_stack
@pytest.mark.parametrize(
    "command",
    [
        pytest.param([sys.executable, "run.py"], id="run.py"),
        pytest.param([sys.executable, "-m", "sshpilot.main"], id="python -m sshpilot.main"),
    ],
)
def test_source_launchers_propagate_bad_flag_status(command):
    result = subprocess.run(
        command + BAD_FLAGS,
        cwd=str(ROOT),
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 2, result.stderr
    assert "not allowed with argument" in result.stderr


def test_nuitka_entry_propagates_main_status(monkeypatch, tmp_path):
    source = (ROOT / "packaging" / "windows" / "nuitka" / "sshPilot.py").read_text(
        encoding="utf-8"
    )
    fake_run = types.ModuleType("run")
    fake_run.main = lambda: 7
    monkeypatch.setitem(sys.modules, "run", fake_run)
    # The wrapper sets both; monkeypatch restores them afterwards.
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("XDG_DATA_DIRS", "")
    compiled = types.SimpleNamespace(containing_dir=str(tmp_path))

    with pytest.raises(SystemExit) as excinfo:
        exec(compile(source, "sshPilot.py", "exec"), {"__compiled__": compiled, "__name__": "__main__"})
    assert excinfo.value.code == 7
