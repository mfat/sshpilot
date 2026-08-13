from pathlib import Path

import pytest

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.models import ConnectionRecord
from sshpilot.core.errors import CoreError, ErrorCode
from sshpilot.api.models.connection_store import ConnectionStoreSnapshot
from sshpilot.api.models.connections import ConnectionHealth, ConnectionSummary
from sshpilot.api.models.common import ConnectionId
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.session_runtime import SessionRuntime


@pytest.fixture(autouse=True)
def _isolate_daemon_xdg(tmp_path_factory, monkeypatch):
    """Ensure every daemon test uses an isolated runtime root by default.

    Overrides XDG_* so resolve_socket_path(None) never hits the user's live
    daemon socket under the real ``$XDG_RUNTIME_DIR``.
    """
    root = tmp_path_factory.mktemp("daemon-xdg")
    runtime = root / "runtime"
    state = root / "state"
    cache = root / "cache"
    config = root / "config"
    for path in (runtime, state, cache, config):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("HOME", str(root / "home"))
    (root / "home").mkdir(exist_ok=True)
    monkeypatch.delenv("SSHPILOT_DAEMON_SOCKET", raising=False)
    yield root


class TestConnection:
    def __init__(self, nickname="demo", hostname="example.test", username="alice"):
        self.nickname = nickname
        self.id = nickname
        self.uuid = nickname
        self.host = nickname
        self.hostname = hostname
        self.username = username
        self.port = 22
        self.protocol = "ssh"
        self.generation = 0
        self.aliases = []
        self.auth_method = 0
        self.keyfile = ""
        self.identity_files = []
        self.certificate = ""
        self.certificate_files = []
        self.x11_forwarding = False
        self.forwarding_rules = []
        self.proxy_jump = []
        self.data = {
            "nickname": nickname,
            "hostname": hostname,
            "username": username,
            "port": 22,
            "protocol": "ssh",
            
        }
        self.password = "must-not-cross-wire"

    def update_data(self, data):
        self.data.update(data)
        for key, value in data.items():
            if not key.startswith("__"):
                setattr(self, key, value)
        self.host = self.nickname


