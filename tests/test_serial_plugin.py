"""Tests for the built-in serial console protocol plugin."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext, ProtocolError
from sshpilot.plugins.builtin.serial_protocol import Plugin, SerialProtocolBackend
from sshpilot.plugins.loader import load_plugins


class FakeConfig:
    def get_setting(self, key, default=None):
        return default


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def _ctx():
    return PluginContext(plugin_id="serial", app_config=None, connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


def test_loader_discovers_and_activates_serial():
    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert any(p.plugin_id == 'serial' and p.builtin for p in loaded)
    backend = registry_mod.protocol_registry().get_or_none('serial')
    assert backend is not None
    assert backend.default_port is None


def test_capabilities_empty_and_fields_declared():
    backend = SerialProtocolBackend()
    assert backend.capabilities() == frozenset()
    by_key = {f.key: f for f in backend.connection_fields()}
    assert by_key['device'].required
    assert by_key['baud'].kind == 'choice'
    assert by_key['baud'].default == '115200'


def test_validate_matrix():
    backend = SerialProtocolBackend()
    assert backend.validate({'device': '/dev/ttyUSB0', 'baud': '115200'}) == []
    assert any('device' in e.lower() for e in backend.validate({'baud': '9600'}))
    assert any('baud' in e.lower() for e in backend.validate({'device': '/dev/x', 'baud': 'abc'}))


def test_build_spawn_prefers_picocom(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which',
                        lambda name: '/usr/bin/picocom' if name == 'picocom' else None)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyUSB0', 'baud': '57600', 'flow': 'hard'})
    spec = SerialProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/picocom', '-b', '57600', '-f', 'h', '/dev/ttyUSB0']


def test_build_spawn_falls_back_to_screen(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which',
                        lambda name: '/usr/bin/screen' if name == 'screen' else None)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyS0', 'baud': '9600'})
    spec = SerialProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/screen', '/dev/ttyS0', '9600']


def test_build_spawn_missing_binaries(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    conn = Connection({'nickname': 's', 'protocol': 'serial', 'device': '/dev/x'})
    with pytest.raises(ProtocolError, match='picocom'):
        SerialProtocolBackend().build_spawn(conn, _ctx())


def test_build_spawn_missing_device(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/picocom')
    conn = Connection({'nickname': 's', 'protocol': 'serial'})
    with pytest.raises(ProtocolError, match='[Nn]o serial device'):
        SerialProtocolBackend().build_spawn(conn, _ctx())


def test_activate_registers_backend():
    Plugin().activate(_ctx())
    assert registry_mod.protocol_registry().get('serial') is not None
