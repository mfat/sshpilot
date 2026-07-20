"""Tests for the example plugin. Mirrors sshPilot's built-in protocol tests:
plugin logic is plain Python and unit-testable without GTK.

Requires sshpilot importable (CI installs it with --no-deps; sshpilot.plugins.api
has no GTK dependency)."""

import importlib.util
import os

import pytest

from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext, ProtocolError

HERE = os.path.dirname(__file__)


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "example_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    monkeypatch.setattr(registry_mod, "_registry", None)


def _ctx():
    return PluginContext(plugin_id="example-plugin", app_config=None,
                         connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


class _Conn:
    def __init__(self, **data):
        self.data = data


def test_fields_and_validate():
    mod = _load_plugin_module()
    backend = mod.ExampleProtocolBackend()
    assert {f.key for f in backend.connection_fields()} == {"command"}
    assert backend.validate({"command": "sh"}) == []
    assert backend.validate({}) != []


def test_build_spawn_argv(monkeypatch):
    mod = _load_plugin_module()
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
    spec = mod.ExampleProtocolBackend().build_spawn(_Conn(command="htop -d 5"), _ctx())
    assert spec.argv == ["/usr/bin/htop", "-d", "5"]


def test_build_spawn_missing_binary(monkeypatch):
    mod = _load_plugin_module()
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with pytest.raises(ProtocolError):
        mod.ExampleProtocolBackend().build_spawn(_Conn(command="nope"), _ctx())


def test_activate_registers_backend():
    mod = _load_plugin_module()
    mod.Plugin().activate(_ctx())
    assert registry_mod.protocol_registry().get("example") is not None
