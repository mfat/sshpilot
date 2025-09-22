import asyncio
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
