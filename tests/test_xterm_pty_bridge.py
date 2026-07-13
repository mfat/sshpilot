"""Headless tests for the in-process PTY bridge engine.

These exercise the real Vte.Pty spawn + GLib fd pump on a live /bin/bash, with no
WebKit/display involved, so they can run in CI. The bridge is the Phase-2 core of
the embedded (Cursor-model) PyXterm backend.
"""
import os

import pytest

import gi

try:
    gi.require_version("Vte", "3.91")
except Exception:  # pragma: no cover
    pass
from gi.repository import GLib, Vte  # noqa: E402

# The default test harness stubs `gi` (see tests/conftest.py); this module needs
# REAL Vte.Pty (headless — no display required). Skip cleanly under the stub so it
# never breaks the normal suite. Run it with real PyGObject via:
#   python3 tests/test_xterm_pty_bridge.py
_REAL_GI = type(GLib).__name__ != "_DummyGIModule" and hasattr(getattr(Vte, "Pty", None), "new_sync")
if not _REAL_GI:  # pragma: no cover
    pytest.skip("requires real PyGObject/Vte.Pty (headless)", allow_module_level=True)

from sshpilot.xterm_pty_bridge import XtermPtyBridge  # noqa: E402


def _run_bridge(script_bytes, extra_delay_ms=800):
    """Spawn bash, drive `script_bytes` into it, return captured output + state."""
    out = []
    state = {"pid": None, "err": None, "exited": None}
    loop = GLib.MainLoop()

    bridge = XtermPtyBridge(
        on_output=out.append,
        on_exit=lambda status: state.__setitem__("exited", status),
        flush_ms=8,
    )

    def on_spawned(pid, err):
        state["pid"], state["err"] = pid, err
        if err is not None:
            loop.quit()
            return
        # drive the scripted input, then exit
        bridge.write(script_bytes)
        GLib.timeout_add(extra_delay_ms, _finish)

    def _finish():
        bridge.write(b"exit\n")
        GLib.timeout_add(400, lambda: (loop.quit(), False)[1])
        return False

    bridge.spawn(
        ["/bin/bash", "--norc", "--noprofile", "-i"],
        env=["TERM=xterm-256color", "PS1=$ ", f"PATH={os.environ.get('PATH','')}"],
        rows=24,
        cols=80,
        on_spawned=on_spawned,
    )
    GLib.timeout_add(6000, lambda: (loop.quit(), False)[1])  # safety
    loop.run()
    bridge.close()
    return "".join(out), state


def test_spawn_and_echo():
    text, state = _run_bridge(b"echo BRIDGE_ECHO_OK\n")
    assert state["err"] is None
    assert isinstance(state["pid"], int) and state["pid"] > 0
    assert "BRIDGE_ECHO_OK" in text


def test_resize_reflected_in_pty():
    out = []
    state = {"pid": None}
    loop = GLib.MainLoop()
    bridge = XtermPtyBridge(on_output=out.append, flush_ms=8)

    def on_spawned(pid, err):
        state["pid"] = pid
        bridge.resize(40, 123)
        bridge.write(b"stty size\n")
        GLib.timeout_add(700, _finish)

    def _finish():
        bridge.write(b"exit\n")
        GLib.timeout_add(400, lambda: (loop.quit(), False)[1])
        return False

    bridge.spawn(["/bin/bash", "--norc", "--noprofile", "-i"],
                 env=["TERM=xterm", "PS1=$ ", f"PATH={os.environ.get('PATH','')}"],
                 on_spawned=on_spawned)
    GLib.timeout_add(6000, lambda: (loop.quit(), False)[1])
    loop.run()
    bridge.close()
    assert "40 123" in "".join(out)


def test_incremental_utf8_not_corrupted():
    # A multibyte char (é = 0xC3 0xA9) printed many times; the incremental decoder
    # must never yield replacement chars even if reads split a codepoint.
    text, state = _run_bridge(b"printf '\\303\\251%.0s' {1..2000}; echo\n", extra_delay_ms=900)
    assert state["err"] is None
    assert "�" not in text          # no U+FFFD replacement chars
    assert text.count("é") >= 2000  # all the é's survived


def test_child_exit_reported():
    text, state = _run_bridge(b"true\n", extra_delay_ms=300)
    assert state["exited"] is not None    # child_watch fired


def test_no_lingering_timer_after_child_exit():
    """The bridge must stop its flush timer when the child dies, even if close()
    is never called (tab teardown kills the child via pgid, not close())."""
    state = {}
    loop = GLib.MainLoop()
    bridge = XtermPtyBridge(on_output=lambda c: None,
                            on_exit=lambda s: state.__setitem__("exited", s), flush_ms=8)

    def on_spawned(pid, err):
        bridge.write(b"exit\n")
        GLib.timeout_add(700, lambda: (loop.quit(), False)[1])

    bridge.spawn(["/bin/bash", "--norc", "--noprofile", "-i"],
                 env=[f"PATH={os.environ.get('PATH','')}", "PS1=$ "], on_spawned=on_spawned)
    GLib.timeout_add(6000, lambda: (loop.quit(), False)[1])
    loop.run()
    assert state.get("exited") is not None
    assert bridge._flush_source is None   # pump stopped, no leaked GLib source
    assert bridge._fd_source is None
    bridge.close()  # idempotent


if __name__ == "__main__":
    # Standalone runner with real gi (bypasses the harness's gi stub).
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {exc!r}")
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
