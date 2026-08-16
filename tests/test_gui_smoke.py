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


def test_real_window_composes_welcome_page_with_application_services(gui):
    from sshpilot.core.connection_application_service import ConnectionApplicationService

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

    assert isinstance(client, ConnectionApplicationService)
    assert welcome.client is client
    assert client_calls == [True]
    assert manager_calls == []


def test_connection_selection_survives_rename_rebuild(gui):
    from sshpilot.connection_manager import Connection

    window = gui.window
    manager = window.connection_manager
    previous_connections = manager.connections
    previous_index = manager._connections_by_id
    connection = Connection(
        {
            "nickname": "BeforeRename",
            "hostname": "rename.example.test",
            "protocol": "ssh",
        }
    )
    manager.connections = [connection]
    manager._connections_by_id = {connection.id: connection}
    window.group_manager.bind_connections(manager.connections)
    try:
        window.rebuild_connection_list()
        original_row = window.connection_rows[connection][0]
        window.connection_list.select_row(original_row)

        connection.update_data(
            {
                **connection.data,
                "nickname": "AfterRename",
            }
        )
        window.rebuild_connection_list()

        selected = list(window.connection_list.get_selected_rows())
        assert len(selected) == 1
        assert selected[0].connection is connection
        assert selected[0].connection.id == connection.id
        assert selected[0].connection.nickname == "AfterRename"
    finally:
        manager.connections = previous_connections
        manager._connections_by_id = previous_index
        window.group_manager.bind_connections(previous_connections)
        window.rebuild_connection_list()


