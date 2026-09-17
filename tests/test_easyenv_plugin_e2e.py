"""End-to-end (no network): load the EasyEnv example with a fake REST client,
press SSH, and check what sshPilot's own host and config writer make of it: an
ordinary SSH connection reached through EasyEnv's ssh gateway with the profile
key (several machines -> a sidebar group).

The plugin keeps its token in the easyenv CLI's config file, so every test here
points ``XDG_CONFIG_HOME`` at a temporary directory. Without that, a sign-out
test would sign the developer's own CLI out.

The removed local ``ConnectionManager`` no longer exists: the plugin talks to a
daemon-backed facade with the current plugin contract
(``add_connection_from_data`` / ``update_connection`` /
``find_connection_by_nickname`` / scoped plugin secrets), so the e2e drives a
host fake implementing exactly that surface plus a fake group store.
"""

import importlib.util
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_model import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.host import PluginHost
from sshpilot.ssh_config_formatter import format_ssh_config_entry

# Derive from the imported package so this works regardless of whether the
# source lives at repo-root sshpilot/ or src/sshpilot/.
EXAMPLE_DIR = os.path.join(os.path.dirname(registry_mod.__file__),
                           'examples', 'easyenv_workspaces')


def _load_example():
    """As sshPilot loads a user plugin: a package, so its relative imports work."""
    name = "easyenv_e2e_example"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(EXAMPLE_DIR, '__init__.py'),
        submodule_search_locations=[EXAMPLE_DIR])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeGroupStore:
    """Host-side group store standing in for the daemon-backed GroupManager:
    find-or-create by display name and synchronous membership moves (the
    plugin only observes groups through the host, never persists them)."""

    def __init__(self):
        self.groups = {}
        self._seq = 0

    def get_all_groups(self):
        return list(self.groups.values())

    def create_group(self, name, color=None):
        target = str(name or "").strip().lower()
        for group in self.groups.values():
            if str(group["name"]).strip().lower() == target:
                return group["id"]
        gid = f"g{self._seq}"
        self._seq += 1
        self.groups[gid] = {
            "id": gid, "name": str(name), "color": color or "",
            "connections": [],
        }
        return gid

    def move_connection(self, nickname, group_id):
        for group in self.groups.values():
            if nickname in group["connections"]:
                group["connections"].remove(nickname)
        if group_id in self.groups and nickname not in self.groups[group_id]["connections"]:
            self.groups[group_id]["connections"].append(nickname)
        return True


class FakeWindow:
    def __init__(self, cm):
        self.tab_view = types.SimpleNamespace(
            pages=[], append=lambda w: None, get_pages=list,
            set_selected_page=lambda p: None)
        self.toast_overlay = types.SimpleNamespace(
            toasts=[], add_toast=lambda t: self.toast_overlay.toasts.append(t))
        self._plugins_menu_section = types.SimpleNamespace(
            items=[], append=lambda label, action: None,
            remove_all=lambda: None)
        self.opened = []
        self.terminal_manager = types.SimpleNamespace(
            connect_to_host=lambda conn: self.opened.append(conn))
        self._actions = {}
        self.connection_manager = cm
        self.group_manager = FakeGroupStore()
        self.rebuilds = 0

    def show_tab_view(self): pass
    def lookup_action(self, name): return self._actions.get(name)
    def add_action(self, action): self._actions[len(self._actions)] = action
    def rebuild_connection_list(self): self.rebuilds += 1


