"""End-to-end (no network): load the EasyEnv example with a fake REST client and
assert it provisions public-IP boxes into standard sshPilot SSH connections
(single -> one connection; multi -> a group), via the public SDK."""

import importlib.util
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import ConnectionManager
from sshpilot.groups import GroupManager
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.host import PluginHost

EXAMPLE = os.path.join(os.path.dirname(__file__), '..', 'sshpilot', 'plugins',
                       'examples', 'easyenv_workspaces', '__init__.py')


class FakeConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeWindow:
    def __init__(self, cm):
        self.tab_view = types.SimpleNamespace(
            pages=[], append=lambda w: None, get_pages=lambda: [],
            set_selected_page=lambda p: None)
        self.toast_overlay = types.SimpleNamespace(
            toasts=[], add_toast=lambda t: self.toast_overlay.toasts.append(t))
        self._plugins_menu_section = types.SimpleNamespace(
            items=[], append=lambda label, action: None)
        self.opened = []
        self.terminal_manager = types.SimpleNamespace(
            connect_to_host=lambda conn: self.opened.append(conn))
        self._actions = {}
        self.connection_manager = cm
        self.group_manager = GroupManager(FakeConfig())
        self.rebuilds = 0

    def show_tab_view(self): pass
    def lookup_action(self, name): return self._actions.get(name)
    def add_action(self, action): self._actions[len(self._actions)] = action
    def rebuild_connection_list(self): self.rebuilds += 1


