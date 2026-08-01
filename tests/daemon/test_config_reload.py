import copy
import json
import os
import threading
import time
from pathlib import Path

from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.events import EventType
from sshpilot.api.in_process_client import InProcessClient
from sshpilot.api.models.connections import CreateConnectionRequest
from sshpilot.config import Config
import sshpilot.connection_manager as connection_manager_module
from sshpilot.connection_manager import ConnectionManager
from sshpilot.daemon.config_reload import AuthoritativeConfigurationBackend
from sshpilot.daemon.server import DaemonCore, DaemonServer
from sshpilot.groups import GroupManager


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _FileConfig:
    def __init__(self, path: Path):
        self.config_file = str(path)
        self.config_data = {"config_version": 3}
        self._connection_uuid_by_nickname = {}
        self._connection_nickname_by_uuid = {}
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
        value = json.loads(Path(self.config_file).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("configuration root must be an object")
        return value

    def bind_connection_identities(self, connections):
        Config.bind_connection_identities(self, connections)


def _build_server(tmp_path, monkeypatch, ssh_text):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    root = ssh_dir / "config"
    root.write_text(ssh_text, encoding="utf-8")
    os.chmod(root, 0o600)
    config = _FileConfig(tmp_path / "config.json")
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
    manager = ConnectionManager(config)
    handlers = {}
    next_handler = 1

    def _connect(signal_name, callback):
        nonlocal next_handler
        handler_id = next_handler
        next_handler += 1
        handlers[handler_id] = (signal_name, callback)
        return handler_id

    def _disconnect(handler_id):
        handlers.pop(handler_id, None)

    def _emit(signal_name, connection):
        for registered_name, callback in tuple(handlers.values()):
            if registered_name == signal_name:
                callback(manager, connection)

    manager.connect = _connect
    manager.disconnect = _disconnect
    manager.emit = _emit
    groups = GroupManager(config, connection_manager=manager)
    core = InProcessClient(
        manager,
        group_manager=groups,
        client_name="reload-test",
        allow_cross_thread_commands=True,
    )
    backend = AuthoritativeConfigurationBackend(
        core,
        manager,
        groups,
        config,
    )
    server = DaemonServer(
        lambda: DaemonCore(core, backend),
        socket_path=tmp_path / "runtime" / "sshpilotd.sock",
        configuration_reload_debounce=0.02,
        configuration_poll_interval=0.02,
    )
    server.start_in_thread()
    return server, manager, root


def _connection_block(name, hostname="example.test"):
    return (
        f"Host {name}\n"
        f"    HostName {hostname}\n"
        "    User tester\n"
        "    Port 22\n"
    )


def test_external_add_is_forwarded(
    tmp_path,
    monkeypatch,
):
    server, _manager, root = _build_server(
        tmp_path,
        monkeypatch,
        _connection_block("Alpha"),
    )
    client = DaemonClient(socket_path=server.socket_path)
    events = []
    subscription = client.subscribe_events(events.append)
    try:
        root.write_text(
            root.read_text(encoding="utf-8") + "\n" + _connection_block("NL1"),
            encoding="utf-8",
        )
        assert _wait_until(
            lambda: any(item.nickname == "NL1" for item in client.list_connections())
        )
        nl1 = next(
            item for item in client.list_connections()
            if item.nickname == "NL1"
        )
        assert nl1.id == "NL1"
        assert _wait_until(
            lambda: any(
                event.type is EventType.CONNECTION_CREATED
                and event.payload.id == nl1.id
                for event in events
            )
        )
        time.sleep(0.08)
        matching = [
            event
            for event in events
            if event.type is EventType.CONNECTION_CREATED
            and event.payload.id == nl1.id
        ]
        assert len(matching) == 1
    finally:
        subscription.unsubscribe()
        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)
        assert not server.socket_path.exists()


def test_external_atomic_rename_updates_connection_id(
    tmp_path,
    monkeypatch,
):
    server, _manager, root = _build_server(
        tmp_path,
        monkeypatch,
        _connection_block("Alpha"),
    )
    client = DaemonClient(socket_path=server.socket_path)
    assert client.list_connections()
    events = []
    subscription = client.subscribe_events(events.append)
    try:
        text = root.read_text(encoding="utf-8").replace(
            "Host Alpha",
            "Host Beta",
        )
        replacement = root.with_suffix(".replacement")
        replacement.write_text(text, encoding="utf-8")
        os.chmod(replacement, 0o600)
        os.replace(replacement, root)
        assert _wait_until(
            lambda: client.list_connections()[0].nickname == "Beta"
        )
        renamed = client.list_connections()[0]
        assert renamed.id == "Beta"
        assert _wait_until(
            lambda: any(
                event.type in {EventType.CONNECTION_CREATED, EventType.CONNECTION_DELETED}
                for event in events
            )
        )
    finally:
        subscription.unsubscribe()
        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)


