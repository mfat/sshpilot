"""Tests for the built-in Docker/Podman exec protocol plugin."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.api.models.sessions import PluginSessionFailureCode
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.builtin._session_failure import BuiltinProtocolError
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


def test_fields_and_validate(monkeypatch):
    import sshpilot.plugins.builtin.docker_protocol as mod

    monkeypatch.setattr(mod, "_", lambda msgid: f"translated:{msgid}")
    backend = DockerProtocolBackend()
    assert backend.capabilities() == frozenset()
    by_key = {f.key: f for f in backend.connection_fields()}
    assert by_key['container'].required
    assert by_key['container'].placeholder == 'translated:name or id'
    assert by_key['user'].placeholder == 'translated:user or UID'
    assert by_key['runtime'].default == 'docker'
    assert [value for value, _label in by_key['runtime'].choices] == [
        'docker', 'podman'
    ]
    assert backend.validate({'container': 'web'}) == []
    assert backend.validate({}) == [
        'translated:A container name or id is required.'
    ]
    assert backend.validate({'container': 'w', 'runtime': 'nope'}) == [
        'translated:Runtime must be docker or podman.'
    ]


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


def test_build_spawn_user_and_workdir(monkeypatch):
    import sshpilot.plugins.builtin.docker_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/' + name)
    conn = Connection({'nickname': 'd', 'protocol': 'docker', 'container': 'web',
                       'user': 'app', 'workdir': '/srv'})
    spec = DockerProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/docker', 'exec', '-it', '-u', 'app',
                         '-w', '/srv', 'web', 'sh']


def test_build_spawn_missing_binary(monkeypatch):
    import sshpilot.plugins.builtin.docker_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    conn = Connection({'nickname': 'd', 'protocol': 'docker', 'container': 'web'})
    with pytest.raises(BuiltinProtocolError, match='not installed') as excinfo:
        DockerProtocolBackend().build_spawn(conn, _ctx())
    assert excinfo.value.failure.code is (
        PluginSessionFailureCode.CONTAINER_RUNTIME_UNAVAILABLE
    )
    assert dict(excinfo.value.failure.parameters) == {"runtime": "docker"}


def test_build_spawn_missing_container(monkeypatch):
    import sshpilot.plugins.builtin.docker_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/docker')
    conn = Connection({'nickname': 'd', 'protocol': 'docker'})
    with pytest.raises(BuiltinProtocolError, match='[Nn]o container') as excinfo:
        DockerProtocolBackend().build_spawn(conn, _ctx())
    assert excinfo.value.failure.code is PluginSessionFailureCode.CONTAINER_REQUIRED
    assert dict(excinfo.value.failure.parameters) == {}


def test_build_spawn_invalid_command_keeps_parser_diagnostic_separate(monkeypatch):
    import sshpilot.plugins.builtin.docker_protocol as mod

    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/docker')
    conn = Connection({
        'nickname': 'd',
        'protocol': 'docker',
        'container': 'web',
        'command': "sh '",
    })

    with pytest.raises(BuiltinProtocolError) as excinfo:
        DockerProtocolBackend().build_spawn(conn, _ctx())

    failure = excinfo.value.failure
    assert failure.code is PluginSessionFailureCode.ARGUMENTS_INVALID
    assert dict(failure.parameters) == {"field": "command"}
    assert failure.diagnostic
    assert failure.diagnostic not in failure.code.value


def test_activate_registers_backend():
    Plugin().activate(_ctx())
    assert registry_mod.protocol_registry().get('docker') is not None
