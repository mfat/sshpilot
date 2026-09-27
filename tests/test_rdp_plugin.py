"""Tests for the built-in RDP protocol plugin (FreeRDP 3 in its own window)."""

import types

import pytest

import sshpilot.plugins.builtin._flatpak as flatpak
import sshpilot.plugins.builtin.rdp_protocol as rdp
from sshpilot.api.models.sessions import PluginSessionFailureCode
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.builtin._session_failure import BuiltinProtocolError
from sshpilot.plugins.builtin.rdp_protocol import RdpProtocolBackend
from sshpilot.plugins.loader import load_plugins


class FakeConfig:
    def get_setting(self, key, default=None):
        return default


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(rdp, "_version_cache", {})
    monkeypatch.setattr(rdp.sys, "platform", "linux")


def _installed(monkeypatch, *programs, v3=True):
    """Pretend only ``programs`` exist; unsuffixed xfreerdp reports ``v3``."""
    monkeypatch.setattr(
        flatpak,
        "resolve_host_binary",
        lambda name: [f"/usr/bin/{name}"] if name in programs else None,
    )

    version = "3.31.0" if v3 else "2.11.7"
    monkeypatch.setattr(
        flatpak,
        "host_binary_version",
        lambda argv: f"This is FreeRDP version {version} (git n/a)\n",
    )


def _spawn(monkeypatch, *, wayland=False, **data):
    if wayland:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    else:
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    connection = types.SimpleNamespace(nickname="desk", data=data)
    return RdpProtocolBackend().build_spawn(connection, None)


def test_loader_discovers_rdp():
    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert any(p.plugin_id == "rdp" and p.builtin for p in loaded)
    backend = registry_mod.protocol_registry().get_or_none("rdp")
    assert backend is not None
    assert backend.default_port == 3389
    assert backend.capabilities() == frozenset()


def test_defaults_give_a_short_command(monkeypatch):
    _installed(monkeypatch, "xfreerdp3")
    spec = _spawn(monkeypatch, host="win.example")
    assert spec.argv == [
        "/usr/bin/xfreerdp3", "/v:win.example:3389", "/t:desk",
        "/dynamic-resolution", "/sound",
    ]


def test_never_reads_credentials_from_stdin(monkeypatch):
    """``/from-stdin`` makes FreeRDP reject unknown certificates unasked."""
    _installed(monkeypatch, "xfreerdp3")
    spec = _spawn(monkeypatch, host="win.example", username="alice",
                  extra_rdp_args="")
    assert not any(arg.startswith("/from-stdin") for arg in spec.argv)
    assert not any(arg.startswith("/p:") for arg in spec.argv)


def test_ipv6_literal_is_bracketed(monkeypatch):
    _installed(monkeypatch, "xfreerdp3")
    spec = _spawn(monkeypatch, host="fe80::1", port=3390)
    assert "/v:[fe80::1]:3390" in spec.argv


@pytest.mark.parametrize(
    "wayland,installed,expected",
    [
        (True, ("sdl-freerdp3", "xfreerdp3"), "sdl-freerdp3"),
        (False, ("sdl-freerdp3", "xfreerdp3"), "xfreerdp3"),
        (True, ("xfreerdp3",), "xfreerdp3"),
        (False, ("sdl-freerdp3",), "sdl-freerdp3"),
        # Fedora and Homebrew install the unsuffixed names.
        (True, ("sdl-freerdp", "xfreerdp"), "sdl-freerdp"),
        (False, ("sdl-freerdp", "xfreerdp"), "xfreerdp"),
    ],
)
def test_automatic_client_follows_the_session(monkeypatch, wayland, installed,
                                              expected):
    _installed(monkeypatch, *installed)
    spec = _spawn(monkeypatch, wayland=wayland, host="win.example")
    assert spec.argv[0] == f"/usr/bin/{expected}"


def test_explicit_client_choice_is_honoured(monkeypatch):
    _installed(monkeypatch, "sdl-freerdp3", "xfreerdp3")
    assert _spawn(monkeypatch, wayland=True, host="h", client="x11").argv[0] \
        == "/usr/bin/xfreerdp3"
    assert _spawn(monkeypatch, host="h", client="sdl").argv[0] \
        == "/usr/bin/sdl-freerdp3"


def test_freerdp2_xfreerdp_is_not_used(monkeypatch):
    """Its options differ (``/cert-ignore``…), so a v2 build must not launch."""
    _installed(monkeypatch, "xfreerdp", v3=False)
    with pytest.raises(BuiltinProtocolError) as raised:
        _spawn(monkeypatch, host="win.example")
    assert raised.value.failure.code is PluginSessionFailureCode.RDP_CLIENT_UNAVAILABLE


def test_missing_client_is_a_structured_failure(monkeypatch):
    _installed(monkeypatch)
    with pytest.raises(BuiltinProtocolError) as raised:
        _spawn(monkeypatch, host="win.example")
    failure = raised.value.failure
    assert failure.code is PluginSessionFailureCode.RDP_CLIENT_UNAVAILABLE
    assert dict(failure.parameters) == {
        "preferred_program": "sdl-freerdp3",
        "fallback_program": "xfreerdp3",
    }


def test_missing_host_is_a_structured_failure(monkeypatch):
    _installed(monkeypatch, "xfreerdp3")
    with pytest.raises(BuiltinProtocolError) as raised:
        _spawn(monkeypatch, host="")
    assert raised.value.failure.code is PluginSessionFailureCode.HOST_REQUIRED


def test_shared_folder_expands_home(monkeypatch):
    _installed(monkeypatch, "xfreerdp3")
    monkeypatch.setenv("HOME", "/home/alice")
    spec = _spawn(monkeypatch, host="h", shared_folder="~/Public")
    assert "/drive:sshpilot,/home/alice/Public" in spec.argv


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", 70000),
        ("port", "rdp"),
        ("size", "1600*900"),
        ("size", "big"),
        ("gateway", "gw.example,u:bob"),
        ("gateway", "gw example"),
        ("shared_folder", "/srv/a,b"),
        ("extra_rdp_args", '/app:"unterminated'),
    ],
)
def test_validation_rejects_malformed_fields(field, value):
    assert RdpProtocolBackend().validate({"host": "h", field: value})


def test_validation_accepts_a_full_connection():
    assert RdpProtocolBackend().validate({
        "host": "h", "port": 3389, "size": "1920x1080",
        "gateway": "gw.example:443", "shared_folder": "~/Public",
        "extra_rdp_args": "/network:auto +fonts",
    }) == []
