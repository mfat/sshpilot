from pathlib import Path

import pytest

from sshpilot.api import InProcessClient
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
        self._handlers = {}
        self._next_handler = 1

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
        connection = TestConnection(
            nickname=data["nickname"],
            hostname=data["hostname"],
            username=data["username"],
        )
        connection.port = data["port"]
        connection.update_data(data)
        self.connections.append(connection)
        self.emit("connection-added", connection)
        return connection

    def update_connection(self, connection, data, *, emit_signal=True):
        connection.update_data(data)
        if emit_signal:
            self.emit("connection-updated", connection)
        return True

    def remove_connection(self, connection):
        self.connections.remove(connection)
        self.emit("connection-removed", connection)
        return True


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
            lambda: InProcessClient(manager, client_name="sshpilotd"),
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
