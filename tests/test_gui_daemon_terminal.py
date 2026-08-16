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
    id = "TerminalTest"
    data = {
        "nickname": nickname,
        "hostname": hostname,
        "username": username,
        "port": port,
        "protocol": protocol,
        "id": id,
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
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.api import DaemonClient
    from sshpilot.api.models import (
        InteractionState,
        InteractionType,
        PasswordPrompt,
    )
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
        lambda: ConnectionApplicationService(manager, client_name="sshpilotd"),
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
        assert widget._controller.tab_state.session_id is not None
        assert client.threads_alive()["reader"]
        interaction = server._interaction_broker.create(
            session_id=widget._controller.tab_state.session_id,
            connection_id=widget._controller.tab_state.connection_id,
            interaction_type=InteractionType.PASSWORD,
            prompt=PasswordPrompt(
                username="tester",
                hostname="terminal.test",
                port=22,
                attempt=1,
                can_remember=False,
                stored_secret_available=False,
            ),
        )
        assert _pump_until(
            gui,
            lambda: interaction.id
            in widget._interaction_dialogs._dialogs,
        )
        dialog = widget._interaction_dialogs._dialogs[interaction.id]
        content = dialog.get_extra_child()
        entry = content.get_first_child()
        remember = entry.get_next_sibling()
        entry.set_text("gui-one-use")
        widget._interaction_dialogs._secret_response(
            interaction,
            dialog,
            "submit",
            entry,
            remember,
        )
        assert _pump_until(
            gui,
            lambda: server._interaction_broker.get(
                interaction.id,
                client._client_id,
            ).state
            is InteractionState.ANSWERED,
        )
        result = server._interaction_broker.wait_for_result(interaction.id)
        assert result is not None
        assert bytes(result.secret or b"") == b"gui-one-use"
        result.clear()
        before = widget.received_bytes
        widget._on_commit(widget._terminal, "x", 1)
        assert _pump_until(gui, lambda: widget.received_bytes > before)
        widget._on_size_changed(widget._terminal)
    finally:
        widget.close()
        window.close()
        bridge.shutdown()
        client.close()
        server.shutdown()
        assert server.wait_stopped()
        runner.close()
