"""Tests for the built-in Docker/Podman exec protocol plugin."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext, ProtocolError
from sshpilot.plugins.builtin.docker_protocol import Plugin, DockerProtocolBackend
from sshpilot.plugins.loader import load_plugins


class FakeConfig:
    def get_setting(self, key, default=None):
        return default


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def _ctx():
    return PluginContext(plugin_id="docker", app_config=None, connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


def test_loader_discovers_and_activates_docker():
    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert any(p.plugin_id == 'docker' and p.builtin for p in loaded)
    assert registry_mod.protocol_registry().get_or_none('docker') is not None


def test_fields_and_validate():
    backend = DockerProtocolBackend()
    assert backend.capabilities() == frozenset()
    by_key = {f.key: f for f in backend.connection_fields()}
    assert by_key['container'].required
    assert by_key['runtime'].default == 'docker'
    assert backend.validate({'container': 'web'}) == []
    assert any('container' in e.lower() for e in backend.validate({}))
    assert any('docker' in e.lower() for e in backend.validate({'container': 'w', 'runtime': 'nope'}))


def test_build_spawn_basic(monkeypatch):
    import sshpilot.plugins.builtin.docker_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/' + name)
    conn = Connection({'nickname': 'd', 'protocol': 'docker', 'container': 'web'})
    spec = DockerProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/docker', 'exec', '-it', 'web', 'sh']


def test_build_spawn_podman_host_and_command(monkeypatch):
    import sshpilot.plugins.builtin.docker_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/' + name)
    conn = Connection({'nickname': 'd', 'protocol': 'docker', 'container': 'api',
                       'runtime': 'podman', 'command': 'bash -l',
                       'docker_host': 'ssh://me@host'})
    spec = DockerProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/podman', '-H', 'ssh://me@host',
                         'exec', '-it', 'api', 'bash', '-l']


def test_build_spawn_missing_binary(monkeypatch):
    import sshpilot.plugins.builtin.docker_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    conn = Connection({'nickname': 'd', 'protocol': 'docker', 'container': 'web'})
    with pytest.raises(ProtocolError, match='not installed'):
        DockerProtocolBackend().build_spawn(conn, _ctx())


def test_build_spawn_missing_container(monkeypatch):
    import sshpilot.plugins.builtin.docker_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/docker')
    conn = Connection({'nickname': 'd', 'protocol': 'docker'})
    with pytest.raises(ProtocolError, match='[Nn]o container'):
        DockerProtocolBackend().build_spawn(conn, _ctx())


def test_activate_registers_backend():
    Plugin().activate(_ctx())
    assert registry_mod.protocol_registry().get('docker') is not None
