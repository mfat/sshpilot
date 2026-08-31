"""Tests for the built-in serial console protocol plugin."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.api.models.sessions import PluginSessionFailureCode
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.builtin._session_failure import BuiltinProtocolError
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


def test_capabilities_empty_and_fields_declared(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod

    monkeypatch.setattr(mod, "_", lambda msgid: f"translated:{msgid}")
    backend = SerialProtocolBackend()
    assert backend.capabilities() == frozenset()
    by_key = {f.key: f for f in backend.connection_fields()}
    assert by_key['device'].required
    assert by_key['baud'].kind == 'choice'
    assert by_key['baud'].default == '115200'
    assert by_key['flow'].choices == [
        ('none', 'translated:None'),
        ('hard', 'translated:Hardware (RTS/CTS)'),
        ('soft', 'translated:Software (XON/XOFF)'),
    ]
    assert by_key['parity'].choices == [
        ('none', 'translated:None'),
        ('even', 'translated:Even'),
        ('odd', 'translated:Odd'),
    ]


def test_validate_matrix(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod

    monkeypatch.setattr(mod, "_", lambda msgid: f"translated:{msgid}")
    backend = SerialProtocolBackend()
    assert backend.validate({'device': '/dev/ttyUSB0', 'baud': '115200'}) == []
    assert backend.validate({'baud': '9600'}) == [
        'translated:A serial device is required.'
    ]
    assert backend.validate({'device': '/dev/x', 'baud': '0'}) == [
        'translated:Baud rate must be a positive number.'
    ]
    assert backend.validate({'device': '/dev/x', 'baud': 'abc'}) == [
        'translated:Baud rate must be a number.'
    ]


def test_build_spawn_prefers_picocom(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which',
                        lambda name: '/usr/bin/picocom' if name == 'picocom' else None)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyUSB0', 'baud': '57600', 'flow': 'hard'})
    spec = SerialProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/picocom', '-b', '57600', '-f', 'h', '/dev/ttyUSB0']


def test_build_spawn_line_params_when_non_default(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which',
                        lambda name: '/usr/bin/picocom' if name == 'picocom' else None)
    conn = Connection({'nickname': 's', 'protocol': 'serial', 'device': '/dev/ttyUSB0',
                       'baud': '9600', 'flow': 'none', 'databits': '7',
                       'parity': 'even', 'stopbits': '2'})
    spec = SerialProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/picocom', '-b', '9600', '-f', 'n',
                         '--databits', '7', '--parity', 'e', '--stopbits', '2',
                         '/dev/ttyUSB0']


def test_build_spawn_omits_default_line_params(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which',
                        lambda name: '/usr/bin/picocom' if name == 'picocom' else None)
    conn = Connection({'nickname': 's', 'protocol': 'serial', 'device': '/dev/ttyUSB0',
                       'baud': '9600', 'databits': '8', 'parity': 'none', 'stopbits': '1'})
    spec = SerialProtocolBackend().build_spawn(conn, _ctx())
    assert '--databits' not in spec.argv and '--parity' not in spec.argv
    assert '--stopbits' not in spec.argv


def _screen_only(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which',
                        lambda name: '/usr/bin/screen' if name == 'screen' else None)


def test_build_spawn_falls_back_to_screen(monkeypatch):
    """The fallback carries the line parameters, not just device+baud.

    ``screen`` takes them as a comma-separated stty-style list appended to the
    baud argument.  Verified against the real binary: this exact string starts
    a session, while a bogus flag in the list is rejected — so the flags are
    parsed and applied, not ignored.
    """
    _screen_only(monkeypatch)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyS0', 'baud': '9600',
                       'databits': '7', 'parity': 'even', 'stopbits': '2',
                       'flow': 'soft'})
    spec = SerialProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/screen', '/dev/ttyS0',
                         '9600,cs7,parenb,-parodd,cstopb,ixon,ixoff']


def test_screen_fallback_states_defaults_explicitly(monkeypatch):
    """Defaults are emitted too, rather than left to the terminal driver.

    ``man screen``: an unspecified parameter takes "values saved from a
    previous connection", so omitting the defaults makes the same saved
    connection behave differently between launches.
    """
    _screen_only(monkeypatch)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyS0', 'baud': '9600'})
    spec = SerialProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/screen', '/dev/ttyS0',
                         '9600,cs8,-parenb,-cstopb,-ixon,-ixoff']


def test_screen_fallback_maps_odd_parity(monkeypatch):
    _screen_only(monkeypatch)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyS0', 'baud': '19200', 'parity': 'odd'})
    spec = SerialProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv[-1] == '19200,cs8,parenb,parodd,-cstopb,-ixon,-ixoff'


def test_screen_fallback_refuses_hardware_flow_control(monkeypatch):
    """Refuse rather than drop: a serial line is not negotiated.

    A parameter that silently fails to apply misframes every byte, with a form
    that still shows it set — the failure looks like broken hardware.
    """
    _screen_only(monkeypatch)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyS0', 'baud': '9600', 'flow': 'hard'})
    with pytest.raises(BuiltinProtocolError) as excinfo:
        SerialProtocolBackend().build_spawn(conn, _ctx())
    assert 'RTS/CTS' in str(excinfo.value)
    assert 'picocom' in str(excinfo.value)
    failure = excinfo.value.failure
    assert failure.code is (
        PluginSessionFailureCode.SERIAL_SCREEN_HARDWARE_FLOW_UNSUPPORTED
    )
    assert dict(failure.parameters) == {
        "fallback_program": "screen",
        "preferred_program": "picocom",
        "flow": "RTS/CTS",
    }


@pytest.mark.parametrize('databits', ['6', '5'])
def test_screen_fallback_refuses_unsupported_databits(monkeypatch, databits):
    """screen offers only cs8/cs7; the editor also offers 6 and 5."""
    _screen_only(monkeypatch)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyS0', 'baud': '9600',
                       'databits': databits})
    with pytest.raises(BuiltinProtocolError) as excinfo:
        SerialProtocolBackend().build_spawn(conn, _ctx())
    assert f'{databits} data bits' in str(excinfo.value)
    failure = excinfo.value.failure
    assert failure.code is (
        PluginSessionFailureCode.SERIAL_SCREEN_DATABITS_UNSUPPORTED
    )
    assert dict(failure.parameters) == {
        "fallback_program": "screen",
        "preferred_program": "picocom",
        "databits": databits,
    }


def test_screen_fallback_reports_every_unsupported_parameter(monkeypatch):
    _screen_only(monkeypatch)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyS0', 'baud': '9600',
                       'databits': '5', 'flow': 'hard'})
    with pytest.raises(BuiltinProtocolError) as excinfo:
        SerialProtocolBackend().build_spawn(conn, _ctx())
    message = str(excinfo.value)
    assert 'RTS/CTS' in message and '5 data bits' in message
    failure = excinfo.value.failure
    assert failure.code is (
        PluginSessionFailureCode.SERIAL_SCREEN_HARDWARE_FLOW_AND_DATABITS_UNSUPPORTED
    )
    assert dict(failure.parameters) == {
        "fallback_program": "screen",
        "preferred_program": "picocom",
        "flow": "RTS/CTS",
        "databits": "5",
    }


def test_picocom_still_serves_what_screen_cannot(monkeypatch):
    """The refusal is about screen only — picocom handles both fine."""
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which',
                        lambda name: '/usr/bin/picocom' if name == 'picocom' else None)
    conn = Connection({'nickname': 's', 'protocol': 'serial',
                       'device': '/dev/ttyS0', 'baud': '9600',
                       'databits': '5', 'flow': 'hard'})
    spec = SerialProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/picocom', '-b', '9600', '-f', 'h',
                         '--databits', '5', '/dev/ttyS0']


def test_build_spawn_missing_binaries(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    conn = Connection({'nickname': 's', 'protocol': 'serial', 'device': '/dev/x'})
    with pytest.raises(BuiltinProtocolError, match='picocom') as excinfo:
        SerialProtocolBackend().build_spawn(conn, _ctx())
    failure = excinfo.value.failure
    assert failure.code is PluginSessionFailureCode.SERIAL_PROGRAMS_UNAVAILABLE
    assert dict(failure.parameters) == {
        "preferred_program": "picocom",
        "fallback_program": "screen",
    }


def test_build_spawn_missing_device(monkeypatch):
    import sshpilot.plugins.builtin.serial_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/picocom')
    conn = Connection({'nickname': 's', 'protocol': 'serial'})
    with pytest.raises(BuiltinProtocolError, match='[Nn]o serial device') as excinfo:
        SerialProtocolBackend().build_spawn(conn, _ctx())
    assert excinfo.value.failure.code is (
        PluginSessionFailureCode.SERIAL_DEVICE_REQUIRED
    )


def test_activate_registers_backend():
    Plugin().activate(_ctx())
    assert registry_mod.protocol_registry().get('serial') is not None
