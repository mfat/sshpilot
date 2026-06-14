"""End-to-end: load the EasyEnv example via the REAL loader + REAL PluginHost,
drive its page logic against the bundled stub `easyenv` on PATH, and assert the
full flow (whoami → list → create → add_connection → open_connection) works
through the public API."""

import json
import os
import shutil
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection, ConnectionManager
from sshpilot.groups import GroupManager
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.host import PluginHost

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
EXAMPLE_DIR = os.path.join(REPO, 'sshpilot', 'plugins', 'examples', 'easyenv_workspaces')
STUB_DIR = os.path.join(EXAMPLE_DIR, 'stub')


class FakeConfig:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeTabPage:
    def set_title(self, t): self.title = t
    def set_icon(self, i): self.icon = i


class FakeTabView:
    def __init__(self): self.pages = []
    def append(self, w): p = FakeTabPage(); self.pages.append(p); return p
    def get_pages(self): return list(self.pages)
    def set_selected_page(self, p): self.selected = p


class FakeWindow:
    def __init__(self, cm):
        self.tab_view = FakeTabView()
        self.toast_overlay = types.SimpleNamespace(toasts=[],
            add_toast=lambda t: self.toast_overlay.toasts.append(t))
        self._plugins_menu_section = types.SimpleNamespace(items=[],
            append=lambda label, action: self._plugins_menu_section.items.append((label, action)))
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
    cm.emitted = []
    cm.emit = lambda *a: cm.emitted.append(a)
    cm.store_password = lambda *a, **k: True
    cm.delete_password = lambda *a, **k: True
    # connect_after is what PluginHost uses to subscribe to CM signals.
    cm.connect_after = lambda *a, **k: 0
    return cm


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Put the stub first on PATH and give it an isolated state dir.
    monkeypatch.setenv("PATH", STUB_DIR + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Make sure the stub is executable (git keeps the bit, but be safe).
    os.chmod(os.path.join(STUB_DIR, "easyenv"), 0o755)
    assert shutil.which("easyenv") == os.path.join(STUB_DIR, "easyenv")

    # Fresh registry + ssh plugin-zero so the registry is valid.
    monkeypatch.setattr(registry_mod, "_registry", None)
    from sshpilot.plugins.builtin.ssh_protocol import Plugin as SshPlugin
    SshPlugin().activate(PluginContext(plugin_id="ssh", app_config=None,
                                       connection_manager=None,
                                       protocol_registry=registry_mod.protocol_registry()))

    cm = _make_cm(tmp_path)
    host = PluginHost(connection_manager=cm)
    host.bind_window(FakeWindow(cm))
    host.dispatch_app_started()
    return cm, host


def _load_plugin(cm, host):
    """Instantiate the example plugin against a real per-plugin context."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "easyenv_e2e", os.path.join(EXAMPLE_DIR, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = PluginContext(plugin_id="easyenv-workspaces", app_config=cm.config,
                        connection_manager=cm,
                        protocol_registry=registry_mod.protocol_registry(),
                        host=host)
    plugin = mod.Plugin()
    plugin.activate(ctx)
    return mod, plugin


def test_backend_registered_via_user_plugin(env):
    cm, host = env
    _load_plugin(cm, host)
    assert registry_mod.protocol_registry().get_or_none("easyenv") is not None
    assert registry_mod.protocol_registry().plugin_id_for("easyenv") == "easyenv-workspaces"


def test_whoami_and_list_against_stub(env):
    cm, host = env
    _mod, plugin = _load_plugin(cm, host)
    assert plugin._do_whoami()  # stub seeds a logged-in account
    items = plugin._do_list()
    assert any(w["name"] == "ansible-cluster" for w in items)  # stub's seed workspace


def _provision(plugin, host, name, template, open_first=False):
    """Run the real off-thread enumerate+provision flow synchronously."""
    ws = plugin._do_create(name, template=template)
    machines = plugin._do_machines(ws["id"])
    plugin._provision_workspace(ws["name"], ws["id"], machines, open_first=open_first)
    return ws, machines


def test_create_cluster_makes_group_of_nodes(env):
    cm, host = env
    _mod, plugin = _load_plugin(cm, host)
    gm = host._window.group_manager

    ws, machines = _provision(plugin, host, "ansible-stack", "ansible-ubuntu-cluster")
    assert len(machines) == 4  # control + 3 ubuntu (stub mirrors the real template)

    # A group exists named after the workspace, with one member per node.
    groups = [g for g in gm.get_all_groups() if g["name"] == "EasyEnv: ansible-stack"]
    assert len(groups) == 1
    gid = groups[0]["id"]
    assert len(gm.groups[gid]["connections"]) == 4

    # Each node is a persisted non-SSH 'easyenv' connection with a machine_id.
    easyenv_conns = [c for c in cm.connections if c.protocol == "easyenv"]
    assert len(easyenv_conns) == 4
    assert all(c.data.get("machine_id") for c in easyenv_conns)
    stored = cm.config.settings.get("connections.non_ssh", [])
    assert sum(1 for e in stored if e.get("workspace_id") == ws["id"]) == 4
    # ~/.ssh/config untouched (mesh connections are non-SSH).
    assert open(cm.ssh_config_path).read() == "# empty\n"


def test_open_node_routes_to_terminal(env):
    cm, host = env
    _mod, plugin = _load_plugin(cm, host)
    window = host._window
    _ws, _machines = _provision(plugin, host, "ansible-stack", "ansible-ubuntu-cluster")
    # Open one node by its group-member nickname.
    nick = gm_first_member(host._window.group_manager, "EasyEnv: ansible-stack")
    assert plugin.ctx.open_connection(nick) is True
    assert any(c.nickname == nick for c in window.opened)


def gm_first_member(gm, group_name):
    gid = next(g["id"] for g in gm.get_all_groups() if g["name"] == group_name)
    return gm.groups[gid]["connections"][0]


def test_reprovision_is_idempotent(env):
    cm, host = env
    _mod, plugin = _load_plugin(cm, host)
    gm = host._window.group_manager
    ws = plugin._do_create("ansible-stack", template="ansible-ubuntu-cluster")
    machines = plugin._do_machines(ws["id"])
    plugin._provision_workspace(ws["name"], ws["id"], machines)
    plugin._provision_workspace(ws["name"], ws["id"], machines)  # again
    groups = [g for g in gm.get_all_groups() if g["name"] == "EasyEnv: ansible-stack"]
    assert len(groups) == 1  # no duplicate group
    assert len(gm.groups[groups[0]["id"]]["connections"]) == 4  # no duplicate members
    assert len([c for c in cm.connections if c.protocol == "easyenv"]) == 4


def test_single_machine_no_group(env):
    cm, host = env
    _mod, plugin = _load_plugin(cm, host)
    gm = host._window.group_manager
    ws, machines = _provision(plugin, host, "scratch", "python-dev-single")
    assert len(machines) == 1
    assert not any(g["name"] == "EasyEnv: scratch" for g in gm.get_all_groups())
    conn = next(c for c in cm.connections if c.nickname == "scratch")
    assert conn.protocol == "easyenv" and conn.data.get("machine_id")


def test_build_spawn_argv_per_node(env):
    cm, host = env
    mod, plugin = _load_plugin(cm, host)
    ws, machines = _provision(plugin, host, "ansible-stack", "ansible-ubuntu-cluster")
    nick = gm_first_member(host._window.group_manager, "EasyEnv: ansible-stack")
    conn = cm.find_connection_by_nickname(nick)
    ctx = PluginContext.for_spawn(plugin_id="easyenv-workspaces", app_config=cm.config,
                                  connection_manager=cm,
                                  protocol_registry=registry_mod.protocol_registry())
    spec = mod.EasyEnvBackend().build_spawn(conn, ctx)
    # easyenv machine ssh <machine_id> -w <ws_id>
    assert spec.argv[-5:-3] == ["machine", "ssh"]
    assert spec.argv[-2:] == ["-w", ws["id"]]
    assert spec.argv[0].endswith("easyenv")
