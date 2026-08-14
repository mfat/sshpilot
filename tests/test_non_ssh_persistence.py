"""Plugin-protocol connections persist as JSON in the daemon's dedicated
``connections.json`` state file, never in ~/.ssh/config, and survive a fresh
repository reload. Passwords never enter the repository JSON — secret storage
belongs to the daemon secret provider.
"""

import json
import os
import sys
from pathlib import Path


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.daemon.connection_secret_provider import DaemonConnectionSecretProvider
from sshpilot.core.connections.models import ConnectionRecord

import sshpilot.secret_storage as ss
from sshpilot.secret_storage import SecretManager, password_spec

SSH_CONFIG = """Host alpha
    HostName alpha.example.com
    User root
"""


def _repo(tmp_path):
    root = tmp_path / 'ssh_config'
    if not root.exists():
        root.write_text(SSH_CONFIG)
    state = tmp_path / 'connections.json'
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / 'config.json',
        isolated=False,
    ), root, state


def _state(path: Path) -> dict:
    return json.loads(path.read_text())


def _telnet_data(nickname='lab-switch'):
    return {'nickname': nickname, 'protocol': 'telnet',
            'hostname': '10.0.0.5', 'port': 2323}


def test_create_persists_json_entry_and_leaves_ssh_config_alone(tmp_path):
    repo, root, state = _repo(tmp_path)
    before = root.read_bytes()

    repo.create_connection(_telnet_data())

    stored = _state(state)['non_ssh_connections']
    assert len(stored) == 1
    assert stored[0]['nickname'] == 'lab-switch'
    assert stored[0]['protocol'] == 'telnet'
    # The SSH config was not touched.
    assert root.read_bytes() == before


def test_fresh_repository_reload_survives(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data())

    fresh = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / 'config.json',
        isolated=False,
    )
    snap = fresh.snapshot()
    conn = next(c for c in snap.connections if c.id == 'lab-switch')
    assert conn.protocol == 'telnet'
    assert conn.hostname == '10.0.0.5'
    # The ssh_config host still loads alongside the JSON record.
    assert any(c.id == 'alpha' for c in snap.connections)


def test_rename_updates_json_entry(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data())

    # Freshly created non-SSH connections carry generation 1; the update
    # happens without a stale-editor generation guard (the daemon owns it).
    repo.update_connection(
        'lab-switch',
        _telnet_data(nickname='renamed-switch'),
    )

    names = [e['nickname'] for e in _state(state)['non_ssh_connections']]
    assert names == ['renamed-switch']
    snap = repo.snapshot()
    assert [c.id for c in snap.connections] == ['alpha', 'renamed-switch']


def test_delete_removes_json_entry(tmp_path):
    repo, root, state = _repo(tmp_path)
    repo.create_connection(_telnet_data())
    assert _state(state)['non_ssh_connections']

    repo.delete_connection('lab-switch')

    assert _state(state)['non_ssh_connections'] == []
    assert all(c.id != 'lab-switch' for c in repo.snapshot().connections)


def test_ssh_connections_never_enter_non_ssh_state(tmp_path):
    repo, root, state = _repo(tmp_path)
    # Create a real SSH connection through the repository.
    repo.create_connection(
        {'nickname': 'new', 'hostname': 'new.example.com', 'username': 'root',
         'protocol': 'ssh'}
    )
    stored = _state(state)
    assert stored['non_ssh_connections'] == []
    # The SSH config grew the new host; JSON only tracks non-SSH state.
    assert 'Host new' in root.read_text()


def test_password_stored_only_through_secret_provider(tmp_path):
    """Passwords must not be asserted through repository JSON: create a
    non-SSH connection, then route its password through the daemon secret
    provider (canonical host key) and confirm the JSON carries no secret."""
    repo, root, state = _repo(tmp_path)
    repo.create_connection(
        {'nickname': 'lab', 'protocol': 'telnet', 'hostname': '10.0.0.5',
         'username': 'admin'}
    )

    # The state file stores no password for the connection.
    raw = state.read_text()
    assert 'password' not in raw.lower()

    # The secret provider owns the credential: store by connection id under
    # the canonical host, and read it back the same way.
    class FakeBackend(ss.SecretBackend):
        def __init__(self):
            self.data = {}

        def is_available(self):
            return True

        def store(self, spec, secret):
            self.data[spec.keyring_account] = secret
            return True

        def lookup(self, spec):
            return self.data.get(spec.keyring_account)

        def delete(self, spec):
            return self.data.pop(spec.keyring_account, None) is not None

    backend = FakeBackend()
    manager = SecretManager()
    manager._backends = {'libsecret': backend, 'keyring': FakeBackend()}
    provider = DaemonConnectionSecretProvider(
        repo.get_record, secret_manager_factory=lambda: manager
    )

    assert provider.store_connection_password('lab', 'hunter2') is True
    assert provider.lookup_connection_password('lab') == 'hunter2'
    assert backend.data[password_spec('10.0.0.5', 'admin').keyring_account] == 'hunter2'
    # Still nothing in the repository JSON.
    assert 'hunter2' not in state.read_text()
    assert provider.delete_connection_password('lab') is True
    assert provider.lookup_connection_password('lab') is None


def _record_with(nickname, **over):
    data = dict({'nickname': nickname, 'protocol': 'telnet',
                 'hostname': '10.0.0.5'}, **over)
    return ConnectionRecord.from_dict(data)


def test_secret_provider_previous_identity_cleanup():
    """The daemon secret provider cleans up the previous identity's key on
    store; equivalent coverage of the retired ConnectionManager path."""
    class FakeBackend(ss.SecretBackend):
        def __init__(self):
            self.data = {}

        def is_available(self):
            return True

        def store(self, spec, secret):
            self.data[spec.keyring_account] = secret
            return True

        def lookup(self, spec):
            return self.data.get(spec.keyring_account)

        def delete(self, spec):
            return self.data.pop(spec.keyring_account, None) is not None

    backend = FakeBackend()
    manager = SecretManager()
    manager._backends = {'libsecret': backend, 'keyring': FakeBackend()}
    records = {'lab': _record_with('lab', hostname='10.0.0.5', username='root')}
    provider = DaemonConnectionSecretProvider(
        lambda cid: records.get(cid), secret_manager_factory=lambda: manager
    )
    # A legacy alias key exists from an earlier identity.
    backend.data[password_spec('lab', 'root').keyring_account] = 'legacy'

    assert provider.store_connection_password(
        'lab', 'new-pw', previous_hostname='old.example', previous_username='root'
    ) is True
    # Canonical host wins; the previous identity key is removed.
    assert backend.data[password_spec('10.0.0.5', 'root').keyring_account] == 'new-pw'
    assert password_spec('old.example', 'root').keyring_account not in backend.data
