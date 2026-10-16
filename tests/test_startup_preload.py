"""Startup preload worker (``sshpilot.startup_preload``) and the GL warm-up."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from sshpilot import startup_preload
from sshpilot.platform.windows import gl_warmup

SRC = Path(__file__).resolve().parents[1] / "src"


def test_every_preloaded_module_exists():
    """A renamed module would otherwise be skipped silently."""
    missing = [m for m in startup_preload.MODULES if importlib.util.find_spec(m) is None]
    assert missing == []


def test_config_is_imported_on_the_calling_thread_not_the_worker():
    assert "sshpilot.config" not in startup_preload.MODULES


def test_preloaded_modules_need_no_gtk_and_register_no_gobject_types():
    """The worker runs beside Adwaita's start-up: no GTK, no new GTypes."""
    # The child counts real GTypes, so it needs PyGObject; the in-process gi is
    # conftest's stub, so ask a fresh interpreter (CI installs no PyGObject).
    probe = subprocess.run([sys.executable, "-c", "import gi"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("needs real PyGObject")
    code = r"""
import importlib, json, sys
BLOCK = ("gi.repository.Gtk", "gi.repository.Gdk", "gi.repository.Adw",
         "gi.repository.GtkSource", "gi.repository.Pango", "gi.repository.Gsk")
class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name in BLOCK:
            raise ImportError("blocked " + name)
sys.meta_path.insert(0, Blocker())
from gi.repository import GObject
import sshpilot.config  # what startup_preload.start() imports first
def gobject_types():
    seen, stack = set(), [GObject.Object]
    while stack:
        cls = stack.pop()
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return seen
import sshpilot.startup_preload as preload
before = gobject_types()
failed = []
for name in preload.MODULES:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append(f"{name}: {exc}")
new = sorted(f"{c.__module__}.{c.__name__}" for c in gobject_types() - before
             if c.__module__.startswith("sshpilot"))
print(json.dumps({"failed": failed, "new_gobject_types": new}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(SRC),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"failed": [], "new_gobject_types": []}


def test_wait_gives_up_on_a_stuck_worker(monkeypatch, caplog):
    stuck = threading.Event()
    worker = threading.Thread(target=stuck.wait, daemon=True)
    worker.start()
    monkeypatch.setattr(startup_preload, "_thread", worker)
    monkeypatch.setattr(startup_preload, "_WAIT_SECONDS", 0.05)
    try:
        startup_preload.wait()
        assert "still running" in caplog.text
    finally:
        stuck.set()


def test_a_failed_preload_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(startup_preload, "MODULES", ("sshpilot.no_such_module",))
    startup_preload._run()
    assert "Preloading sshpilot.no_such_module failed" in caplog.text


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_disable_switches_accept_the_same_values(monkeypatch, value):
    from sshpilot import daemon_prelaunch

    monkeypatch.setenv(startup_preload.DISABLE_ENV, value)
    monkeypatch.setenv(gl_warmup.DISABLE_ENV, value)
    monkeypatch.setenv(daemon_prelaunch.DISABLE_ENV, value)
    monkeypatch.setattr(gl_warmup, "_started", False)
    monkeypatch.setattr(daemon_prelaunch, "_state", None)

    assert startup_preload.disabled()
    assert gl_warmup.start() is False
    assert daemon_prelaunch.start() is False


def test_gl_warmup_skips_renderers_that_do_not_use_wgl(monkeypatch):
    monkeypatch.delenv(gl_warmup.DISABLE_ENV, raising=False)
    monkeypatch.setattr(gl_warmup, "_started", False)
    monkeypatch.setattr(gl_warmup.sys, "platform", "win32")
    for renderer in ("cairo", "Vulkan"):
        monkeypatch.setenv("GSK_RENDERER", renderer)
        assert gl_warmup.start() is False


def test_gl_warmup_is_windows_only(monkeypatch):
    monkeypatch.delenv(gl_warmup.DISABLE_ENV, raising=False)
    monkeypatch.delenv("GSK_RENDERER", raising=False)
    monkeypatch.setattr(gl_warmup, "_started", False)
    monkeypatch.setattr(gl_warmup.sys, "platform", "linux")
    assert gl_warmup.start() is False


@pytest.mark.skipif(sys.platform != "win32", reason="WGL is Windows only")
def test_gl_warmup_creates_and_drops_a_context_without_error():
    gl_warmup._warm()  # raises on a ctypes signature mistake
