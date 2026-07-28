"""Smoke test: the real app boots under the GUI harness and a window comes up."""

import threading
import time

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def test_app_boots_and_window_present(gui):
    assert gui.window is not None
    # The pinned Start tab means at least one page exists on a fresh window.
    assert gui.window.tab_view.get_n_pages() >= 1
    # No stray confirmation dialogs on a clean boot.
    assert gui.message_dialogs() == []


def test_open_local_tabs(gui):
    gui.open_local_tabs(2)
    assert len(gui.user_pages()) == 2


def test_real_window_composes_welcome_page_with_in_process_client(gui):
    from sshpilot.api import InProcessClient

    window = gui.window
    client = window.client
    welcome = window.welcome_view
    client_calls = []
    manager_calls = []
    original_client_list = client.list_connections
    original_manager_list = window.connection_manager.get_connections

    def list_through_client():
        client_calls.append(True)
        return []

    def direct_manager_read():
        manager_calls.append(True)
        raise AssertionError("WelcomePage bypassed SshPilotClient")

    client.list_connections = list_through_client
    window.connection_manager.get_connections = direct_manager_read
    try:
        welcome._populate_recent_box()
        gui.pump(50)
    finally:
        client.list_connections = original_client_list
        window.connection_manager.get_connections = original_manager_list

    assert isinstance(client, InProcessClient)
    assert welcome.client is client
    assert client_calls == [True]
    assert manager_calls == []


@pytest.mark.parametrize("_repeat", range(3))
def test_real_window_daemon_read_keeps_gtk_main_context_responsive(
    gui,
    tmp_path,
    _repeat,
):
    from sshpilot.api import DaemonClient, InProcessClient
    from sshpilot.api.client_factory import ClientMode, ClientSelection
    from sshpilot.daemon import DaemonServer
    from sshpilot.gtk_client_bridge import GtkClientBridge

    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    class _DelayedManager:
        def get_connections(self):
            entered.set()
            release.wait(2)
            completed.set()
            return []

    socket_dir = tmp_path / "daemon-gui"
    socket_dir.mkdir(mode=0o700)
    server = DaemonServer(
        lambda: InProcessClient(_DelayedManager(), client_name="gtk-test-daemon"),
        socket_path=socket_dir / "sshpilotd.sock",
    )
    server.start_in_thread()

    window = gui.window
    welcome = window.welcome_view
    app = gui.app
    old_bridge = getattr(app, "_api_client_bridge", None)
    bridge = GtkClientBridge()
    daemon_client = DaemonClient(socket_path=server.socket_path, timeout=2)
    calls = []
    original_list = daemon_client.list_connections

    def _recorded_list():
        calls.append(True)
        return original_list()

    daemon_client.list_connections = _recorded_list
    app._api_client_bridge = bridge
    window.client_bridge = bridge
    try:
        window._apply_client_selection(
            ClientSelection(client=daemon_client, mode=ClientMode.DAEMON)
        )
        assert entered.wait(1)

        main_context_tick = []
        gui.GLib.idle_add(lambda: main_context_tick.append(True) or False)
        gui.pump(100)

        assert main_context_tick == [True]
        assert completed.is_set() is False
        assert window.client is daemon_client
        assert welcome.client is daemon_client
        assert calls == [True]

        release.set()
        gui.pump(200)
        assert completed.is_set()
    finally:
        release.set()
        app.clear_api_event_subscription()
        daemon_client.close()
        bridge.shutdown()
        replacement = InProcessClient(
            window.connection_manager,
            group_manager=window.group_manager,
        )
        window.client = replacement
        window.client_bridge = None
        welcome._closed = False
        welcome.set_client(replacement)
        app._api_client_selection = ClientSelection(
            client=replacement,
            mode=ClientMode.IN_PROCESS,
        )
        app._api_client_bridge = old_bridge
        server.shutdown()
        assert server.wait_stopped()


