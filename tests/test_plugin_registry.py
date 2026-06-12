"""Tests for the protocol backend registry (sshpilot.plugins.registry)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import Capability, ProtocolBackend, SpawnSpec


class _Backend(ProtocolBackend):
    protocol_id = "dummy"
    display_name = "Dummy"

    def capabilities(self):
        return frozenset({Capability.REMOTE_COMMAND})

    def build_spawn(self, connection, ctx):
        return SpawnSpec(argv=["true"])


class _Shadow(_Backend):
    display_name = "Shadow"


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    monkeypatch.setattr(registry_mod, "_registry", None)


def test_register_and_get():
    reg = registry_mod.protocol_registry()
    backend = _Backend()
    reg.register(backend)
    assert reg.get("dummy") is backend
    assert reg.get_or_none("dummy") is backend
    assert reg.all() == [backend]


def test_singleton_returns_same_instance():
    assert registry_mod.protocol_registry() is registry_mod.protocol_registry()


def test_unknown_protocol():
    reg = registry_mod.protocol_registry()
    assert reg.get_or_none("nope") is None
    with pytest.raises(KeyError):
        reg.get("nope")


def test_duplicate_registration_first_wins():
    reg = registry_mod.protocol_registry()
    first = _Backend()
    reg.register(first)
    reg.register(_Shadow())  # must be ignored, not raise
    assert reg.get("dummy") is first
    assert len(reg.all()) == 1


def test_empty_protocol_id_rejected():
    reg = registry_mod.protocol_registry()
    backend = _Backend()
    backend.protocol_id = ""
    with pytest.raises(ValueError):
        reg.register(backend)
