"""A machine as an sshPilot connection."""

import subprocess

import pytest

import importlib.util as _ilu
import os as _os
import sys as _sys

from sshpilot.plugins import registry as _registry

# The plugin ships as an example beside the SDK; its modules are loaded from
# there, the way sshPilot's loader loads a user plugin.
_ROOT = _os.path.join(_os.path.dirname(_registry.__file__), "examples", "easyenv_workspaces")


def load(name):
    spec = _ilu.spec_from_file_location(f"easyenv_example_{name}",
                                        _os.path.join(_ROOT, f"{name}.py"))
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin():
    package = "sshpilot_easyenv_example_under_test"
    if package in _sys.modules:
        return _sys.modules[package]
    spec = _ilu.spec_from_file_location(package, _os.path.join(_ROOT, "__init__.py"),
                                        submodule_search_locations=[_ROOT])
    module = _ilu.module_from_spec(spec)
    _sys.modules[package] = module
    spec.loader.exec_module(module)
    return module

conn = load("connections")


def machine(uuid="AbC123", title="node", password="", user="easyenv"):
    return {"uuid": uuid, "title": title, "recipe": "Ubuntu", "state": "running",
            "user": user, "password": password}


def test_machines_are_reached_through_the_gateway_with_a_key():
    data = conn.connection_data("dev", machine(password="pw"))
    assert "proxy_command" not in data  # sshPilot's daemon drops that field
    lines = data["extra_ssh_config"].splitlines()
    assert lines[0] == ("ProxyCommand ssh -o StrictHostKeyChecking=accept-new "
                        "-o ExitOnForwardFailure=yes -W %h:%p easyenv@ssh.easyenv.io")
    assert "StrictHostKeyChecking accept-new" in lines
    assert data["hostname"] == "AbC123.box.easyenv.io"
    assert data["port"] == 22
    # The gateway takes keys only; a password connection turned them off.
    assert (data["auth_method"], data["password"]) == (0, "")
    assert "PubkeyAuthentication" not in data["extra_ssh_config"]


def test_the_username_the_api_gives_is_used():
    assert conn.connection_data("dev", machine(user="dev"))["username"] == "dev"


def test_one_machine_is_named_after_its_workspace_and_several_are_numbered():
    assert conn.nicknames("dev", [machine("a")]) == {"a": "dev"}
    names = conn.nicknames("dev", [machine("b", "node"), machine("a", "node"), machine("c", "db")])
    assert names == {"a": "dev-node-1", "b": "dev-node-2", "c": "dev-db"}


def test_machines_the_api_named_after_the_workspace_are_not_named_twice():
    # create_body names them dev-1, dev-2.
    names = conn.nicknames("dev", [machine("a", "dev-1"), machine("b", "dev-2")])
    assert names == {"a": "dev-1", "b": "dev-2"}


def test_nicknames_are_destinations_ssh_accepts():
    assert conn.nicknames("My lab / v2", [machine("a")]) == {"a": "My-lab-v2"}
    assert conn.alias("--x") == "x"
    assert conn.alias(" ") == "easyenv"


class Info:
    def __init__(self, nickname):
        self.nickname = nickname


class Ctx:
    def __init__(self, existing=(), add_raises=False):
        self.saved = {n: {} for n in existing}
        self.add_raises = add_raises
        self.groups = []
        self.calls = []

    def list_connections(self):
        return [Info(n) for n in self.saved]

    def add_connection(self, data):
        self.calls.append(("add", data["nickname"]))
        if self.add_raises:
            self.saved[data["nickname"]] = {}
            raise RuntimeError("CONNECTION_ALREADY_EXISTS")
        self.saved[data["nickname"]] = data

    def update_connection(self, nickname, data):
        self.calls.append(("update", nickname))
        if nickname not in self.saved:
            return False
        self.saved[nickname] = data
        return True

    def create_group(self, name):
        return "g1"

    def add_connection_to_group(self, nickname, group):
        self.groups.append((nickname, group))
        return True


def test_an_existing_connection_is_updated_not_added_again():
    ctx = Ctx(existing=["dev"])
    conn.upsert(ctx, conn.connection_data("dev", machine()))
    assert ctx.calls == [("update", "dev")]


def test_a_connection_made_meanwhile_is_updated_after_the_add_fails():
    ctx = Ctx(add_raises=True)
    conn.upsert(ctx, conn.connection_data("dev", machine()))
    assert ctx.calls == [("add", "dev"), ("update", "dev")]
    assert ctx.saved["dev"]["hostname"] == "AbC123.box.easyenv.io"


def test_saving_one_machine_of_several_keeps_every_nickname_and_groups_it():
    view = {"title": "lab", "machines": [machine("a", "web"), machine("b", "db")]}
    ctx = Ctx()
    names = conn.save_workspace(ctx, view, only="b")
    assert names == {"a": "lab-web", "b": "lab-db"}
    assert list(ctx.saved) == ["lab-db"]
    assert ctx.groups == [("lab-db", "g1")]


def test_the_copied_command_is_the_dashboards():
    assert conn.ssh_command_line("AbC123", "dev") == \
        "ssh -J easyenv@ssh.easyenv.io dev@AbC123.box.easyenv.io"


@pytest.mark.skipif(subprocess.run(["which", "ssh"], capture_output=True).returncode != 0,
                    reason="no ssh client")
def test_ssh_reads_the_written_host_block(tmp_path):
    nickname = conn.nicknames("my dev", [machine()])["AbC123"]
    data = conn.connection_data(nickname, machine())
    config = tmp_path / "config"
    body = "\n".join(f"    {line}" for line in data["extra_ssh_config"].splitlines())
    # As sshPilot writes it, and run the way sshPilot runs it: by nickname.
    config.write_text(f'Host {nickname}\n    HostName {data["hostname"]}\n'
                      f'    User {data["username"]}\n{body}\n')
    out = subprocess.run(["ssh", "-G", "-F", str(config), nickname],
                         capture_output=True, text=True, check=True).stdout
    proxy = next(l for l in out.splitlines() if l.startswith("proxycommand "))
    assert proxy.endswith("-W %h:%p easyenv@ssh.easyenv.io")
    assert "hostname abc123.box.easyenv.io" in out.lower()
