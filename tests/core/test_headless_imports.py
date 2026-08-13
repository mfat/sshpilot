"""Headless import gate: core / api / daemon must not touch ``gi``.

Uses a fresh subprocess so already-loaded modules cannot hide violations.
Also asserts that plain ``import gi`` fails under the blocker (not a fake module).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

HEADLESS_MODULES = (
    "sshpilot.core",
    "sshpilot.core.errors",
    "sshpilot.core.settings",
    "sshpilot.core.validation",
    "sshpilot.core.keys",
    "sshpilot.core.known_hosts",
    "sshpilot.core.secrets",
    "sshpilot.core.plugins",
    "sshpilot.core.forwards",
    "sshpilot.core.import_export",
    "sshpilot.core.ssh",
    "sshpilot.core.connections",
    "sshpilot.core.transfers",
    "sshpilot.core.interaction",
    "sshpilot.core.cli",
    "sshpilot.api",
    "sshpilot.daemon",
)

_SUBPROCESS_SCRIPT = r"""
import builtins
import importlib
import sys

sys.path.insert(0, %r)

_real_import = builtins.__import__

def _blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".", 1)[0]
    if root == "gi" or name == "gi" or name.startswith("gi."):
        raise ImportError(f"gi is blocked in headless tests ({name})")
    return _real_import(name, globals, locals, fromlist, level)

builtins.__import__ = _blocked_import
sys.modules.pop("gi", None)
for key in list(sys.modules):
    if key == "gi" or key.startswith("gi."):
        sys.modules.pop(key, None)

# Plain import gi must fail hard (not return a permissive fake).
try:
    import gi  # noqa: F401
except ImportError:
    pass
else:
    raise SystemExit("import gi unexpectedly succeeded under blocker")

modules = %r
for mod_name in modules:
    importlib.import_module(mod_name)
print("OK")
"""


def test_headless_core_api_daemon_import_without_gi_subprocess():
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    # -I ignores PYTHONPATH; inject SRC inside the script instead.
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _SUBPROCESS_SCRIPT % (str(SRC), HEADLESS_MODULES),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout


def test_cli_subprocess_without_display():
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    script = ROOT / "sshpilot-core"
    proc = subprocess.run(
        [
            "env",
            "-u",
            "DISPLAY",
            "-u",
            "WAYLAND_DISPLAY",
            sys.executable,
            "-I",
            str(script),
            "validate-connection",
            "--nickname",
            "Demo",
            "--host",
            "example.com",
            "--user",
            "alice",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # sshpilot-core launcher adds src to sys.path itself.
    assert proc.returncode == 0, proc.stderr