def test_wildcard_include_creation_rebuilds_watch_set(tmp_path, monkeypatch):
    include_dir = tmp_path / "ssh" / "conf.d"
    include_dir.mkdir(parents=True)
    server, _manager, _root = _build_server(
        tmp_path,
        monkeypatch,
        "Include conf.d/*\n" + _connection_block("Root"),
    )
    client = DaemonClient(socket_path=server.socket_path)
    try:
        included = include_dir / "work.conf"
        included.write_text(_connection_block("Included"), encoding="utf-8")
        os.chmod(included, 0o600)
        assert _wait_until(
            lambda: any(
                item.nickname == "Included"
                for item in client.list_connections()
            )
        )
        coordinator = server._configuration_reload
        assert coordinator is not None
        assert included in coordinator.backend.discover_paths()
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)


def test_malformed_edit_keeps_snapshot_and_later_change_recovers(
    tmp_path,
    monkeypatch,
):
    server, _manager, root = _build_server(
        tmp_path,
        monkeypatch,
        _connection_block("Alpha"),
    )
    client = DaemonClient(socket_path=server.socket_path)
    original = copy.deepcopy(client.list_connections())
    try:
        root.write_text('Host "unterminated\n', encoding="utf-8")
        time.sleep(0.12)
        assert client.list_connections() == original
        root.write_text(_connection_block("Recovered"), encoding="utf-8")
        assert _wait_until(
            lambda: client.list_connections()[0].nickname == "Recovered"
        )
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)


def test_shutdown_stops_watcher_and_coordinator(tmp_path, monkeypatch):
    server, _manager, _root = _build_server(
        tmp_path,
        monkeypatch,
        _connection_block("Alpha"),
    )
    coordinator = server._configuration_reload
    assert coordinator is not None
    watcher_thread = coordinator.watcher._thread
    reload_thread = coordinator._thread
    server.shutdown()
    assert server.wait_stopped(timeout=2)
    assert watcher_thread is not None and not watcher_thread.is_alive()
    assert reload_thread is not None and not reload_thread.is_alive()


def test_daemon_self_write_emits_no_duplicate_reload_event(
    tmp_path,
    monkeypatch,
):
    server, _manager, _root = _build_server(
        tmp_path,
        monkeypatch,
        _connection_block("Alpha"),
    )
    client = DaemonClient(socket_path=server.socket_path)
    events = []
    subscription = client.subscribe_events(events.append)
    try:
        created = client.create_connection(
            CreateConnectionRequest(
                nickname="CreatedByDaemon",
                hostname="created.example.test",
                username="tester",
            )
        )
        assert _wait_until(
            lambda: any(
                event.type is EventType.CONNECTION_CREATED
                and event.payload.id == created.id
                for event in events
            )
        )
        coordinator = server._configuration_reload
        assert coordinator is not None
        assert _wait_until(lambda: coordinator.reload_count >= 1)
        time.sleep(0.08)
        matching = [
            event
            for event in events
            if event.type is EventType.CONNECTION_CREATED
            and event.payload.id == created.id
        ]
        assert len(matching) == 1
    finally:
        subscription.unsubscribe()
        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)


def test_external_delete_reaches_multiple_clients(tmp_path, monkeypatch):
    server, _manager, root = _build_server(
        tmp_path,
        monkeypatch,
        _connection_block("Alpha") + "\n" + _connection_block("Beta"),
    )
    client_a = DaemonClient(socket_path=server.socket_path, client_id="reload:a")
    client_b = DaemonClient(socket_path=server.socket_path, client_id="reload:b")
    events_a = []
    events_b = []
    subscription_a = client_a.subscribe_events(events_a.append)
    subscription_b = client_b.subscribe_events(events_b.append)
    removed = next(
        item for item in client_a.list_connections()
        if item.nickname == "Beta"
    )
    assert any(item.nickname == "Alpha" for item in client_a.list_connections())
    try:
        root.write_text(
            "Host Alpha\n"
            "    HostName example.test\n"
            "    User tester\n"
            "    Port 22\n",
            encoding="utf-8",
        )
        assert _wait_until(lambda: len(client_a.list_connections()) == 1)
        assert client_a.list_connections() == client_b.list_connections()
        assert _wait_until(
            lambda: all(
                any(
                    event.type is EventType.CONNECTION_DELETED
                    and event.payload.id == removed.id
                    for event in events
                )
                for events in (events_a, events_b)
            )
        )
    finally:
        subscription_a.unsubscribe()
        subscription_b.unsubscribe()
        client_a.close()
        client_b.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)


