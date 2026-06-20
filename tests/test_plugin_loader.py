"""Tests for plugin discovery and loading (sshpilot.plugins.loader)."""

import json
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins import loader as loader_mod
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.loader import discover_plugins, load_plugins


class FakeConfig:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    # Point the user plugin dir at an (initially empty) temp dir.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def _write_user_plugin(tmp_path, plugin_id, api_version=1, body=None):
    plugin_dir = tmp_path / "xdg-data" / "sshpilot" / "plugins" / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(json.dumps({
        "id": plugin_id,
        "name": f"{plugin_id} plugin",
        "api_version": api_version,
    }))
    (plugin_dir / "__init__.py").write_text(body or textwrap.dedent(f"""
        from sshpilot.plugins.api import (
            ProtocolBackend, SpawnSpec, SshPilotPlugin,
        )

        class _Backend(ProtocolBackend):
            protocol_id = "{plugin_id}"
            display_name = "{plugin_id}"

            def capabilities(self):
                return frozenset()

            def build_spawn(self, connection, ctx):
                return SpawnSpec(argv=["true"])

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                ctx.register_protocol(_Backend())
    """))
    return plugin_dir


def test_builtin_ssh_plugin_loads():
    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert any(p.plugin_id == "ssh" and p.builtin for p in loaded)
    assert registry_mod.protocol_registry().get_or_none("ssh") is not None


def test_required_builtin_ignores_disabled_list():
    cfg = FakeConfig({"plugins.disabled": ["ssh"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert any(p.plugin_id == "ssh" for p in loaded)


def test_load_plugins_raises_without_ssh_backend(monkeypatch):
    monkeypatch.setattr(loader_mod, "_load_builtin", lambda ctx, disabled: [])
    with pytest.raises(RuntimeError):
        load_plugins(app_config=FakeConfig(), connection_manager=None)


def test_user_plugin_is_opt_in(tmp_path):
    _write_user_plugin(tmp_path, "dummy")

    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert not any(p.plugin_id == "dummy" for p in loaded)
    assert registry_mod.protocol_registry().get_or_none("dummy") is None

    cfg = FakeConfig({"plugins.enabled": ["dummy"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert any(p.plugin_id == "dummy" and not p.builtin for p in loaded)
    assert registry_mod.protocol_registry().get_or_none("dummy") is not None


def test_api_version_mismatch_skipped(tmp_path):
    _write_user_plugin(tmp_path, "futuristic", api_version=99)
    cfg = FakeConfig({"plugins.enabled": ["futuristic"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert not any(p.plugin_id == "futuristic" for p in loaded)


def test_broken_user_plugin_does_not_break_loading(tmp_path):
    _write_user_plugin(tmp_path, "broken", body="raise RuntimeError('boom')\n")
    cfg = FakeConfig({"plugins.enabled": ["broken"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert any(p.plugin_id == "ssh" for p in loaded)
    assert not any(p.plugin_id == "broken" for p in loaded)


def test_discover_reads_manifest_version(tmp_path):
    # A `version` in plugin.json populates PluginInfo.version (drives update
    # detection); its absence leaves it None.
    versioned = (tmp_path / "xdg-data" / "sshpilot" / "plugins" / "versioned")
    versioned.mkdir(parents=True)
    (versioned / "plugin.json").write_text(json.dumps({
        "id": "versioned", "name": "Versioned", "api_version": 1,
        "version": "1.2.3",
    }))
    (versioned / "__init__.py").write_text("")
    _write_user_plugin(tmp_path, "nover")  # no version field

    by_id = {i.plugin_id: i for i in discover_plugins()}
    assert by_id["versioned"].version == "1.2.3"
    assert by_id["nover"].version is None


def test_user_plugin_with_dataclass_loads(tmp_path):
    # A user plugin using @dataclass + `from __future__ import annotations`
    # must load. On Python 3.14 @dataclass resolves annotations via
    # sys.modules[cls.__module__], so the loader has to register the module
    # before exec_module — otherwise this raises AttributeError on import.
    body = textwrap.dedent("""
        from __future__ import annotations
        from dataclasses import dataclass
        from sshpilot.plugins.api import SshPilotPlugin

        @dataclass
        class Row:
            name: str
            port: int = 22

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                self.row = Row("x")
    """)
    _write_user_plugin(tmp_path, "dataclassy", body=body)
    cfg = FakeConfig({"plugins.enabled": ["dataclassy"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert any(p.plugin_id == "dataclassy" and not p.builtin for p in loaded)


def test_discover_plugins_lists_without_importing(tmp_path):
    plugin_dir = _write_user_plugin(
        tmp_path, "lazy",
        body="open(__file__ + '.executed', 'w').close()\n")
    infos = discover_plugins()
    by_id = {i.plugin_id: i for i in infos}

    assert "ssh" in by_id
    assert by_id["ssh"].builtin and by_id["ssh"].required
    assert by_id["ssh"].api_compatible

    assert "lazy" in by_id
    assert not by_id["lazy"].builtin
    # Discovery must not execute plugin code.
    assert not (plugin_dir / "__init__.py.executed").exists()
