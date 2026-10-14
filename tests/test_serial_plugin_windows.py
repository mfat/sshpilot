"""Serial connections on Windows: COM ports opened with PuTTY's plink."""

from __future__ import annotations

import pytest

import sshpilot.plugins.builtin.serial_protocol as mod
from sshpilot.api.models.sessions import PluginSessionFailureCode
from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.builtin._session_failure import BuiltinProtocolError
from sshpilot.plugins.builtin.serial_protocol import SerialProtocolBackend


@pytest.fixture(autouse=True)
def windows(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(mod.sys, "platform", "win32")


def _ctx():
    return PluginContext(plugin_id="serial", app_config=None, connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


def _conn(**data):
    return Connection({"nickname": "s", "protocol": "serial", "device": "COM3", **data})


def test_plink_opens_the_com_port_with_the_line_settings(monkeypatch):
    monkeypatch.setattr(mod, "_find_plink", lambda: r"C:\Program Files\PuTTY\plink.exe")
    spec = SerialProtocolBackend().build_spawn(
        _conn(baud="57600", flow="hard", databits="7", parity="even", stopbits="2"), _ctx()
    )
    assert spec.argv == [
        r"C:\Program Files\PuTTY\plink.exe", "-serial", "COM3", "-sercfg", "57600,7,e,2,R",
    ]


def test_defaults_are_8n1_without_flow_control(monkeypatch):
    monkeypatch.setattr(mod, "_find_plink", lambda: "plink.exe")
    spec = SerialProtocolBackend().build_spawn(_conn(), _ctx())
    assert spec.argv[-1] == "115200,8,n,1,N"


def test_missing_plink_is_reported(monkeypatch):
    monkeypatch.setattr(mod, "_find_plink", lambda: "")
    with pytest.raises(BuiltinProtocolError) as raised:
        SerialProtocolBackend().build_spawn(_conn(), _ctx())
    assert raised.value.failure.code is PluginSessionFailureCode.SERIAL_PLINK_UNAVAILABLE


@pytest.mark.parametrize("option", [{"local_echo": True}, {"send_newline": "lf"}, {"logfile": "x.log"}])
def test_terminal_options_plink_cannot_do_are_refused(monkeypatch, option):
    monkeypatch.setattr(mod, "_find_plink", lambda: "plink.exe")
    with pytest.raises(BuiltinProtocolError) as raised:
        SerialProtocolBackend().build_spawn(_conn(**option), _ctx())
    assert (
        raised.value.failure.code
        is PluginSessionFailureCode.SERIAL_PLINK_TERMINAL_OPTIONS_UNSUPPORTED
    )


def test_port_suggestions_come_from_the_registry(monkeypatch):
    monkeypatch.setattr(mod, "_windows_com_ports", lambda: ["COM1", "COM10", "COM3"])
    assert mod._serial_ports({}, None) == [("COM1", "COM1"), ("COM10", "COM10"), ("COM3", "COM3")]


def test_find_plink_falls_back_to_the_putty_install_directory(monkeypatch, tmp_path):
    (tmp_path / "PuTTY").mkdir()
    (tmp_path / "PuTTY" / "plink.exe").write_text("")
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    assert mod._find_plink() == str(tmp_path / "PuTTY" / "plink.exe")
