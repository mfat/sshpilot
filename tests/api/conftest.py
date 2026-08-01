from types import SimpleNamespace

import pytest

from sshpilot.api import DaemonClient, InProcessClient
from sshpilot.daemon import DaemonServer


class FakeConnection:
    def __init__(
        self,
        nickname="demo",
        hostname="example.test",
        username="alice",
        port=22,
        protocol="ssh",
    ):
        self.nickname = nickname
        self.id = nickname
        self.uuid = nickname
        self.host = nickname
        self.hostname = hostname
        self.username = username
        self.port = port
        self.protocol = protocol
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
            "port": port,
            "protocol": protocol,
            
        }
        # Deliberately present on the internal object. DTOs must omit them.
        self.password = "do-not-expose"
        self.key_passphrase = "do-not-expose-either"

    def update_data(self, data):
        self.data.update(data)
        for key, value in data.items():
            if not key.startswith("__"):
                setattr(self, key, value)
        self.host = self.nickname


class FakeConnectionManager:
    def __init__(self, connections=None):
        self.connections = list(connections or [])
        self.last_update = None
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
        connection = FakeConnection(
            nickname=data["nickname"],
            hostname=data["hostname"],
            username=data["username"],
            port=data["port"],
            protocol=data["protocol"],
        )
        self.connections.append(connection)
        self.emit("connection-added", connection)
        return connection

    def update_connection(self, connection, data, *, emit_signal=True):
        self.last_update = dict(data)
        connection.update_data(data)
        if emit_signal:
            self.emit("connection-updated", connection)
        return True

    def remove_connection(self, connection):
        self.connections.remove(connection)
        self.emit("connection-removed", connection)
        return True


@pytest.fixture
def fake_connection():
    return FakeConnection()


@pytest.fixture
def fake_manager(fake_connection):
    return FakeConnectionManager([fake_connection])


@pytest.fixture(params=("in-process", "daemon"))
def client_backend(request):
    """Run shared public contracts against both production client adapters."""

    return request.param


@pytest.fixture
def client_factory(client_backend, tmp_path):
    """Create either client over its real manager/transport boundary."""

    clients = []
    servers = []

    def _factory(manager, **kwargs):
        if client_backend == "in-process":
            client = InProcessClient(manager, **kwargs)
        else:
            socket_dir = tmp_path / f"daemon-{len(servers)}"
            socket_dir.mkdir(mode=0o700)
            socket_path = socket_dir / "sshpilotd.sock"
            group_manager = kwargs.pop("group_manager", None)

            def _core_factory():
                return InProcessClient(
                    manager,
                    group_manager=group_manager,
                    client_name="sshpilotd",
                )

            server = DaemonServer(_core_factory, socket_path=socket_path)
            server.start_in_thread()
            servers.append(server)
            client = DaemonClient(socket_path=socket_path, **kwargs)
        clients.append(client)
        return client

    yield _factory
    for client in clients:
        client.close()
    for server in servers:
        server.shutdown()
        assert server.wait_stopped()


@pytest.fixture
def in_process_client_factory():
    clients = []

    def _factory(manager, **kwargs):
        client = InProcessClient(manager, **kwargs)
        clients.append(client)
        return client

    yield _factory
    for client in clients:
        client.close()


@pytest.fixture
def group_manager():
    return SimpleNamespace(
        groups={"group-1": {"name": "Production"}},
        get_connection_groups=lambda nickname: ["group-1"] if nickname == "demo" else [],
    )
