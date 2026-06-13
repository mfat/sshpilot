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

    def show_tab_view(self): pass
    def lookup_action(self, name): return self._actions.get(name)
    def add_action(self, action): self._actions[len(self._actions)] = action


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
    assert any(w["name"] == "demo-sandbox" for w in items)  # stub's seed workspace


def test_create_then_connect_creates_easyenv_connection(env):
    cm, host = env
    _mod, plugin = _load_plugin(cm, host)
    window = host._window

    ws = plugin._do_create("review-pr-1284", template="python_devenv")
    assert ws["name"] == "review-pr-1284" and ws["id"]

    plugin._connect_workspace(ws["name"], ws["id"])

    # A non-SSH 'easyenv' connection was persisted with the workspace id.
    conn = next(c for c in cm.connections if c.nickname == "review-pr-1284")
    assert conn.protocol == "easyenv"
    assert conn.data.get("workspace_id") == ws["id"]
    stored = cm.config.settings.get("connections.non_ssh", [])
    assert any(e.get("workspace_id") == ws["id"] for e in stored)
    # ~/.ssh/config untouched.
    assert open(cm.ssh_config_path).read() == "# empty\n"

    # open_connection routed to the terminal manager.
    assert any(c.nickname == "review-pr-1284" for c in window.opened)


def test_build_spawn_argv_for_created_workspace(env):
    cm, host = env
    mod, plugin = _load_plugin(cm, host)
    ws = plugin._do_create("argv-check")
    conn = Connection({"nickname": ws["name"], "protocol": "easyenv",
                       "workspace_id": ws["id"]})
    ctx = PluginContext.for_spawn(plugin_id="easyenv-workspaces", app_config=cm.config,
                                  connection_manager=cm,
                                  protocol_registry=registry_mod.protocol_registry())
    spec = mod.EasyEnvBackend().build_spawn(conn, ctx)
    assert spec.argv[-3:] == ["workspace", "ssh", ws["id"]]
    assert spec.argv[0].endswith("easyenv")
