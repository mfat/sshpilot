"""PluginContext secrets are namespaced per plugin id and ride the injected
secret surface (``store_plugin_secret`` / ``get_plugin_secret`` /
``delete_plugin_secret``), which the daemon secret provider implements as a
``sshpilot-plugin/<plugin_id>`` keyring namespace (no raw keyring access from
plugins).

Two surfaces are tested: the preferred scoped ``ctx.secrets`` (auto-scoped to
the plugin) and the legacy explicit-id ``ctx.get_secret/set_secret/delete_secret``
which now refuses any id other than the context's own plugin.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.daemon.connection_secret_provider import DaemonConnectionSecretProvider
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.registry import ProtocolRegistry


class FakeSecretManager:
    """Stands in for the daemon secret provider's injected secret surface: an
    in-memory (host, key) -> value map keyed under the reserved per-plugin
    namespace, mirroring ``DaemonConnectionSecretProvider.store_plugin_secret``."""

    @staticmethod
    def _plugin_secret_host(plugin_id):
        return DaemonConnectionSecretProvider._plugin_secret_host(plugin_id)

    def __init__(self, available=True):
        self.secrets = {}
        self.available = available

    def store_plugin_secret(self, plugin_id, key, value):
        if not self.available:
            return False
        self.secrets[(self._plugin_secret_host(plugin_id), key)] = value
        return True

    def get_plugin_secret(self, plugin_id, key):
        return self.secrets.get((self._plugin_secret_host(plugin_id), key))

    def delete_plugin_secret(self, plugin_id, key):
        return self.secrets.pop((self._plugin_secret_host(plugin_id), key), None) is not None


def _ctx(manager, plugin_id="test-plugin"):
    return PluginContext(plugin_id=plugin_id, app_config=None,
                         connection_manager=manager,
                         protocol_registry=ProtocolRegistry())


def test_scoped_store_round_trip_and_namespacing():
    manager = FakeSecretManager()
    ctx = _ctx(manager)

    ctx.secrets.set('api_token', 's3cret')
    assert ctx.secrets.get('api_token') == 's3cret'
    # Stored under the reserved per-plugin namespace, not as an SSH password.
    assert manager.secrets == {('sshpilot-plugin/test-plugin', 'api_token'): 's3cret'}

    # A different plugin's scoped store cannot see it.
    other = _ctx(manager, plugin_id="other-plugin")
    assert other.secrets.get('api_token') is None
    other.secrets.set('api_token', 'different')
    assert ctx.secrets.get('api_token') == 's3cret'

    assert ctx.secrets.delete('api_token') is True
    assert ctx.secrets.get('api_token') is None
    assert ctx.secrets.delete('api_token') is False


def test_legacy_api_allows_only_own_id():
    manager = FakeSecretManager()
    ctx = _ctx(manager, plugin_id="vps-tool")

    # Same id (the plugin's own) works exactly like the scoped store.
    ctx.set_secret('vps-tool', 'api_token', 's3cret')
    assert ctx.get_secret('vps-tool', 'api_token') == 's3cret'
    assert manager.secrets == {('sshpilot-plugin/vps-tool', 'api_token'): 's3cret'}
    assert ctx.delete_secret('vps-tool', 'api_token') is True


def test_legacy_api_rejects_foreign_id():
    ctx = _ctx(FakeSecretManager(), plugin_id="vps-tool")
    with pytest.raises(ValueError):
        ctx.get_secret('someone-else', 'api_token')
    with pytest.raises(ValueError):
        ctx.set_secret('someone-else', 'api_token', 'v')
    with pytest.raises(ValueError):
        ctx.delete_secret('someone-else', 'api_token')


def test_invalid_arguments_rejected():
    ctx = _ctx(FakeSecretManager(), plugin_id="test-plugin")
    with pytest.raises(ValueError):
        ctx.set_secret('', 'k', 'v')
    with pytest.raises(ValueError):
        ctx.set_secret('has/slash', 'k', 'v')
    with pytest.raises(ValueError):
        ctx.get_secret('test-plugin', '')


def test_storage_unavailable_raises():
    ctx = _ctx(FakeSecretManager(available=False))
    with pytest.raises(RuntimeError):
        ctx.secrets.set('api_token', 's3cret')


def test_plugin_namespace_cannot_collide_with_ssh_hosts():
    # The reserved prefix differs from any plain hostname identifier the SSH
    # password path uses ('host' is a bare hostname/nickname there). The
    # daemon secret provider owns this namespace mapping.
    assert DaemonConnectionSecretProvider._plugin_secret_host('vps') == 'sshpilot-plugin/vps'
