"""Tests for the built-in telnet protocol plugin — the first non-SSH
protocol, proving the plugin API end to end."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext, ProtocolError
from sshpilot.plugins.builtin.telnet_protocol import Plugin, TelnetProtocolBackend
from sshpilot.plugins.loader import load_plugins


class FakeConfig:
    def get_setting(self, key, default=None):
        return default


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def _ctx():
    return PluginContext(app_config=None, connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


def test_loader_discovers_and_activates_telnet():
    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert any(p.plugin_id == 'telnet' and p.builtin for p in loaded)
    backend = registry_mod.protocol_registry().get_or_none('telnet')
    assert backend is not None
    assert backend.default_port == 23


def test_telnet_is_disableable():
    class DisablingConfig:
        def get_setting(self, key, default=None):
            if key == 'plugins.disabled':
                return ['telnet']
            return default

    loaded = load_plugins(app_config=DisablingConfig(), connection_manager=None)
    assert not any(p.plugin_id == 'telnet' for p in loaded)
    assert registry_mod.protocol_registry().get_or_none('telnet') is None
    # SSH (required) is unaffected.
    assert registry_mod.protocol_registry().get_or_none('ssh') is not None


def test_capabilities_empty_and_fields_declared():
    backend = TelnetProtocolBackend()
    assert backend.capabilities() == frozenset()
    fields = backend.connection_fields()
    by_key = {f.key: f for f in fields}
    assert by_key['host'].required
    assert by_key['port'].kind == 'int'
    assert by_key['port'].default == 23


def test_validate_matrix():
    backend = TelnetProtocolBackend()
    assert backend.validate({'host': 'h', 'port': 23}) == []
    assert backend.validate({'host': 'h'}) == []
    assert any('host' in e.lower() for e in backend.validate({'port': 23}))
    assert any('port' in e.lower() for e in backend.validate({'host': 'h', 'port': 0}))
    assert any('port' in e.lower() for e in backend.validate({'host': 'h', 'port': 'abc'}))


def test_build_spawn_argv(monkeypatch):
    import sshpilot.plugins.builtin.telnet_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/telnet')

    backend = TelnetProtocolBackend()

    conn = Connection({'nickname': 't', 'protocol': 'telnet', 'host': '10.0.0.5'})
    spec = backend.build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/telnet', '10.0.0.5']
    assert spec.extras == {}  # no sshpass/askpass hints

    conn = Connection({'nickname': 't', 'protocol': 'telnet',
                       'host': '10.0.0.5', 'port': 2323})
    spec = backend.build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/telnet', '10.0.0.5', '2323']


def test_build_spawn_default_port_omitted(monkeypatch):
    import sshpilot.plugins.builtin.telnet_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/telnet')
    conn = Connection({'nickname': 't', 'protocol': 'telnet',
                       'host': 'example.com', 'port': 23})
    spec = TelnetProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/telnet', 'example.com']


def test_build_spawn_missing_binary(monkeypatch):
    import sshpilot.plugins.builtin.telnet_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    conn = Connection({'nickname': 't', 'protocol': 'telnet', 'host': 'h'})
    with pytest.raises(ProtocolError, match='not installed'):
        TelnetProtocolBackend().build_spawn(conn, _ctx())


def test_build_spawn_missing_host(monkeypatch):
    import sshpilot.plugins.builtin.telnet_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/telnet')
    conn = Connection({'nickname': 'incomplete', 'protocol': 'telnet'})
    conn.hostname = ''
    conn.host = ''
    with pytest.raises(ProtocolError, match='[Nn]o host'):
        TelnetProtocolBackend().build_spawn(conn, _ctx())


def test_activate_registers_backend():
    Plugin().activate(_ctx())
    assert registry_mod.protocol_registry().get('telnet') is not None
