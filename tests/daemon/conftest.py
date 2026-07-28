from pathlib import Path

import pytest

from sshpilot.api import InProcessClient
from sshpilot.daemon import DaemonServer


class TestConnection:
    def __init__(self, nickname="demo", hostname="example.test", username="alice"):
        self.nickname = nickname
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
        self.password = "must-not-cross-wire"


class TestConnectionManager:
    def __init__(self):
        self.connections = [TestConnection()]

    def get_connections(self):
        return list(self.connections)


@pytest.fixture
def daemon_factory(tmp_path):
    servers = []

    def _factory(
        *,
        manager=None,
        socket_path=None,
        start=True,
    ):
        manager = manager or TestConnectionManager()
        path = Path(socket_path or tmp_path / f"daemon-{len(servers)}" / "sshpilotd.sock")
        path.parent.mkdir(mode=0o700, exist_ok=True)
        server = DaemonServer(
            lambda: InProcessClient(manager, client_name="sshpilotd"),
            socket_path=path,
        )
        servers.append(server)
        if start:
            server.start_in_thread()
        return server, manager

    yield _factory
    for server in servers:
        server.shutdown()
        server.wait_stopped()
