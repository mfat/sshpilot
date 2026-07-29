"""Real-GTK coverage for the experimental daemon terminal widget."""

import os
import sys
import time

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, _GLib = requires_gui()

try:
    import gi

    gi.require_version("Vte", "3.91")
    from gi.repository import Vte  # noqa: F401
except Exception as error:  # pragma: no cover - environment dependent
    pytest.skip(f"VTE unavailable: {error}", allow_module_level=True)

pytestmark = pytest.mark.gui


class _Connection:
    nickname = host = "TerminalTest"
    hostname = "terminal.test"
    username = "tester"
    port = 22
    protocol = "ssh"
    aliases = []
    auth_method = 0
    keyfile = ""
    identity_files = []
    certificate = ""
    certificate_files = []
    x11_forwarding = False
    forwarding_rules = []
    proxy_jump = []
    uuid = "550e8400-e29b-41d4-a716-446655440001"
    data = {
        "nickname": nickname,
        "hostname": hostname,
        "username": username,
        "port": port,
        "protocol": protocol,
        "uuid": uuid,
    }


class _Manager:
    def __init__(self):
        self.connections = [_Connection()]
        self._handlers = {}
        self._next_handler = 1

    def get_connections(self):
        return list(self.connections)

    def connect(self, name, callback):
        token = self._next_handler
        self._next_handler += 1
        self._handlers[token] = (name, callback)
        return token

    def disconnect(self, token):
        self._handlers.pop(token, None)


def _pump_until(gui, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gui.pump(10)
        if predicate():
            return True
    return False


def test_daemon_terminal_streams_without_blocking_gtk(gui, tmp_path):
    from sshpilot.api import DaemonClient, InProcessClient
    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.pty_runner import PtySessionProcessRunner
    from sshpilot.daemon.session_runtime import SessionRuntime
    from sshpilot.daemon_terminal_widget import DaemonTerminalWidget
    from sshpilot.gtk_client_bridge import GtkClientBridge

    script = (
        "import os,tty;tty.setraw(0);"
        "os.write(1,b'READY\\n');"
        "data=os.read(0,1);os.write(1,b'ECHO:'+data)"
    )
    runner = PtySessionProcessRunner(
        lambda _spec: (
            (sys.executable, "-u", "-c", script),
            {"PATH": os.environ.get("PATH", "")},
        )
    )
    manager = _Manager()
    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700)
    server = DaemonServer(
        lambda: InProcessClient(manager, client_name="sshpilotd"),
        socket_path=socket_path,
        session_runtime_factory=lambda core: SessionRuntime(
            core,
            runner=runner,
        ),
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=socket_path)
    bridge = GtkClientBridge()
    widget = DaemonTerminalWidget(
        client,
        bridge,
        client.list_connections()[0].id,
    )
    window = Gtk.Window()
    window.set_child(widget)
    window.present()
    widget.start()
    try:
        heartbeat = []
        _GLib.idle_add(lambda: heartbeat.append(True) and False)
        assert _pump_until(gui, lambda: widget.received_bytes >= len(b"READY\n"))
        assert heartbeat
        before = widget.received_bytes
        widget._on_commit(widget._terminal, "x", 1)
        assert _pump_until(gui, lambda: widget.received_bytes > before)
        widget._on_size_changed(widget._terminal, 80, 24)
    finally:
        widget.close()
        window.close()
        bridge.shutdown()
        client.close()
        server.shutdown()
        assert server.wait_stopped()
        runner.close()
