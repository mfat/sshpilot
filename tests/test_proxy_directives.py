import asyncio
import importlib
import sys
import types

from sshpilot.connection_manager import Connection, ConnectionManager



# Ensure an event loop for Connection objects
asyncio.set_event_loop(asyncio.new_event_loop())

def test_parse_and_load_proxy_directives(tmp_path):
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "\n".join(
            [
                "Host proxycmd",
                "    HostName example.com",
                "    ProxyCommand ssh -W %h:%p bastion",
                "",
                "Host proxyjump",
                "    HostName example.com",
                "    ProxyJump bastion",
                "",
                "Host multijump",
                "    HostName example.com",
                "    ProxyJump bast1,bast2",
                "    ForwardAgent yes",
            ]
        )
    )

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(cfg_path)
    cm.load_ssh_config()

    assert len(cm.connections) == 3
    proxy_cmd_conn = next(c for c in cm.connections if c.nickname == "proxycmd")
    proxy_jump_conn = next(c for c in cm.connections if c.nickname == "proxyjump")
    multi_conn = next(c for c in cm.connections if c.nickname == "multijump")

    assert proxy_cmd_conn.proxy_command == "ssh -W %h:%p bastion"
    assert proxy_jump_conn.proxy_jump == ["bastion"]
    assert multi_conn.proxy_jump == ["bast1", "bast2"]
    assert multi_conn.forward_agent is True

async def _connect(conn: Connection):
    await conn.connect()

def test_connection_passes_proxy_options():
    loop = asyncio.get_event_loop()
    conn1 = Connection({"host": "example.com", "proxy_command": "ssh -W %h:%p bastion"})
    conn2 = Connection({"host": "example.com", "proxy_jump": ["bastion"]})
    conn3 = Connection({"host": "example.com", "proxy_jump": ["b1", "b2"], "forward_agent": True})
    loop.run_until_complete(_connect(conn1))
    loop.run_until_complete(_connect(conn2))
    loop.run_until_complete(_connect(conn3))
    assert "ProxyCommand=ssh -W %h:%p bastion" in conn1.ssh_cmd
    assert "ProxyJump=bastion" in conn2.ssh_cmd
    assert "ProxyJump=b1,b2" in conn3.ssh_cmd
    assert "-A" in conn3.ssh_cmd
    assert "ForwardAgent=yes" in conn3.ssh_cmd


def test_terminal_widget_uses_prepared_proxy_command(monkeypatch):
    loop = asyncio.get_event_loop()
    conn = Connection(
        {
            "host": "example.com",
            "username": "alice",
            "proxy_command": "ssh -W %h:%p bastion",
            "proxy_jump": ["b1", "b2"],
        }
    )
    loop.run_until_complete(_connect(conn))

    from sshpilot import terminal as terminal_mod

    class DummyVte:
        def __init__(self):
            self.last_cmd = None

        def spawn_async(self, *args):
            self.last_cmd = list(args[2])

        def grab_focus(self):
            pass

    widget = terminal_mod.TerminalWidget.__new__(terminal_mod.TerminalWidget)
    widget.connection = conn
    widget.config = types.SimpleNamespace(get_ssh_config=lambda: {})
    widget.connection_manager = types.SimpleNamespace(
        get_password=lambda *a, **k: None,
        prepare_key_for_connection=lambda *a, **k: True,
        known_hosts_path="",
    )
    widget.vte = DummyVte()
    widget.apply_theme = lambda *a, **k: None
    widget._show_forwarding_error_dialog = lambda *a, **k: None
    widget._set_connecting_overlay_visible = lambda *a, **k: None
    widget._set_disconnected_banner_visible = lambda *a, **k: None
    def _fail(*_args, **_kwargs):
        raise AssertionError("unexpected failure")

    widget._on_connection_failed = _fail
    widget._on_spawn_complete = lambda *a, **k: None
    widget._fallback_hide_spinner = lambda *a, **k: False
    widget.connecting_bg = types.SimpleNamespace(set_visible=lambda *a, **k: None)
    widget.connecting_box = types.SimpleNamespace(set_visible=lambda *a, **k: None)
    widget._fallback_timer_id = None
    widget._is_quitting = False

    monkeypatch.setattr(
        terminal_mod,
        "get_port_checker",
        lambda: types.SimpleNamespace(get_port_conflicts=lambda ports, addr: []),
    )
    monkeypatch.setattr(
        terminal_mod.Vte,
        "Pty",
        types.SimpleNamespace(new_sync=lambda *a, **k: object()),
        raising=False,
    )
    monkeypatch.setattr(
        terminal_mod.Vte,
        "PtyFlags",
        types.SimpleNamespace(DEFAULT=0),
        raising=False,
    )
    monkeypatch.setattr(
        terminal_mod.GLib,
        "SpawnFlags",
        types.SimpleNamespace(DEFAULT=0),
        raising=False,
    )
    monkeypatch.setattr(terminal_mod.GLib, "timeout_add_seconds", lambda *a, **k: 0, raising=False)
    if not hasattr(terminal_mod.GLib, "source_remove"):
        monkeypatch.setattr(terminal_mod.GLib, "source_remove", lambda *a, **k: None, raising=False)

    widget._setup_ssh_terminal()

    cmd = widget.vte.last_cmd
    assert cmd is not None
    assert any(arg == "ProxyCommand=ssh -W %h:%p bastion" for arg in cmd)
    assert any(arg == "ProxyJump=b1,b2" for arg in cmd)
    assert cmd.count(conn.ssh_cmd[-1]) == 1


