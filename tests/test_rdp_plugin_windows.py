"""RDP on Windows without FreeRDP: Windows' own Remote Desktop client."""

from __future__ import annotations

import pytest

import sshpilot.plugins.builtin.rdp_protocol as mod
from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.builtin.rdp_protocol import mstsc_settings


@pytest.fixture(autouse=True)
def windows(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod, "_resolve_client", lambda _choice, _env: None)
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))


def _ctx():
    return PluginContext(plugin_id="rdp", app_config=None, connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


def test_mstsc_is_used_when_freerdp_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_windows_mstsc", lambda: r"C:\Windows\System32\mstsc.exe")
    conn = Connection({"nickname": "Office PC", "protocol": "rdp", "host": "pc.example",
                       "username": "alice", "domain": "CORP", "display_mode": "fullscreen"})
    spec = mod.RdpProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv[0] == r"C:\Windows\System32\mstsc.exe"
    rdp = spec.argv[1]
    assert rdp.endswith("Office_PC.rdp")
    text = open(rdp, encoding="utf-16").read()
    assert "full address:s:pc.example" in text
    assert "username:s:CORP\\alice" in text
    assert "screen mode id:i:2" in text
    assert "password" not in text.lower()


def test_settings_follow_the_connection_options():
    lines = mstsc_settings(
        {"display_mode": "fit", "size": "1600x900", "multimon": True,
         "clipboard": False, "sound": False, "microphone": True, "printer": True},
        "10.0.0.5", 3390,
    )
    assert "full address:s:10.0.0.5:3390" in lines
    assert "smart sizing:i:1" in lines and "screen mode id:i:1" in lines
    assert "desktopwidth:i:1600" in lines and "desktopheight:i:900" in lines
    assert "use multimon:i:1" in lines
    assert "redirectclipboard:i:0" in lines and "audiomode:i:2" in lines
    assert "audiocapturemode:i:1" in lines and "redirectprinters:i:1" in lines


def test_without_any_client_the_failure_is_unchanged(monkeypatch):
    from sshpilot.api.models.sessions import PluginSessionFailureCode
    from sshpilot.plugins.builtin._session_failure import BuiltinProtocolError

    monkeypatch.setattr(mod, "_windows_mstsc", lambda: "")
    conn = Connection({"nickname": "x", "protocol": "rdp", "host": "h"})
    with pytest.raises(BuiltinProtocolError) as raised:
        mod.RdpProtocolBackend().build_spawn(conn, _ctx())
    assert raised.value.failure.code is PluginSessionFailureCode.RDP_CLIENT_UNAVAILABLE
