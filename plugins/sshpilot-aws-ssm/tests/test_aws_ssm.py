"""Tests for AWS SSM. Pure argv assembly is tested directly; the backend's
registration is tested via the registry (template style). No GTK needed."""

import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "aws_ssm_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Conn:
    def __init__(self, **data):
        self.data = data


def test_build_argv_minimal():
    mod = _load()
    argv = mod.build_ssm_argv({"instance_id": "i-123"}, "/usr/bin/aws")
    assert argv == ["/usr/bin/aws", "ssm", "start-session", "--target", "i-123"]


def test_build_argv_with_all_options():
    mod = _load()
    argv = mod.build_ssm_argv({
        "instance_id": "i-abc",
        "profile": "prod",
        "region": "eu-central-1",
        "document_name": "AWS-StartInteractiveCommand",
    }, "aws")
    assert argv == [
        "aws", "ssm", "start-session", "--target", "i-abc",
        "--profile", "prod",
        "--region", "eu-central-1",
        "--document-name", "AWS-StartInteractiveCommand",
    ]


def test_build_argv_flatpak_prefix():
    mod = _load()
    argv = mod.build_ssm_argv({"instance_id": "i-1"}, "aws", flatpak_prefix=True)
    assert argv[:3] == ["flatpak-spawn", "--host", "aws"]
    assert argv[-2:] == ["--target", "i-1"]


def test_build_argv_requires_instance():
    mod = _load()
    with pytest.raises(mod.ProtocolError):
        mod.build_ssm_argv({"instance_id": "  "}, "aws")


def test_validate():
    mod = _load()
    backend = mod.AwsSsmBackend()
    assert backend.validate({"instance_id": "i-1"}) == []
    assert backend.validate({}) != []
    assert backend.capabilities() == frozenset()
    assert {f.key for f in backend.connection_fields()} == {
        "instance_id", "profile", "region", "document_name"}


def test_build_spawn_uses_which(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.shutil, "which",
                        lambda name: "/usr/bin/aws" if name == "aws" else None)
    spec = mod.AwsSsmBackend().build_spawn(_Conn(instance_id="i-9"), None)
    assert spec.argv == ["/usr/bin/aws", "ssm", "start-session", "--target", "i-9"]


def test_build_spawn_missing_aws_raises(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod, "_is_flatpak", lambda: False)
    with pytest.raises(mod.ProtocolError):
        mod.AwsSsmBackend().build_spawn(_Conn(instance_id="i-9"), None)


def test_activate_registers_protocol():
    mod = _load()
    from sshpilot.plugins import registry as registry_mod
    registry_mod._registry = None
    mod.Plugin().activate(_ctx(registry_mod))
    assert registry_mod.protocol_registry().get_or_none("ssm") is not None


def _ctx(registry_mod):
    from sshpilot.plugins.api import PluginContext
    return PluginContext(plugin_id="aws-ssm", app_config=None,
                         connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())
