from types import SimpleNamespace

import pytest

from sshpilot.api import InProcessClient


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
        # Deliberately present on the internal object. DTOs must omit them.
        self.password = "do-not-expose"
        self.key_passphrase = "do-not-expose-either"


class FakeConnectionManager:
    def __init__(self, connections=None):
        self.connections = list(connections or [])
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


@pytest.fixture
def fake_connection():
    return FakeConnection()


@pytest.fixture
def fake_manager(fake_connection):
    return FakeConnectionManager([fake_connection])


@pytest.fixture
def client_factory():
    """Reusable contract factory; add DaemonClient here in a later phase."""

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