def test_slow_reload_does_not_block_ipc_and_serializes_mutation(
    tmp_path,
    monkeypatch,
):
    server, _manager, root = _build_server(
        tmp_path,
        monkeypatch,
        _connection_block("Alpha"),
    )
    coordinator = server._configuration_reload
    assert coordinator is not None
    backend = coordinator.backend
    original_reload = backend.reload
    entered = threading.Event()
    release = threading.Event()

    def _blocked_reload():
        entered.set()
        assert release.wait(1)
        return original_reload()

    backend.reload = _blocked_reload
    client_a = DaemonClient(socket_path=server.socket_path, client_id="reload:a")
    try:
        root.touch()
        assert entered.wait(1)
        # The selector and immutable connection snapshot remain available.
        client_b = DaemonClient(
            socket_path=server.socket_path,
            client_id="reload:b",
        )
        try:
            assert client_b.list_connections()[0].nickname == "Alpha"
            assert client_b.list_sessions() == []

            done = threading.Event()
            errors = []

            def _create():
                try:
                    client_a.create_connection(
                        CreateConnectionRequest(
                            nickname="AfterReload",
                            hostname="after.example.test",
                            username="tester",
                        )
                    )
                except BaseException as error:
                    errors.append(error)
                finally:
                    done.set()

            mutation_thread = threading.Thread(target=_create)
            mutation_thread.start()
            assert not done.wait(0.05)
            assert client_b.list_connections()[0].nickname == "Alpha"
            release.set()
            assert done.wait(1)
            mutation_thread.join(1)
            assert errors == []
            assert _wait_until(
                lambda: any(
                    item.nickname == "AfterReload"
                    for item in client_b.list_connections()
                )
            )
        finally:
            client_b.close()
    finally:
        release.set()
        client_a.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)


def test_partial_json_write_preserves_snapshot_and_recovers(
    tmp_path,
    monkeypatch,
):
    server, _manager, _root = _build_server(
        tmp_path,
        monkeypatch,
        _connection_block("Alpha"),
    )
    client = DaemonClient(socket_path=server.socket_path)
    coordinator = server._configuration_reload
    assert coordinator is not None
    config_path = Path(coordinator.backend.config.config_file)
    original = client.list_connections()
    try:
        config_path.write_text('{"config_version":', encoding="utf-8")
        time.sleep(0.12)
        assert client.list_connections() == original
        config_path.write_text(
            json.dumps({"config_version": 3}),
            encoding="utf-8",
        )
        before = coordinator.reload_count
        assert _wait_until(lambda: coordinator.reload_count > before)
        assert client.list_connections() == original
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)


def test_change_after_reload_read_schedules_follow_up(tmp_path, monkeypatch):
    server, _manager, root = _build_server(
        tmp_path,
        monkeypatch,
        _connection_block("Alpha"),
    )
    client = DaemonClient(socket_path=server.socket_path)
    coordinator = server._configuration_reload
    assert coordinator is not None
    assert _wait_until(lambda: coordinator.reload_count >= 1)
    backend = coordinator.backend
    original_reload = backend.reload
    captured = threading.Event()
    release = threading.Event()

    def _pause_after_read():
        result = original_reload()
        captured.set()
        assert release.wait(1)
        return result

    backend.reload = _pause_after_read
    try:
        root.touch()
        assert captured.wait(1)
        root.write_text(
            root.read_text(encoding="utf-8")
            + "\n"
            + _connection_block("LateChange"),
            encoding="utf-8",
        )
        release.set()
        assert _wait_until(
            lambda: any(
                item.nickname == "LateChange"
                for item in client.list_connections()
            )
        )
    finally:
        release.set()
        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)


def test_create_connection_with_proxy_jump_persists_to_ssh_config(
    daemon_factory,
):
    server, manager = daemon_factory(start=True)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        created = client.create_connection(
            CreateConnectionRequest(
                nickname="JumpTestHost",
                hostname="target.example.test",
                username="tester",
                port=22,
                config_patch={"proxy_jump": ["jump.example.test"]},
            )
        )
        assert created.nickname == "JumpTestHost"
        assert created.proxy_jump == ("jump.example.test",)

        details = client.get_connection(created.id)
        assert details.proxy_jump == ("jump.example.test",)
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=2)