class TestConnectionManager:
    def __init__(self):
        self.connections = [TestConnection()]
        self.plugin_secrets = {}
        self._handlers = {}
        self._next_handler = 1
        self._listeners = []
        self._generation = 0

    def _snapshot(self):
        return ConnectionStoreSnapshot(
            generation=self._generation,
            connections=tuple(
                ConnectionSummary(
                    id=ConnectionId(connection.id),
                    nickname=connection.nickname,
                    host=connection.host,
                    hostname=connection.hostname,
                    username=connection.username,
                    port=connection.port,
                    protocol=connection.protocol,
                    health=ConnectionHealth.UNKNOWN,
                )
                for connection in self.connections
            ),
            groups=(),
            root_connection_ids=tuple(
                ConnectionId(connection.id) for connection in self.connections
            ),
            metadata=(),
        )

    def _changed(self, before):
        self._generation += 1
        after = self._snapshot()
        for listener in tuple(self._listeners):
            listener(type("RepositoryChange", (), {"before": before, "after": after})())

    def add_listener(self, callback):
        self._listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def snapshot(self):
        return self._snapshot()

    def list_records(self):
        return tuple(
            ConnectionRecord.from_dict(connection.data, connection_id=connection.id)
            for connection in self.connections
        )

    def get_record(self, connection_id):
        return self.find_connection_by_nickname(str(connection_id))

    def get_editor_record(self, connection_id):
        return self.get_record(connection_id)

    def discover_paths(self):
        return frozenset()

    def reload(self):
        return self._snapshot()

    def get_connections(self):
        return list(self.connections)

    def connect(self, signal_name, callback):
        handler_id = self._next_handler
        self._next_handler += 1
        self._handlers[handler_id] = (signal_name, callback)
        return handler_id

    def disconnect(self, handler_id):
        self._handlers.pop(handler_id, None)

    def emit(self, signal_name, connection):
        for registered_name, callback in tuple(self._handlers.values()):
            if registered_name == signal_name:
                callback(self, connection)

    def find_connection_by_nickname(self, nickname):
        return next(
            (
                connection
                for connection in self.connections
                if connection.nickname == nickname
            ),
            None,
        )

    def create_connection(self, data):
        before = self._snapshot()
        connection = TestConnection(
            nickname=data["nickname"],
            hostname=data["hostname"],
            username=data["username"],
        )
        connection.port = data["port"]
        connection.update_data(data)
        self.connections.append(connection)
        self.emit("connection-added", connection)
        self._changed(before)
        return connection

    def update_connection(
        self, connection, data, *, expected_generation=None, emit_signal=True
    ):
        if isinstance(connection, str):
            connection = self.get_record(connection)
        if connection is None:
            raise KeyError("connection")
        if (
            expected_generation is not None
            and expected_generation != connection.generation
        ):
            raise CoreError(
                ErrorCode.STALE_CONNECTION_STATE,
                "The connection has been modified since it was last read",
            )
        before = self._snapshot()
        connection.update_data(data)
        connection.generation += 1
        if emit_signal:
            self.emit("connection-updated", connection)
        self._changed(before)
        return connection

    def duplicate_connection(self, connection_id):
        source = self.get_record(connection_id)
        if source is None:
            raise KeyError(connection_id)
        data = dict(source.data)
        data["nickname"] = f"{source.nickname} copy"
        return self.create_connection(data)

    def remove_connection(self, connection):
        before = self._snapshot()
        self.connections.remove(connection)
        self.emit("connection-removed", connection)
        self._changed(before)
        return True

    def delete_connection(self, connection_id):
        connection = self.get_record(connection_id)
        if connection is None:
            raise KeyError(connection_id)
        self.remove_connection(connection)

    def split_connection(self, connection_id, original_host_token, data, *, expected_generation=None):
        return self.create_connection(data)

    def lookup_connection_password(self, connection_id):
        getter = getattr(self, "get_connection_password", None)
        if callable(getter):
            connection = self.get_record(connection_id)
            return getter(connection) if connection is not None else None
        return self.plugin_secrets.get(("connection", str(connection_id)))

    def store_connection_password(self, connection_id, password, **_kwargs):
        self.plugin_secrets[("connection", str(connection_id))] = password
        return True

    def delete_connection_password(self, connection_id, **_kwargs):
        return self.plugin_secrets.pop(("connection", str(connection_id)), None) is not None

    def lookup_key_passphrase(self, key_path):
        return self.plugin_secrets.get(("key-passphrase", key_path))

    def has_key_passphrase(self, key_path):
        return self.lookup_key_passphrase(key_path) is not None

    def store_key_passphrase(self, key_path, passphrase):
        self.plugin_secrets[("key-passphrase", key_path)] = passphrase
        return True

    def delete_key_passphrase(self, key_path):
        return self.plugin_secrets.pop(("key-passphrase", key_path), None) is not None

    def store_plugin_secret(self, plugin_id, key, value):
        self.plugin_secrets[(plugin_id, key)] = value
        return True

    def get_plugin_secret(self, plugin_id, key):
        return self.plugin_secrets.get((plugin_id, key))

    def delete_plugin_secret(self, plugin_id, key):
        return self.plugin_secrets.pop((plugin_id, key), None) is not None

    def prepare_terminal_launch(
        self, connection_id, *, interaction_policy="none", remote_command=None, force_tty=False
    ):
        connection = self.get_record(connection_id)
        if connection is None:
            return None
        if getattr(connection, "protocol", "ssh") != "ssh":
            from sshpilot.plugins.registry import protocol_registry

            registry = protocol_registry()
            try:
                backend = registry.get(connection.protocol)
            except ValueError:
                return None
            if backend is None:
                return None
            plugin_id = registry.plugin_id_for(connection.protocol) or connection.protocol
            ctx = type("SpawnContext", (), {"plugin_id": plugin_id})()
            spec = backend.build_spawn(connection, ctx)
            import shutil

            argv = tuple(spec.argv or ())
            env = dict(spec.env or {})
            executable = shutil.which(argv[0], path=env.get("PATH"))
            if executable:
                return (executable, *argv[1:]), env
            return argv, env
        loop = None
        try:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except Exception:
            pass
        if loop is not None and hasattr(connection, "native_connect"):
            try:
                loop.run_until_complete(connection.native_connect())
            finally:
                if loop is not asyncio.get_event_loop():
                    loop.close()
        cmd = getattr(connection, "ssh_connection_cmd", None)
        if cmd is None:
            return None
        import shutil

        preload = getattr(connection, "_preload_keys_into_agent", None)
        if callable(preload):
            preload()
        argv = list(cmd.command)
        env = dict(cmd.env)
        local_command = getattr(connection, "local_command", None)
        if local_command and len(argv) >= 2:
            argv = [
                *argv[:-1],
                "-o",
                "PermitLocalCommand=yes",
                "-o",
                f"LocalCommand={local_command}",
                argv[-1],
            ]
        executable = shutil.which(argv[0], path=env.get("PATH"))
        if executable:
            argv[0] = executable
        if remote_command:
            argv.append(remote_command)
        return tuple(argv), env


@pytest.fixture
def daemon_factory(tmp_path):
    servers = []

    def _factory(
        *,
        manager=None,
        socket_path=None,
        start=True,
        client_event_queue_limit=256,
        max_client_outbound_bytes=4 * 1024 * 1024,
        max_client_terminal_bytes=1024 * 1024,
        session_runner=None,
        session_runtime_kwargs=None,
        session_command_workers=4,
        session_command_queue_limit=64,
        session_shutdown_timeout=3.0,
        idle_shutdown_seconds=0.0,
        service_mode=False,
        packaged=False,
        drain_timeout_seconds=5.0,
    ):
        manager = manager or TestConnectionManager()
        path = Path(socket_path or tmp_path / f"daemon-{len(servers)}" / "sshpilotd.sock")
        path.parent.mkdir(mode=0o700, exist_ok=True)
        server = DaemonServer(
            lambda: ConnectionApplicationService(
                manager,
                secret_provider=manager,
                client_name="sshpilotd",
            ),
            socket_path=path,
            client_event_queue_limit=client_event_queue_limit,
            max_client_outbound_bytes=max_client_outbound_bytes,
            max_client_terminal_bytes=max_client_terminal_bytes,
            session_runtime_factory=(
                (
                    lambda core: SessionRuntime(
                        core,
                        runner=session_runner,
                        **(session_runtime_kwargs or {}),
                    )
                )
                if session_runner is not None or session_runtime_kwargs
                else None
            ),
            session_command_workers=session_command_workers,
            session_command_queue_limit=session_command_queue_limit,
            session_shutdown_timeout=session_shutdown_timeout,
            idle_shutdown_seconds=idle_shutdown_seconds,
            service_mode=service_mode,
            packaged=packaged,
            drain_timeout_seconds=drain_timeout_seconds,
        )
        servers.append(server)
        if start:
            server.start_in_thread()
        return server, manager

    yield _factory
    for server in servers:
        server.shutdown()
        server.wait_stopped()