def _make_cm(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.config = FakeConfig()
    cm.connections = []
    cm.rules = []
    cm.ssh_config = {}
    cm.isolated_mode = False
    cm.ssh_config_path = str(tmp_path / 'ssh_config')
    cm.known_hosts_path = str(tmp_path / 'known_hosts')
    open(cm.ssh_config_path, 'w').write("# empty\n")
    cm.emit = lambda *a: None
    cm.stored_passwords = []
    cm.store_password = lambda host, user, pw: cm.stored_passwords.append((host, user, pw)) or True
    cm.delete_password = lambda *a, **k: True
    cm.connect_after = lambda *a, **k: 0
    # keyring-backed plugin secrets used by ctx.secrets:
    cm._sec = {}
    cm.store_plugin_secret = lambda pid, k, v: cm._sec.__setitem__((pid, k), v) or True
    cm.get_plugin_secret = lambda pid, k: cm._sec.get((pid, k))
    cm.delete_plugin_secret = lambda pid, k: cm._sec.pop((pid, k), None) is not None
    return cm


class FakeClient:
    """Stands in for EasyEnvClient — no network."""
    def __init__(self):
        self._accounts = [{"uuid": "acct-1", "title": "demo@easyenv.io"}]
        self._recipes = [{"uuid": "ubuntu-24-04", "title": "Ubuntu 24.04 LTS"},
                         {"uuid": "python-dev", "title": "Python Dev Env"}]
        self._ws = {}  # uuid -> workspace dict (with boxes)

    def accounts(self): return self._accounts
    def recipes(self, term=""): return self._recipes
    def workspaces(self): return [{"uuid": u, "title": w["title"], "status": w["status"]}
                                  for u, w in self._ws.items()]
    def workspace(self, uuid): return self._ws.get(uuid)
    def start(self, uuid):
        if uuid in self._ws:
            self._ws[uuid]["status"] = "active"
    def stop(self, uuid):
        if uuid in self._ws:
            self._ws[uuid]["status"] = "stopped"
    def delete(self, uuid): self._ws.pop(uuid, None)

    def add_ws(self, uuid, title, boxes, status="active"):
        self._ws[uuid] = {"uuid": uuid, "title": title, "status": status, "boxes": boxes}


def _box(uuid, title, ip, user="easyenv", port=22, pw="secret"):
    return {"uuid": uuid, "title": title, "host_address": ip,
            "ssh_username": user, "ssh_port": port, "vm_password": pw, "status": "started"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(registry_mod, "_registry", None)
    from sshpilot.plugins.builtin.ssh_protocol import Plugin as SshPlugin
    SshPlugin().activate(PluginContext(plugin_id="ssh", app_config=None,
                                       connection_manager=None,
                                       protocol_registry=registry_mod.protocol_registry()))
    cm = _make_cm(tmp_path)
    host = PluginHost(connection_manager=cm)
    host.bind_window(FakeWindow(cm))
    host.dispatch_app_started()

    spec = importlib.util.spec_from_file_location("easyenv_e2e", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = PluginContext(plugin_id="easyenv-workspaces", app_config=cm.config,
                        connection_manager=cm,
                        protocol_registry=registry_mod.protocol_registry(), host=host)
    plugin = mod.Plugin()
    plugin.activate(ctx)
    fake = FakeClient()
    plugin._client = lambda: fake          # inject the fake REST client
    ctx.settings.set("account_uuid", "acct-1")
    return cm, host, plugin, fake, mod


def _ssh_conns(cm):
    return [c for c in cm.connections if getattr(c, "protocol", "ssh") == "ssh"]


def test_single_box_makes_ssh_connection(env):
    cm, host, plugin, _fake, _mod = env
    ws = {"uuid": "w1", "title": "scratch", "status": "active",
          "boxes": [_box("b1", "Ubuntu", "51.15.0.1")]}
    plugin._materialize(ws, open_after=True)

    conn = cm.find_connection_by_nickname("scratch")
    assert conn is not None and conn.protocol == "ssh"
    assert conn.data["hostname"] == "51.15.0.1"
    assert conn.data["username"] == "easyenv"
    assert conn.data["auth_method"] == 1
    # password stored in the keyring for (host, user)
    assert ("51.15.0.1", "easyenv", "secret") in cm.stored_passwords
    # written to ~/.ssh/config with the forced-password ssh options
    cfg = open(cm.ssh_config_path).read()
    assert "Host scratch" in cfg and "51.15.0.1" in cfg
    assert "StrictHostKeyChecking accept-new" in cfg
    assert "PreferredAuthentications password" in cfg
    # opened a terminal for it
    assert any(c.nickname == "scratch" for c in host._window.opened)


def test_multi_box_makes_group(env):
    cm, host, plugin, _fake, _mod = env
    boxes = [_box("b0", "control", "10.0.0.1"),
             _box("b1", "Ubuntu 24.04 LTS", "10.0.0.2"),
             _box("b2", "Ubuntu 24.04 LTS", "10.0.0.3"),
             _box("b3", "Ubuntu 24.04 LTS", "10.0.0.4")]
    ws = {"uuid": "w2", "title": "ansible", "status": "active", "boxes": boxes}
    plugin._materialize(ws, open_after=False)

    gm = host._window.group_manager
    groups = [g for g in gm.get_all_groups() if g["name"] == "EasyEnv: ansible"]
    assert len(groups) == 1
    assert len(gm.groups[groups[0]["id"]]["connections"]) == 4
    sshs = _ssh_conns(cm)
    assert len(sshs) == 4
    # duplicate box titles deduped into unique nicknames
    assert len({c.nickname for c in sshs}) == 4


def test_mesh_only_box_is_skipped_not_connected(env):
    """A NetBird-mesh box (unroutable 'box-…' host) must NOT become a dead SSH
    connection — regression for 'Could not resolve hostname box-…'."""
    cm, host, plugin, _fake, _mod = env
    ws = {"uuid": "w-mesh", "title": "test3", "status": "active",
          "boxes": [_box("b1", "Ubuntu", "box-3VZo6G4A-hAm88YxD")]}
    plugin._materialize(ws, open_after=True)
    assert cm.find_connection_by_nickname("test3") is None
    assert len(_ssh_conns(cm)) == 0
    assert not host._window.opened


def test_mixed_boxes_keep_only_routable(env):
    cm, host, plugin, _fake, _mod = env
    ws = {"uuid": "w-mix", "title": "mixed", "status": "active",
          "boxes": [_box("b1", "public", "51.15.0.9"),
                    _box("b2", "mesh", "box-deadbeef-cafef00d")]}
    plugin._materialize(ws, open_after=False)
    sshs = _ssh_conns(cm)
    assert len(sshs) == 1
    assert sshs[0].data["hostname"] == "51.15.0.9"


def test_row_actions_gated_by_status(env):
    """A terminal (stopped/expired) workspace must not offer Open/Start — only
    Recreate/Delete; running offers Open/Stop/Delete."""
    _cm, _host, plugin, _fake, _mod = env
    assert plugin._row_actions("stopped") == (("Recreate", "recreate"), ("Delete", "delete"))
    assert plugin._row_actions("expired") == (("Recreate", "recreate"), ("Delete", "delete"))
    assert plugin._row_actions("active") == (("Open", "open"), ("Stop", "stop"), ("Delete", "delete"))
    assert plugin._row_actions("provisioning") == (("Start", "start"), ("Delete", "delete"))


def test_friendly_collapses_cannot_be_started(env):
    _cm, _host, _plugin, _fake, mod = env
    exc = mod.EasyEnvError(
        'POST /v1/workspaces/x/start/ -> HTTP 400: '
        '{"errors":[{"message":["Stopped workspace cannot be started."]}]}')
    assert "can't be restarted" in mod._friendly(exc)
    assert mod._is_terminal("stopped") and not mod._is_terminal("active")


def test_recreate_specs_reuse_box_recipes(env):
    """Recreate rebuilds a create body from a terminal workspace's box recipes,
    skipping boxes that have no recipe (legacy mesh-only)."""
    _cm, _host, plugin, _fake, _mod = env
    old = {"title": "lab", "boxes": [
        {"title": "node-a", "recipe": {"uuid": "ubuntu_24_04"}},
        {"title": "node-b", "recipe": {"uuid": "python_dev"}},
        {"title": "legacy", "recipe": None}]}
    specs = plugin._recreate_specs(old, "lab")
    assert specs == [
        {"title": "node-a", "recipe": "ubuntu_24_04", "position": 0},
        {"title": "node-b", "recipe": "python_dev", "position": 1}]
    assert plugin._recreate_specs({"boxes": []}, "lab") == []


def test_update_on_restart_refreshes_host_and_password(env):
    cm, host, plugin, _fake, _mod = env
    ws_a = {"uuid": "w3", "title": "box", "status": "active",
            "boxes": [_box("b1", "Ubuntu", "1.1.1.1", pw="oldpw")]}
    plugin._materialize(ws_a)
    ws_b = {"uuid": "w3", "title": "box", "status": "active",
            "boxes": [_box("b1", "Ubuntu", "2.2.2.2", pw="newpw")]}
    plugin._materialize(ws_b)

    assert len([c for c in cm.connections if c.nickname == "box"]) == 1  # no dup
    conn = cm.find_connection_by_nickname("box")
    assert conn.data["hostname"] == "2.2.2.2"  # refreshed
    assert ("2.2.2.2", "easyenv", "newpw") in cm.stored_passwords


def test_signin_selects_account_and_stores_token(env):
    cm, host, plugin, fake, _mod = env
    plugin._after_signin(fake.accounts())
    assert cm.config.get_setting("plugins.easyenv-workspaces.account_uuid") == "acct-1"


def test_open_starts_stopped_workspace_then_materializes(env):
    cm, host, plugin, fake, _mod = env
    fake.add_ws("w4", "lab", [_box("b1", "Ubuntu", "3.3.3.3")], status="stopped")
    # _open_workspace_async runs a daemon thread; drive the logic synchronously:
    ws = fake.workspace("w4")
    if ws["status"] != "active":
        fake.start("w4")
        ws = plugin._poll_active(fake, "w4")
    plugin._materialize(ws, open_after=True)
    assert fake.workspace("w4")["status"] == "active"
    assert cm.find_connection_by_nickname("lab") is not None
    assert any(c.nickname == "lab" for c in host._window.opened)


def test_ctx_update_connection(env):
    cm, _host, plugin, _fake, _mod = env
    ctx = plugin.ctx
    ctx.add_connection({"protocol": "ssh", "nickname": "u1", "hostname": "1.1.1.1",
                        "username": "x", "port": 22, "password": "p", "auth_method": 1})
    assert ctx.update_connection("u1", {
        "protocol": "ssh", "nickname": "u1", "hostname": "9.9.9.9",
        "username": "x", "port": 22, "password": "p2", "auth_method": 1}) is True
    assert cm.find_connection_by_nickname("u1").data["hostname"] == "9.9.9.9"
    assert ctx.update_connection("missing", {"nickname": "missing"}) is False


def test_recipes_parsed(env):
    cm, host, plugin, _fake, _mod = env
    recipes = plugin._do_recipes()
    names = {r["name"] for r in recipes}
    assert "Ubuntu 24.04 LTS" in names and "Python Dev Env" in names
    plugin._populate_recipes(recipes)
    assert "ubuntu-24-04" in plugin._recipe_values
