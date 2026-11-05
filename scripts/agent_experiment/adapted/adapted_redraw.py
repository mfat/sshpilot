"""Bug-report criterion for 0ec819c6: with the default bridge, a daemon reply
is queued on GLib at a priority that runs ahead of GDK's redraw
(PRIORITY_HIGH_IDLE + 20 = 120; lower number = sooner). Mechanism-neutral."""
import threading

from sshpilot import gtk_client_bridge
from sshpilot.gtk_client_bridge import GtkClientBridge

GDK_REDRAW_PRIORITY = 100 + 20
GLIB_DEFAULT_IDLE = 200
#: Real GLib priority values. Under the test suite's stub GLib a constant bound
#: at import time is a placeholder object; it is translated back by identity.
REAL = {"PRIORITY_HIGH": -100, "PRIORITY_DEFAULT": 0, "PRIORITY_HIGH_IDLE": 100, "PRIORITY_DEFAULT_IDLE": 200, "PRIORITY_LOW": 300}


def test_reply_is_delivered_ahead_of_redraw(monkeypatch):
    seen = []
    done = threading.Event()
    placeholders = {id(getattr(gtk_client_bridge.GLib, name, None)): value for name, value in REAL.items()}

    def idle_add(callback, *args, **kwargs):
        value = kwargs.get("priority", GLIB_DEFAULT_IDLE)
        seen.append(value if isinstance(value, int) else placeholders.get(id(value), value))
        done.set()
        return 1

    monkeypatch.setattr(gtk_client_bridge.GLib, "idle_add", idle_add, raising=False)
    for name, value in REAL.items():
        if not isinstance(getattr(gtk_client_bridge.GLib, name, None), int):
            monkeypatch.setattr(gtk_client_bridge.GLib, name, value, raising=False)
    bridge = GtkClientBridge()
    try:
        bridge.submit(lambda: 42, on_success=lambda _v: None, on_error=lambda _e: None)
        assert done.wait(10), "reply was never handed to GLib"
    finally:
        bridge.shutdown(wait=True) if hasattr(bridge, "shutdown") else None
    assert seen and seen[0] < GDK_REDRAW_PRIORITY, seen