def test_terminal_manager_prepares_connection_before_spawn(monkeypatch):
    stub_terminal_module = types.ModuleType("sshpilot.terminal")
    stub_terminal_module.TerminalWidget = object
    monkeypatch.setitem(sys.modules, "sshpilot.terminal", stub_terminal_module)

    stub_preferences = types.ModuleType("sshpilot.preferences")
    stub_preferences.should_hide_external_terminal_options = lambda: False
    monkeypatch.setitem(sys.modules, "sshpilot.preferences", stub_preferences)

    gi_module = types.ModuleType("gi")
    repository_module = types.ModuleType("gi.repository")
    gio_module = types.ModuleType("gi.repository.Gio")
    gio_module.ThemedIcon = types.SimpleNamespace(new=lambda *a, **k: None)

    def immediate_idle_add(callback, *args, **kwargs):
        callback(*args)
        return 1

    glib_module = types.ModuleType("gi.repository.GLib")
    glib_module.idle_add = immediate_idle_add

    class DummyMessageDialog:
        def __init__(self, *args, **kwargs):
            pass

        def add_response(self, *args, **kwargs):
            pass

        def set_default_response(self, *args, **kwargs):
            pass

        def present(self):
            pass

    adw_module = types.ModuleType("gi.repository.Adw")
    adw_module.MessageDialog = DummyMessageDialog

    gdk_module = types.ModuleType("gi.repository.Gdk")
    gdk_module.RGBA = type("RGBA", (), {})
    gdkpixbuf_module = types.ModuleType("gi.repository.GdkPixbuf")

    repository_module.Gio = gio_module
    repository_module.GLib = glib_module
    repository_module.Adw = adw_module
    repository_module.Gdk = gdk_module
    repository_module.GdkPixbuf = gdkpixbuf_module
    gi_module.repository = repository_module

    monkeypatch.setitem(sys.modules, "gi", gi_module)
    monkeypatch.setitem(sys.modules, "gi.repository", repository_module)
    monkeypatch.setitem(sys.modules, "gi.repository.Gio", gio_module)
    monkeypatch.setitem(sys.modules, "gi.repository.GLib", glib_module)
    monkeypatch.setitem(sys.modules, "gi.repository.Adw", adw_module)
    monkeypatch.setitem(sys.modules, "gi.repository.Gdk", gdk_module)
    monkeypatch.setitem(sys.modules, "gi.repository.GdkPixbuf", gdkpixbuf_module)

    monkeypatch.setitem(sys.modules, "cairo", types.SimpleNamespace())
    sys.modules.pop("sshpilot.terminal_manager", None)
    terminal_manager_mod = importlib.import_module("sshpilot.terminal_manager")

    conn = Connection(
        {
            "host": "example.com",
            "username": "bob",
            "proxy_command": "ssh -W %h:%p bastion",
            "proxy_jump": ["b1", "b2"],
        }
    )

    recorded_cmd = {}

    class DummyTerminalWidget:
        def __init__(self, connection, config, connection_manager, group_color=None):
            self.connection = connection
            self.config = config
            self.connection_manager = connection_manager
            self.vte = types.SimpleNamespace(queue_draw=lambda: None)

        def connect(self, *args, **kwargs):
            return 0

        def apply_theme(self):
            pass

        def _connect_ssh(self):
            recorded_cmd["value"] = list(self.connection.ssh_cmd)
            return True

        def disconnect(self):
            pass

    class DummyPage:
        def __init__(self, child):
            self._child = child

        def set_title(self, title):
            self.title = title

        def set_icon(self, icon):
            self.icon = icon

        def get_child(self):
            return self._child

    class DummyTabView:
        def __init__(self):
            self.pages = []

        def append(self, terminal):
            page = DummyPage(terminal)
            self.pages.append(page)
            return page

        def get_page(self, terminal):
            for page in self.pages:
                if page.get_child() is terminal:
                    return page
            return None

        def set_selected_page(self, page):
            self.selected = page

        def close_page(self, page):
            if page in self.pages:
                self.pages.remove(page)

    class DummyConfig:
        def get_setting(self, key, default=None):
            return False

    class DummyWindow:
        def __init__(self):
            self.config = DummyConfig()
            self.connection_manager = types.SimpleNamespace()
            self.tab_view = DummyTabView()
            self.connection_to_terminals = {}
            self.terminal_to_connection = {}
            self.active_terminals = {}

        def show_tab_view(self):
            self.shown = True

    monkeypatch.setattr(terminal_manager_mod, "TerminalWidget", DummyTerminalWidget)

    window = DummyWindow()
    manager = terminal_manager_mod.TerminalManager(window)

    manager.connect_to_host(conn)

    assert "value" in recorded_cmd
    cmd = recorded_cmd["value"]
    assert any(part == "ProxyCommand=ssh -W %h:%p bastion" for part in cmd)
    assert any(part == "ProxyJump=b1,b2" for part in cmd)
    # Ensure target host argument preserved
    assert cmd[-1].endswith("@example.com")