class FakeCM:
    """Host fake for the daemon-backed plugin connection surface: keeps an
    in-memory connection list, persists SSH entries to the fake config file,
    and records keyring password/secret operations (never persisting them)."""

    def __init__(self, tmp_path, config):
        self.config = config
        self.connections = []
        self.ssh_config_path = str(tmp_path / 'ssh_config')
        self.known_hosts_path = str(tmp_path / 'known_hosts')
        with open(self.ssh_config_path, 'w', encoding='utf-8') as fh:
            fh.write("# empty\n")
        self.stored_passwords = []
        self._plugin_secrets = {}

    def connect_after(self, *a, **k):
        return 0

    def add_connection_from_data(self, data):
        # What the daemon keeps, not what the plugin sent. The real service
        # forwards only EDITABLE_CONFIG_FIELDS (plus the core fields and the
        # password, which travels separately), and silently drops the rest. A
        # fake that kept everything let a ProxyCommand the daemon never saw
        # pass every test here while the real app wrote a Host block without it.
        from sshpilot.api.models.connections import EDITABLE_CONFIG_FIELDS
        kept = EDITABLE_CONFIG_FIELDS | {'host', 'password'}
        values = {k: v for k, v in dict(data).items() if k in kept}
        nickname = str(values.get('nickname') or values.get('host') or '').strip()
        if any(c.nickname == nickname for c in self.connections):
            # The daemon's answer, not the ValueError the SDK documents: a
            # plugin written against the documentation and tested against a
            # fake that followed it could never reopen a connection.
            from sshpilot.api.errors import ErrorCode, SshPilotError
            raise SshPilotError(ErrorCode.CONNECTION_ALREADY_EXISTS,
                                "A connection with this nickname already exists")
        conn = Connection(values)
        self.connections.append(conn)
        password = values.get('password')
        if password:
            self.store_password(
                values.get('hostname') or values.get('host') or '',
                values.get('username') or '', password)
        if conn.protocol == 'ssh':
            with open(self.ssh_config_path, 'a', encoding='utf-8') as fh:
                fh.write('\n' + format_ssh_config_entry(values))
        return conn

    def update_connection(self, conn, data):
        from sshpilot.api.models.connections import EDITABLE_CONFIG_FIELDS
        kept = EDITABLE_CONFIG_FIELDS | {'host', 'password'}
        data = {k: v for k, v in dict(data).items() if k in kept}
        conn.update_data(dict(data))
        password = data.get('password')
        if password:
            self.store_password(
                data.get('hostname') or data.get('host') or conn.hostname,
                data.get('username') or conn.username, password)
        return True

    def find_connection_by_nickname(self, nickname):
        return next((c for c in self.connections if c.nickname == nickname), None)

    def store_password(self, host, user, pw):
        self.stored_passwords.append((host, user, pw))
        return True

    def delete_password(self, *a, **k):
        return True

    def store_plugin_secret(self, plugin_id, key, value):
        self._plugin_secrets[(plugin_id, key)] = value
        return True

    def get_plugin_secret(self, plugin_id, key):
        return self._plugin_secrets.get((plugin_id, key))

    def delete_plugin_secret(self, plugin_id, key):
        return self._plugin_secrets.pop((plugin_id, key), None) is not None


class FakeClient:
    """Stands in for the REST client: no network, answers from ``WORKSPACES``."""
    WORKSPACES = {}

    def __init__(self, token, account=None, server=None):
        self.token, self.account = token, account

    def workspace(self, uuid):
        return FakeClient.WORKSPACES[uuid]


class InlineThread:
    def __init__(self, target, daemon=None, name=None):
        self.target = target

    def start(self):
        self.target()


