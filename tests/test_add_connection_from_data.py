"""Plugin-requested connection creation through the daemon-backed facade.

The local ``ConnectionManager.add_connection_from_data`` plugin path was
removed; plugin creation now routes through ``PluginContext.add_connection``
→ ``DaemonConnectionServices.add_connection_from_data`` → the daemon
connection repository.  These tests pin that boundary: creation, duplicate
rejection, nickname validation, stable ``ConnectionInfo`` DTOs, and the rule
that secrets never enter persisted connection data.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.api.models import ConnectionMutationResult, ConnectionSummary
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.errors import CoreError, ErrorCode
from sshpilot.gtk.daemon_connection_services import DaemonConnectionServices
from sshpilot.plugins.api import ConnectionInfo, PluginContext
from sshpilot.plugins.registry import ProtocolRegistry


class Projection:
    """Read-only projection backed by the repository snapshot (the shape the
    real presentation store exposes)."""

    def __init__(self, repo):
        self._repo = repo

    @property
    def connections(self):
        return self._repo.snapshot().connections

    def get_connections(self):
        return list(self.connections)

    def find_connection_by_nickname(self, nickname):
        return next((c for c in self.connections if c.nickname == nickname), None)

    def get_connection_by_id(self, connection_id):
        return next((c for c in self.connections if c.id == connection_id), None)

    get_connection_by_uuid = get_connection_by_id


class RepoClient:
    """Daemon-shaped fake client: routes plugin mutations straight into a real
    ``ConnectionRepository`` (the same split the daemon process owns), and
    records password stores separately."""

    def __init__(self, repo):
        self._repo = repo
        self.stored_passwords = []

    def create_connection(self, request):
        data = {
            "nickname": request.nickname,
            "hostname": request.hostname,
            "username": request.username,
            "port": request.port,
            "protocol": request.protocol,
        }
        if request.protocol == "ssh":
            data.update(request.config_patch)
        else:
            data.update(request.plugin_data)
        record = self._repo.create_connection(data)
        return ConnectionMutationResult(record.id, record.nickname, record.generation)

    def get_connection(self, connection_id):
        record = self._repo.get_record(connection_id)
        if record is None:
            raise CoreError(ErrorCode.CONNECTION_NOT_FOUND, "The connection does not exist")
        return ConnectionSummary(
            id=record.id,
            nickname=record.nickname,
            host=record.host,
            hostname=record.hostname,
            username=record.username,
            port=record.port,
            protocol=record.protocol,
        )

    def update_connection(self, connection_id, request):
        data = {"nickname": request.nickname, "protocol": "ssh"}
        if request.hostname is not None:
            data["hostname"] = request.hostname
        if request.username is not None:
            data["username"] = request.username
        if request.port is not None:
            data["port"] = request.port
        data.update(dict(request.config_patch or {}))
        record = self._repo.update_connection(
            connection_id, data, expected_generation=request.expected_generation
        )
        return ConnectionMutationResult(record.id, record.nickname, record.generation)

    def store_connection_password(self, request):
        self.stored_passwords.append((request.connection_id, request.password))
        return True


@pytest.fixture
def env(tmp_path):
    root = tmp_path / 'ssh_config'
    root.write_text("# empty\n")
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / 'connections.json',
        legacy_config_path=tmp_path / 'config.json',
        isolated=False,
    )
    facade = DaemonConnectionServices(Projection(repo))
    client = RepoClient(repo)
    facade.attach_client(client)
    ctx = PluginContext(plugin_id="mock-vps", app_config=None,
                        connection_manager=facade,
                        protocol_registry=ProtocolRegistry())
    return ctx, repo, client, root


def test_plugin_creates_ssh_connection(env):
    ctx, repo, _client, root = env
    info = ctx.add_connection({
        'nickname': 'newhost',
        'hostname': 'new.example.com',
        'username': 'root',
        'port': 2222,
    })
    assert isinstance(info, ConnectionInfo)
    assert info.nickname == 'newhost'
    assert info.protocol == 'ssh'

    snap = repo.snapshot()
    assert any(c.ssh_alias == 'newhost' for c in snap.connections)
    content = root.read_text()
    assert 'Host newhost' in content
    assert 'new.example.com' in content


def test_plugin_password_routed_to_secret_client(env):
    ctx, repo, client, root = env
    ctx.add_connection({
        'nickname': 'pwhost',
        'hostname': 'pw.example.com',
        'username': 'admin',
        'password': 'hunter2',
    })
    assert client.stored_passwords == [('pwhost', 'hunter2')]
    # The secret never reaches the persisted SSH config or the state file.
    assert 'hunter2' not in root.read_text()
    record = repo.get_record('pwhost')
    assert 'password' not in (record.data or {})


def test_duplicate_nickname_rejected_and_list_unpolluted(env):
    ctx, repo, _client, _root = env
    ctx.add_connection({'nickname': 'dup', 'hostname': 'a.example.com'})
    before = repo.snapshot().connections
    with pytest.raises(CoreError) as exc:
        ctx.add_connection({'nickname': 'dup', 'hostname': 'b.example.com'})
    assert exc.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert repo.snapshot().connections == before


def test_missing_nickname_rejected(env):
    ctx, repo, _client, _root = env
    # The daemon facade's CreateConnectionRequest rejects an empty nickname
    # before anything reaches the repository.
    with pytest.raises(ValueError):
        ctx.add_connection({'hostname': 'nobody.example.com'})
    assert repo.snapshot().connections == ()


def test_plugin_context_returns_stable_dto(env):
    ctx, repo, _client, _root = env
    info = ctx.add_connection({'nickname': 'viactx', 'hostname': 'ctx.example.com'})
    # add_connection returns a stable ConnectionInfo snapshot, not an internal
    # mutable record.
    assert isinstance(info, ConnectionInfo)
    assert info.nickname == 'viactx'
    assert info.host == 'ctx.example.com'
    assert repo.snapshot().connections[0].nickname == 'viactx'


def test_secrets_stripped_from_non_ssh_plugin_data(env):
    ctx, repo, _client, _root = env
    ctx.add_connection({
        'nickname': 'serial-demo',
        'hostname': '10.0.0.5',
        'protocol': 'serial',
        'baudrate': 115200,
        'api_token': 'must-not-be-persisted',
    })
    record = repo.get_record('serial-demo')
    assert record is not None and record.protocol == 'serial'
    data = record.data or {}
    assert data.get('baudrate') == 115200
    assert 'api_token' not in data
    assert 'password' not in data
