"""PluginContext secrets are namespaced per plugin id and ride the existing
ConnectionManager keyring path (no raw keyring access from plugins)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import ConnectionManager
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.registry import ProtocolRegistry


class FakeStoreManager:
    """Stands in for ConnectionManager: an in-memory (host, user) -> secret map
    matching the store_password/get_password/delete_password contract."""

    store_plugin_secret = ConnectionManager.store_plugin_secret
    get_plugin_secret = ConnectionManager.get_plugin_secret
    delete_plugin_secret = ConnectionManager.delete_plugin_secret
    _plugin_secret_host = staticmethod(ConnectionManager._plugin_secret_host)

    def __init__(self, available=True):
        self.secrets = {}
        self.available = available

    def store_password(self, host, username, password):
        if not self.available:
            return False
        self.secrets[(host, username)] = password
        return True

    def get_password(self, host, username):
        return self.secrets.get((host, username))

    def delete_password(self, host, username):
        return self.secrets.pop((host, username), None) is not None


def _ctx(manager):
    return PluginContext(app_config=None, connection_manager=manager,
                         protocol_registry=ProtocolRegistry())


def test_round_trip_and_namespacing():
    manager = FakeStoreManager()
    ctx = _ctx(manager)

    ctx.set_secret('vps', 'api_token', 's3cret')
    assert ctx.get_secret('vps', 'api_token') == 's3cret'
    # Stored under the reserved plugin namespace, not as an SSH password.
    assert manager.secrets == {('sshpilot-plugin/vps', 'api_token'): 's3cret'}

    # Another plugin cannot see it; same key under another id is separate.
    assert ctx.get_secret('other', 'api_token') is None
    ctx.set_secret('other', 'api_token', 'different')
    assert ctx.get_secret('vps', 'api_token') == 's3cret'

    assert ctx.delete_secret('vps', 'api_token') is True
    assert ctx.get_secret('vps', 'api_token') is None
    assert ctx.delete_secret('vps', 'api_token') is False


def test_invalid_arguments_rejected():
    ctx = _ctx(FakeStoreManager())
    with pytest.raises(ValueError):
        ctx.set_secret('', 'k', 'v')
    with pytest.raises(ValueError):
        ctx.set_secret('has/slash', 'k', 'v')
    with pytest.raises(ValueError):
        ctx.get_secret('vps', '')


def test_storage_unavailable_raises():
    ctx = _ctx(FakeStoreManager(available=False))
    with pytest.raises(RuntimeError):
        ctx.set_secret('vps', 'api_token', 's3cret')


def test_plugin_namespace_cannot_collide_with_ssh_hosts():
    # The reserved prefix differs from any plain hostname identifier the SSH
    # password path uses ('host' is a bare hostname/nickname there).
    assert ConnectionManager._plugin_secret_host('vps') == 'sshpilot-plugin/vps'