@pytest.mark.parametrize("_repeat", range(3))
def test_real_window_daemon_read_keeps_gtk_main_context_responsive(
    gui,
    tmp_path,
    _repeat,
):
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.api import DaemonClient
    from sshpilot.api.client_factory import ClientSelection
    from sshpilot.daemon import DaemonServer
    from sshpilot.gtk_client_bridge import GtkClientBridge

    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    class _DelayedManager:
        def get_connections(self):
            entered.set()
            # Wait until the test releases — never time out on our own, or the
            # intentional stall races the client transport timeout.
            release.wait(30)
            completed.set()
            return []

    socket_dir = tmp_path / "daemon-gui"
    socket_dir.mkdir(mode=0o700)
    server = DaemonServer(
        lambda: ConnectionApplicationService(_DelayedManager(), client_name="gtk-test-daemon"),
        socket_path=socket_dir / "sshpilotd.sock",
    )
    server.start_in_thread()

    window = gui.window
    welcome = window.welcome_view
    app = gui.app
    old_bridge = getattr(app, "_api_client_bridge", None)
    bridge = GtkClientBridge()
    daemon_client = DaemonClient(socket_path=server.socket_path, timeout=10)
    calls = []
    original_list = daemon_client.list_connections

    def _recorded_list():
        calls.append(True)
        return original_list()

    daemon_client.list_connections = _recorded_list
    app._api_client_bridge = bridge
    window.client_bridge = bridge
    previous_restore = window.config.get_setting(
        "terminal.daemon_restore_sessions", True
    )
    try:
        # Disable restore so it cannot enqueue a second control RPC behind the
        # intentionally blocked connections.list (single request gate).
        window.config.set_setting("terminal.daemon_restore_sessions", False)
        window._apply_client_selection(
            ClientSelection(client=daemon_client)
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
        assert daemon_client.server_instance_id
        alive = daemon_client.threads_alive()
        assert alive["reader"] and alive["event"] and alive["terminal"]

        release.set()
        gui.pump(200)
        assert completed.is_set()
    finally:
        release.set()
        try:
            window.config.set_setting(
                "terminal.daemon_restore_sessions", previous_restore
            )
        except Exception:
            pass
        app.clear_api_event_subscription()
        daemon_client.close()
        bridge.shutdown()
        replacement = ConnectionApplicationService(
            window.connection_manager,
            group_manager=window.group_manager,
        )
        window.client = replacement
        window.client_bridge = None
        welcome._closed = False
        welcome.set_client(replacement)
        app._api_client_selection = ClientSelection(
            client=replacement,
            mode=None,
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

    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.api import DaemonClient
    from sshpilot.api.client_factory import ClientSelection
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
        lambda: ConnectionApplicationService(manager, client_name="gtk-event-daemon"),
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
            ClientSelection(client=daemon_client)
        )
        assert _wait_until(lambda: len(calls) >= 1)

        connection = SimpleNamespace(
            id=f"EventDemo{_repeat}",
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
        replacement = ConnectionApplicationService(
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
            mode=None,
        )
        app._api_client_bridge = old_bridge
        server.shutdown()
        assert server.wait_stopped()


def test_real_window_refreshes_after_daemon_owned_external_config_reload(
    gui,
    tmp_path,
    monkeypatch,
):
    import json
    import os
    from pathlib import Path

    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.api import DaemonClient
    from sshpilot.api.client_factory import ClientSelection
    from sshpilot.config import Config
    import sshpilot.connection_manager as connection_manager_module
    from sshpilot.connection_manager import ConnectionManager
    from sshpilot.daemon.config_reload import AuthoritativeConfigurationBackend
    from sshpilot.daemon.server import CoreServices, DaemonServer
    from sshpilot.groups import GroupManager
    from sshpilot.gtk_client_bridge import GtkClientBridge

    class _FileConfig:
        def __init__(self, path):
            self.config_file = str(path)
            self.config_data = {"config_version": 3}
            
            
            self.save_json_config()

        def get_setting(self, key, default=None):
            current = self.config_data
            for part in key.split("."):
                if not isinstance(current, dict) or part not in current:
                    return default
                current = current[part]
            return current

        def set_setting(self, key, value):
            ConnectionManager._set_nested_setting(self.config_data, key, value)
            self.save_json_config()

        def save_json_config(self, data=None, *, raise_on_error=False):
            return Config.save_json_config(
                self,
                data,
                raise_on_error=raise_on_error,
            )

        def read_json_config_strict(self):
            return json.loads(
                Path(self.config_file).read_text(encoding="utf-8")
            )

        def bind_connection_identities(self, connections):
            Config.bind_connection_identities(self, connections)

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

    ssh_dir = tmp_path / "external-reload-ssh"
    ssh_dir.mkdir(mode=0o700)
    root = ssh_dir / "config"
    root.write_text(
        "Host Alpha\n    HostName alpha.example.test\n    User tester\n",
        encoding="utf-8",
    )
    os.chmod(root, 0o600)
    monkeypatch.setattr(
        connection_manager_module,
        "get_ssh_dir",
        lambda: str(ssh_dir),
    )
    monkeypatch.setattr(
        connection_manager_module,
        "get_config_dir",
        lambda: str(tmp_path),
    )
    config = _FileConfig(tmp_path / "daemon-config.json")
    manager = ConnectionManager(config)
    groups = GroupManager(config, connection_manager=manager)
    core = ConnectionApplicationService(
        manager,
        group_manager=groups,
        client_name="gtk-external-reload",
        allow_cross_thread_commands=True,
    )
    backend = AuthoritativeConfigurationBackend(core, manager, groups, config)
    socket_dir = tmp_path / "daemon-external-reload"
    socket_dir.mkdir(mode=0o700)
    server = DaemonServer(
        lambda: CoreServices(core, backend),
        socket_path=socket_dir / "sshpilotd.sock",
        configuration_reload_debounce=0.02,
        configuration_poll_interval=0.02,
    )
    server.start_in_thread()

    window = gui.window
    welcome = window.welcome_view
    app = gui.app
    old_bridge = getattr(app, "_api_client_bridge", None)
    old_get_meta = welcome.config.get_connection_meta
    bridge = GtkClientBridge()
    daemon_client = DaemonClient(socket_path=server.socket_path, timeout=2)
    welcome.config.get_connection_meta = lambda _nickname: {"last_used": 1}
    app._api_client_bridge = bridge
    window.client_bridge = bridge
    try:
        window._apply_client_selection(
            ClientSelection(client=daemon_client)
        )
        assert window.connection_manager.identity_migration_enabled is False
        root.write_text(
            root.read_text(encoding="utf-8")
            + "\nHost NL1\n    HostName nl1.example.test\n    User tester\n",
            encoding="utf-8",
        )
        ticked = []
        gui.GLib.idle_add(lambda: ticked.append(True) or False)
        assert _wait_until(lambda: bool(ticked))
        assert _wait_until(
            lambda: "NL1" in _label_texts(welcome._recent_box)
        )
        assert window.connection_manager.identity_migration_enabled is False
    finally:
        app.clear_api_event_subscription()
        daemon_client.close()
        bridge.shutdown()
        window.connection_manager.identity_migration_enabled = True
        replacement = ConnectionApplicationService(
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
            mode=None,
        )
        app._api_client_bridge = old_bridge
        server.shutdown()
        assert server.wait_stopped()


@pytest.mark.parametrize("_repeat", range(5))
def test_real_gtk_session_open_is_non_blocking_and_observes_events(
    gui,
    tmp_path,
    _repeat,
):
    from types import SimpleNamespace

    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.api import DaemonClient
    from sshpilot.api.client_factory import ClientSelection
    from sshpilot.api.models.sessions import SessionExitInfo, SessionState
    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.session_runtime import SessionRuntime
    from sshpilot.gtk_client_bridge import GtkClientBridge

    entered = threading.Event()
    release = threading.Event()

    class _Handle:
        def __init__(self, on_exit):
            self.on_exit = on_exit
            self.exit_info = None

        def terminate(self):
            if self.exit_info is None:
                self.exit_info = SessionExitInfo(exit_code=0, reason="terminated")
                self.on_exit(self.exit_info)

        def kill(self):
            self.terminate()

        def wait(self, _timeout):
            return self.exit_info

    class _Runner:
        def start(self, _spec, on_exit):
            entered.set()
            release.wait(2)
            return _Handle(on_exit)

        def close(self):
            return None

    class _Manager:
        def __init__(self):
            self.connection = SimpleNamespace(
                id=f"SessionDemo{_repeat}",
                nickname=f"SessionDemo{_repeat}",
                host=f"SessionDemo{_repeat}",
                hostname="session.example.test",
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
            )

        def get_connections(self):
            return [self.connection]

        def connect(self, _signal_name, _callback):
            return 1

        def disconnect(self, _handler_id):
            return None

    def _wait_until(predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            gui.pump(10)
        return bool(predicate())

    manager = _Manager()
    socket_dir = tmp_path / f"daemon-session-gui-{_repeat}"
    socket_dir.mkdir(mode=0o700)
    server = DaemonServer(
        lambda: ConnectionApplicationService(manager, client_name="gtk-session-daemon"),
        socket_path=socket_dir / "sshpilotd.sock",
        session_runtime_factory=lambda core: SessionRuntime(
            core,
            runner=_Runner(),
        ),
    )
    server.start_in_thread()

    window = gui.window
    welcome = window.welcome_view
    app = gui.app
    old_bridge = getattr(app, "_api_client_bridge", None)
    bridge = GtkClientBridge()
    daemon_client = DaemonClient(socket_path=server.socket_path, timeout=2)
    probe_client = DaemonClient(
        socket_path=server.socket_path,
        client_id=f"gtk-session-probe:{_repeat}",
        timeout=2,
    )
    completed = []
    observed = []
    old_is_quitting = window._is_quitting
    old_summary = getattr(app, "_last_api_session_summary", None)
    app._api_session_event_callback = observed.append
    app._api_client_bridge = bridge
    window.client_bridge = bridge
    try:
        window._apply_client_selection(
            ClientSelection(client=daemon_client)
        )
        connection_id = manager.connection.nickname
        if _repeat == 4:
            window._is_quitting = True
        app.open_daemon_session_for_diagnostics(
            connection_id,
            on_success=completed.append,
        )
        assert entered.wait(1)

        ticked = []
        gui.GLib.idle_add(lambda: ticked.append(True) or False)
        gui.pump(100)

        assert ticked == [True]
        assert probe_client.list_connections()
        assert probe_client.list_sessions()[0].state is SessionState.STARTING
        release.set()
        if _repeat == 4:
            assert completed == []
            assert _wait_until(
                lambda: probe_client.list_sessions()[0].state
                is SessionState.RUNNING
            )
            gui.pump(100)
            assert getattr(app, "_last_api_session_summary", None) is old_summary
        else:
            assert _wait_until(lambda: bool(completed))
            assert completed[0].state is SessionState.STARTING
            assert _wait_until(
                lambda: any(
                    getattr(event.payload, "state", None) is SessionState.RUNNING
                    for event in observed
                )
            )
            assert getattr(app, "_last_api_session_summary") == completed[0]
        assert window.client is daemon_client
    finally:
        release.set()
        window._is_quitting = old_is_quitting
        app.clear_api_event_subscription()
        app._api_session_event_callback = None
        probe_client.close()
        daemon_client.close()
        bridge.shutdown()
        replacement = ConnectionApplicationService(
            window.connection_manager,
            group_manager=window.group_manager,
        )
        window.client = replacement
        window.client_bridge = None
        welcome._closed = False
        welcome.set_client(replacement)
        app._api_client_selection = ClientSelection(
            client=replacement,
            mode=None,
        )
        app._api_client_bridge = old_bridge
        server.shutdown()
        assert server.wait_stopped()
