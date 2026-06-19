"""Tests for the built-in Kubernetes exec protocol plugin."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext, ProtocolError
from sshpilot.plugins.builtin.kubernetes_protocol import Plugin, KubernetesProtocolBackend
from sshpilot.plugins.loader import load_plugins


class FakeConfig:
    def get_setting(self, key, default=None):
        return default


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def _ctx():
    return PluginContext(plugin_id="k8s", app_config=None, connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


def test_loader_discovers_and_activates_k8s():
    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert any(p.plugin_id == 'k8s' and p.builtin for p in loaded)
    assert registry_mod.protocol_registry().get_or_none('k8s') is not None


def test_fields_and_validate():
    backend = KubernetesProtocolBackend()
    assert backend.capabilities() == frozenset()
    by_key = {f.key: f for f in backend.connection_fields()}
    assert by_key['pod'].required
    assert backend.validate({'pod': 'web-0'}) == []
    assert any('pod' in e.lower() for e in backend.validate({}))


def test_build_spawn_basic(monkeypatch):
    import sshpilot.plugins.builtin.kubernetes_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/kubectl')
    conn = Connection({'nickname': 'k', 'protocol': 'k8s', 'pod': 'web-0'})
    spec = KubernetesProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/kubectl', 'exec', '-it', 'web-0', '--', 'sh']


def test_build_spawn_full(monkeypatch):
    import sshpilot.plugins.builtin.kubernetes_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/kubectl')
    conn = Connection({'nickname': 'k', 'protocol': 'k8s', 'pod': 'api-1',
                       'container': 'app', 'namespace': 'prod',
                       'kube_context': 'staging', 'command': 'bash'})
    spec = KubernetesProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/kubectl', '--context', 'staging', '-n', 'prod',
                         'exec', '-it', 'api-1', '-c', 'app', '--', 'bash']


def test_build_spawn_missing_binary(monkeypatch):
    import sshpilot.plugins.builtin.kubernetes_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    conn = Connection({'nickname': 'k', 'protocol': 'k8s', 'pod': 'web-0'})
    with pytest.raises(ProtocolError, match='not installed'):
        KubernetesProtocolBackend().build_spawn(conn, _ctx())


def test_build_spawn_missing_pod(monkeypatch):
    import sshpilot.plugins.builtin.kubernetes_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/kubectl')
    conn = Connection({'nickname': 'k', 'protocol': 'k8s'})
    with pytest.raises(ProtocolError, match='[Nn]o pod'):
        KubernetesProtocolBackend().build_spawn(conn, _ctx())


def test_activate_registers_backend():
    Plugin().activate(_ctx())
    assert registry_mod.protocol_registry().get('k8s') is not None
