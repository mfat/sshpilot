import pytest

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api import DaemonClient
from sshpilot.daemon import DaemonServer
from tests.helpers.fake_connection_repository import make_test_repository


@pytest.fixture
def fake_repo():
    return make_test_repository()


@pytest.fixture(params=("in-process", "daemon"))
def client_backend(request):
    """Run shared public contracts against both production client adapters."""

    return request.param


@pytest.fixture
def client_factory(client_backend, tmp_path):
    """Create either client over its real repository/transport boundary."""

    clients = []
    servers = []

    def _factory(repo=None, **kwargs):
        if repo is None:
            repo = make_test_repository()
        if client_backend == "in-process":
            client = ConnectionApplicationService(repo, **kwargs)
        else:
            socket_dir = tmp_path / f"daemon-{len(servers)}"
            socket_dir.mkdir(mode=0o700)
            socket_path = socket_dir / "sshpilotd.sock"

            def _core_factory():
                return ConnectionApplicationService(
                    repo,
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
def application_services_factory():
    clients = []

    def _factory(repo=None, **kwargs):
        if repo is None:
            repo = make_test_repository()
        client = ConnectionApplicationService(repo, **kwargs)
        clients.append(client)
        return client

    yield _factory
    for client in clients:
        client.close()