def _box(uuid, title, user="easyenv", pw="secret"):
    return {"uuid": uuid, "title": title, "ssh_username": user, "ssh_port": 2222,
            "vm_password": pw, "status": "started",
            "recipe": {"uuid": "ubuntu_24_04", "title": "Ubuntu 24.04 LTS"}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("EASYENV_SERVER", "EASYENV_TOKEN", "EASYENV_ACCOUNT", "EASYENV_WEB_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(registry_mod, "_registry", None)
    from sshpilot.plugins.builtin.ssh_protocol import Plugin as SshPlugin
    SshPlugin().activate(PluginContext(plugin_id="ssh", app_config=None,
                                       connection_manager=None,
                                       protocol_registry=registry_mod.protocol_registry()))
    config = FakeConfig()
    cm = FakeCM(tmp_path, config)
    host = PluginHost(connection_manager=cm)
    fake_window = FakeWindow(cm)
    host.bind_window(fake_window)
    host.notify_backend_settled()
    host.dispatch_app_started()

    mod = _load_example()
    monkeypatch.setattr(mod.threading, "Thread", InlineThread)
    monkeypatch.setattr(mod.Plugin, "client_class", FakeClient)
    FakeClient.WORKSPACES = {}
    ctx = PluginContext(plugin_id="easyenv-workspaces", app_config=config,
                        connection_manager=cm,
                        protocol_registry=registry_mod.protocol_registry(), host=host)
    plugin = mod.Plugin()
    plugin.activate(ctx)
    plugin.page = types.SimpleNamespace(set_busy=lambda *a: None,
                                        set_status=lambda *a: None)
    mod.cli_config.write_config({"service_token": "tok-1", "default_account": "acct-1"})
    return cm, host, plugin, mod


def _ssh(plugin, mod, ws, machine_uuid):
    FakeClient.WORKSPACES[ws["uuid"]] = ws
    view = mod.model.workspace_view(ws)
    machine = next(m for m in view["machines"] if m["uuid"] == machine_uuid)
    plugin.ssh(view, machine)


def _ssh_conns(cm):
    return [c for c in cm.connections if getattr(c, "protocol", "ssh") == "ssh"]


def test_ssh_saves_the_machine_through_the_gateway_and_opens_it(env):
    cm, host, plugin, mod = env
    _ssh(plugin, mod, {"uuid": "w1", "title": "scratch", "status": "active",
                       "boxes": [_box("AbC123xY", "Ubuntu")]}, "AbC123xY")

    conn = cm.find_connection_by_nickname("scratch")
    assert conn is not None and conn.protocol == "ssh"
    assert conn.data["hostname"] == "AbC123xY.box.easyenv.io"
    assert conn.data["username"] == "easyenv"
    # Port 22 inside the box, not the NAT port the API reports.
    assert conn.data["port"] == 22
    # The gateway takes keys only: no password is stored or offered.
    assert conn.data["auth_method"] == 0
    assert cm.stored_passwords == []
    cfg = open(cm.ssh_config_path).read()
    assert "Host scratch" in cfg
    assert "-W %h:%p easyenv@ssh.easyenv.io" in cfg
    assert "PubkeyAuthentication no" not in cfg
    assert any(c.nickname == "scratch" for c in host._window.opened)


def test_ssh_itself_reads_the_proxy_command_back(env):
    """Not a string match: ask ssh what it would run for the host."""
    import shutil
    import subprocess
    if not shutil.which("ssh"):
        pytest.skip("no ssh client")
    cm, _host, plugin, mod = env
    _ssh(plugin, mod, {"uuid": "w", "title": "My lab", "status": "active",
                       "boxes": [_box("Qw3rTy12", "Ubuntu")]}, "Qw3rTy12")
    # A title with a space becomes a destination ssh accepts.
    out = subprocess.run(["ssh", "-G", "-F", cm.ssh_config_path, "My-lab"],
                         capture_output=True, text=True, check=True).stdout
    effective = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    assert effective["hostname"] == "qw3rty12.box.easyenv.io"  # ssh lowercases this one
    assert effective["proxycommand"].endswith("-W %h:%p easyenv@ssh.easyenv.io")
    assert effective["port"] == "22"


def test_pressing_ssh_again_updates_the_connection(env):
    """sshPilot's daemon answers a second add with SshPilotError, not the
    ValueError the SDK documents; the plugin looks before it adds."""
    cm, host, plugin, mod = env
    ws = {"uuid": "w", "title": "again", "status": "active", "boxes": [_box("R1", "Ubuntu")]}
    _ssh(plugin, mod, ws, "R1")
    ws["boxes"][0]["ssh_username"] = "dev"
    _ssh(plugin, mod, ws, "R1")
    assert [c.nickname for c in _ssh_conns(cm)] == ["again"]
    assert cm.find_connection_by_nickname("again").data["username"] == "dev"
    assert len(host._window.opened) == 2


def test_several_machines_share_a_group(env):
    cm, host, plugin, mod = env
    boxes = [_box("b0", "control"), _box("b1", "node"), _box("b2", "node")]
    ws = {"uuid": "w2", "title": "ansible", "status": "active", "boxes": boxes}
    for uuid in ("b0", "b1", "b2"):
        _ssh(plugin, mod, ws, uuid)

    gm = host._window.group_manager
    groups = [g for g in gm.get_all_groups() if g["name"] == "EasyEnv: ansible"]
    assert len(groups) == 1
    assert sorted(gm.groups[groups[0]["id"]]["connections"]) == [
        "ansible-control", "ansible-node-1", "ansible-node-2"]
    assert {c.data["hostname"] for c in _ssh_conns(cm)} == {
        "b0.box.easyenv.io", "b1.box.easyenv.io", "b2.box.easyenv.io"}


def test_a_machine_gone_from_the_workspace_is_said_not_saved(env):
    cm, _host, plugin, mod = env
    ws = {"uuid": "w", "title": "gone", "status": "active", "boxes": [_box("G1", "Ubuntu")]}
    view = mod.model.workspace_view(ws)
    FakeClient.WORKSPACES["w"] = dict(ws, boxes=[])
    notes = []
    plugin.notify = notes.append
    plugin.refresh = lambda: None
    plugin.ssh(view, view["machines"][0])
    assert _ssh_conns(cm) == []
    assert notes == ["Ubuntu is no longer part of gone"]
