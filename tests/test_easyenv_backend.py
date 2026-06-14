"""Unit tests for the EasyEnv protocol backend (examples/easyenv_workspaces)."""

import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.plugins.api import PluginContext, ProtocolError
from sshpilot.plugins.registry import ProtocolRegistry

_PLUGIN = os.path.join(
    os.path.dirname(__file__), '..', 'sshpilot', 'plugins', 'examples',
    'easyenv_workspaces', '__init__.py')


def _load_module():
    spec = importlib.util.spec_from_file_location("easyenv_ex", _PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def _ctx():
    return PluginContext.for_spawn(plugin_id="easyenv-workspaces", app_config=None,
                                   connection_manager=None,
                                   protocol_registry=ProtocolRegistry())


def test_argv_workspace_ssh_fallback(monkeypatch):
    """No machine pinned -> connect to the first machine via workspace ssh."""
    monkeypatch.setattr(mod.shutil, "which", lambda n: "/usr/bin/easyenv")
    monkeypatch.setattr(mod.os.path, "exists", lambda p: False)  # not flatpak
    conn = Connection({"nickname": "w", "protocol": "easyenv", "workspace_id": "ws-1"})
    spec = mod.EasyEnvBackend().build_spawn(conn, _ctx())
    assert spec.argv == ["/usr/bin/easyenv", "workspace", "ssh", "ws-1"]


def test_argv_machine_ssh_per_node(monkeypatch):
    """A pinned machine_id -> easyenv machine ssh <m> -w <ws>."""
    monkeypatch.setattr(mod.shutil, "which", lambda n: "/usr/bin/easyenv")
    monkeypatch.setattr(mod.os.path, "exists", lambda p: False)
    conn = Connection({"nickname": "w", "protocol": "easyenv",
                       "workspace_id": "ws-1", "machine_id": "m-2"})
    spec = mod.EasyEnvBackend().build_spawn(conn, _ctx())
    assert spec.argv == ["/usr/bin/easyenv", "machine", "ssh", "m-2", "-w", "ws-1"]


def test_argv_machine_back_compat_alias(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda n: "/usr/bin/easyenv")
    monkeypatch.setattr(mod.os.path, "exists", lambda p: False)
    conn = Connection({"nickname": "w", "protocol": "easyenv",
                       "workspace_id": "ws-1", "machine": "m-9"})
    spec = mod.EasyEnvBackend().build_spawn(conn, _ctx())
    assert spec.argv == ["/usr/bin/easyenv", "machine", "ssh", "m-9", "-w", "ws-1"]


def test_missing_binary_raises_protocol_error(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda n: None)
    monkeypatch.setattr(mod.os.path, "exists", lambda p: False)
    conn = Connection({"nickname": "w", "protocol": "easyenv", "workspace_id": "ws-1"})
    with pytest.raises(ProtocolError, match="easyenv"):
        mod.EasyEnvBackend().build_spawn(conn, _ctx())


def test_missing_workspace_id_raises(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda n: "/usr/bin/easyenv")
    monkeypatch.setattr(mod.os.path, "exists", lambda p: False)
    conn = Connection({"nickname": "", "protocol": "easyenv"})
    # nickname empty and no workspace_id -> no id to ssh to
    conn.nickname = ""
    with pytest.raises(ProtocolError):
        mod.EasyEnvBackend().build_spawn(conn, _ctx())


def test_flatpak_prefix(monkeypatch):
    monkeypatch.setattr(mod.os.path, "exists", lambda p: p == "/.flatpak-info")
    conn = Connection({"nickname": "w", "protocol": "easyenv",
                       "workspace_id": "ws-1", "machine_id": "m-2"})
    spec = mod.EasyEnvBackend().build_spawn(conn, _ctx())
    assert spec.argv == ["flatpak-spawn", "--host", "easyenv",
                         "machine", "ssh", "m-2", "-w", "ws-1"]


def test_fields_and_validate():
    backend = mod.EasyEnvBackend()
    assert backend.capabilities() == frozenset()
    keys = {f.key for f in backend.connection_fields()}
    assert keys == {"workspace_id", "machine_id", "machine_name"}
    assert backend.validate({}) != []
    assert backend.validate({"workspace_id": "ws-1"}) == []


def test_parse_machines():
    # machine list shape: [{uuid,title,status,host_address}]
    out = mod._parse_machines(
        '[{"uuid":"m-1","title":"Ansible Dev Env","status":"active"},'
        '{"uuid":"m-2","title":"Ubuntu 24.04 LTS","status":"active"}]')
    assert out == [{"id": "m-1", "name": "Ansible Dev Env", "status": "active"},
                   {"id": "m-2", "name": "Ubuntu 24.04 LTS", "status": "active"}]
    # workspace get nests machines under "boxes"
    out = mod._parse_machines('{"uuid":"ws-1","title":"t","boxes":[{"uuid":"b1","title":"n"}]}')
    assert out == [{"id": "b1", "name": "n", "status": "unknown"}]
    assert mod._parse_machines("not json") == []


def test_node_nicknames_dedupes_repeated_titles():
    machines = [{"id": "m-1", "name": "Ansible Dev Env", "status": "active"},
                {"id": "m-2", "name": "Ubuntu 24.04 LTS", "status": "active"},
                {"id": "m-3", "name": "Ubuntu 24.04 LTS", "status": "active"},
                {"id": "m-4", "name": "Ubuntu 24.04 LTS", "status": "active"}]
    pairs = mod.Plugin._node_nicknames("ansible-cluster", machines)
    nicks = [n for _m, n in pairs]
    assert len(set(nicks)) == 4  # all unique
    assert "ansible-cluster / Ansible Dev Env" in nicks
    assert "ansible-cluster / Ubuntu 24.04 LTS 1" in nicks
    assert "ansible-cluster / Ubuntu 24.04 LTS 3" in nicks


def test_parse_workspaces_tolerates_shapes():
    # EasyEnv API shape: uuid + title + status
    assert mod._parse_workspaces('[{"uuid":"ws-1","title":"x","status":"active"}]') == \
        [{"id": "ws-1", "name": "x", "status": "active"}]
    # paginated wrapper (PaginatedWorkspaceListList -> results)
    out = mod._parse_workspaces('{"results":[{"uuid":"ws-2","title":"y","status":"stopped"}]}')
    assert out == [{"id": "ws-2", "name": "y", "status": "stopped"}]
    # alternate field names still tolerated (id/name/state)
    assert mod._parse_workspaces('[{"id":7,"name":"z","state":"running"}]') == \
        [{"id": "7", "name": "z", "status": "running"}]
    # garbage
    assert mod._parse_workspaces("not json") == []
