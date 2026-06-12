"""Tests for capability lookup used by UI gating (sshpilot.plugins.registry)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import Capability, ProtocolBackend, SpawnSpec
from sshpilot.plugins.registry import capabilities_for


class _DummyBackend(ProtocolBackend):
    protocol_id = "dummy"
    display_name = "Dummy"

    def capabilities(self):
        return frozenset({Capability.REMOTE_COMMAND})

    def build_spawn(self, connection, ctx):
        return SpawnSpec(argv=["true"])


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    monkeypatch.setattr(registry_mod, "_registry", None)


def _load_ssh_backend():
    from sshpilot.plugins.builtin.ssh_protocol import Plugin
    from sshpilot.plugins.api import PluginContext
    ctx = PluginContext(app_config=None, connection_manager=None,
                        protocol_registry=registry_mod.protocol_registry())
    Plugin().activate(ctx)


def test_ssh_connection_has_all_ui_capabilities():
    _load_ssh_backend()
    conn = Connection({'nickname': 'a', 'hostname': 'a.example.com'})
    caps = capabilities_for(conn)
    for cap in (Capability.FILE_TRANSFER, Capability.PORT_FORWARDING,
                Capability.REMOTE_COMMAND, Capability.KEY_DEPLOYMENT):
        assert cap in caps


def test_missing_protocol_attribute_defaults_to_ssh():
    _load_ssh_backend()

    class Bare:
        pass

    assert Capability.FILE_TRANSFER in capabilities_for(Bare())


def test_plugin_protocol_capabilities():
    registry_mod.protocol_registry().register(_DummyBackend())
    conn = Connection({'nickname': 'd', 'protocol': 'dummy'})
    caps = capabilities_for(conn)
    assert caps == frozenset({Capability.REMOTE_COMMAND})
    assert Capability.FILE_TRANSFER not in caps


def test_unknown_protocol_yields_empty_set():
    conn = Connection({'nickname': 'x', 'protocol': 'nonexistent'})
    assert capabilities_for(conn) == frozenset()