@pytest.mark.parametrize("_repeat", range(3))
def test_real_window_refreshes_after_idle_daemon_connection_event(
    gui,
    tmp_path,
    _repeat,
):
    from types import SimpleNamespace

    from sshpilot.api import DaemonClient, InProcessClient
    from sshpilot.api.client_factory import ClientMode, ClientSelection
    from sshpilot.daemon import DaemonServer
    from sshpilot.gtk_client_bridge import GtkClientBridge

    class _EventManager:
        def __init__(self):
            self.connections = []
            self.handlers = {}
            self.next_handler = 1

        def get_connections(self):
            return list(self.connections)

        def connect(self, signal_name, callback):
            handler_id = self.next_handler
            self.next_handler += 1
            self.handlers[handler_id] = (signal_name, callback)
            return handler_id

        def disconnect(self, handler_id):
            self.handlers.pop(handler_id, None)

        def emit(self, signal_name, connection):
            for registered_name, callback in tuple(self.handlers.values()):
                if registered_name == signal_name:
                    callback(self, connection)

    def _label_texts(widget):
        texts = []
        if isinstance(widget, gui.Gtk.Label):
            texts.append(widget.get_text())
        child = widget.get_first_child()
        while child is not None:
            texts.extend(_label_texts(child))
            child = child.get_next_sibling()
        return texts

    def _wait_until(predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            gui.pump(10)
        return bool(predicate())

    manager = _EventManager()
    socket_dir = tmp_path / f"daemon-event-gui-{_repeat}"
    socket_dir.mkdir(mode=0o700)
    server = DaemonServer(
        lambda: InProcessClient(manager, client_name="gtk-event-daemon"),
        socket_path=socket_dir / "sshpilotd.sock",
    )
    server.start_in_thread()

    window = gui.window
    welcome = window.welcome_view
    app = gui.app
    old_bridge = getattr(app, "_api_client_bridge", None)
    old_get_meta = welcome.config.get_connection_meta
    bridge = GtkClientBridge()
    daemon_client = DaemonClient(socket_path=server.socket_path, timeout=2)
    calls = []
    original_list = daemon_client.list_connections

    def _recorded_list():
        calls.append(True)
        return original_list()

    daemon_client.list_connections = _recorded_list
    welcome.config.get_connection_meta = lambda _nickname: {"last_used": 1}
    app._api_client_bridge = bridge
    window.client_bridge = bridge
    try:
        window._apply_client_selection(
            ClientSelection(client=daemon_client, mode=ClientMode.DAEMON)
        )
        assert _wait_until(lambda: len(calls) >= 1)

        connection = SimpleNamespace(
            nickname=f"EventDemo{_repeat}",
            host=f"EventDemo{_repeat}",
            hostname="event.example.test",
            username="alice",
            port=22,
            protocol="ssh",
            aliases=[],
            auth_method=0,
            keyfile="",
            identity_files=[],
            certificate="",
            certificate_files=[],
            x11_forwarding=False,
            forwarding_rules=[],
            proxy_jump=[],
            password="must-not-cross-event",
        )
        manager.connections.append(connection)

        ticked = []
        manager.emit("connection-added", connection)
        gui.GLib.idle_add(lambda: ticked.append(True) or False)

        assert _wait_until(lambda: bool(ticked))
        assert _wait_until(lambda: len(calls) >= 2)
        assert _wait_until(
            lambda: connection.nickname in _label_texts(welcome._recent_box)
        )
    finally:
        app.clear_api_event_subscription()
        daemon_client.close()
        bridge.shutdown()
        replacement = InProcessClient(
            window.connection_manager,
            group_manager=window.group_manager,
        )
        window.client = replacement
        window.client_bridge = None
        welcome._closed = False
        welcome.config.get_connection_meta = old_get_meta
        welcome.set_client(replacement)
        app._api_client_selection = ClientSelection(
            client=replacement,
            mode=ClientMode.IN_PROCESS,
        )
        app._api_client_bridge = old_bridge
        server.shutdown()
        assert server.wait_stopped()
