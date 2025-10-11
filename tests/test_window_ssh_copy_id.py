import importlib
import sys
import types


sys.modules.setdefault("cairo", types.ModuleType("cairo"))


def test_ssh_copy_id_identity_agent_disabled_uses_force(monkeypatch, tmp_path):
    window_mod = importlib.import_module("sshpilot.window")
    askpass_mod = importlib.import_module("sshpilot.askpass_utils")

    class DummyWidget:
        def __init__(self, *args, **kwargs):
            self.children = []

        def append(self, child):
            self.children.append(child)
            return None

        def connect(self, *args, **kwargs):
            return None

        def __getattr__(self, name):
            def _method(*args, **kwargs):
                return None

            return _method

    class DummyVte:
        def __init__(self):
            self.spawn_env = None
            self.spawn_args = None

        def feed(self, *args, **kwargs):
            return None

        def spawn_async(self, *args):
            self.spawn_args = args
            if len(args) > 3:
                self.spawn_env = args[3]
            return None

        def connect(self, *args, **kwargs):
            return None

        def get_text_range(self, *args, **kwargs):
            return ("",)

    class DummyTerminalWidget:
        last_instance = None

        def __init__(self, connection, config, manager):
            self.connection = connection
            self.config = config
            self.connection_manager = manager
            self.vte = DummyVte()
            DummyTerminalWidget.last_instance = self

        def _set_connecting_overlay_visible(self, *args, **kwargs):
            return None

        def _set_disconnected_banner_visible(self, *args, **kwargs):
            return None

        def set_hexpand(self, *args, **kwargs):
            return None

        def set_vexpand(self, *args, **kwargs):
            return None

        def disconnect(self, *args, **kwargs):
            return None

    class DummyConfig:
        def get_ssh_config(self):
            return {}

    class DummyManager:
        known_hosts_path = None
        identity_agent_disabled = False

        def get_password(self, *args, **kwargs):
            return None

        def get_key_passphrase(self, *args, **kwargs):
            return "cached-secret"

        def store_key_passphrase(self, *args, **kwargs):
            return None

    def fake_idle_add(func, *args, **kwargs):
        func(*args, **kwargs)
        return 1

    forced_env = {
        "SSH_ASKPASS": "/tmp/helper",
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": ":1",
    }
    prefer_env = {
        "SSH_ASKPASS": "/tmp/helper",
        "SSH_ASKPASS_REQUIRE": "prefer",
        "DISPLAY": ":1",
    }

    monkeypatch.setattr(window_mod, "TerminalWidget", DummyTerminalWidget)
    monkeypatch.setattr(
        window_mod,
        "Adw",
        types.SimpleNamespace(
            Window=DummyWidget,
            HeaderBar=DummyWidget,
            MessageDialog=DummyWidget,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        window_mod,
        "Gtk",
        types.SimpleNamespace(
            Box=DummyWidget,
            Label=DummyWidget,
            Button=DummyWidget,
            Align=types.SimpleNamespace(START=0, END=1),
            Orientation=types.SimpleNamespace(VERTICAL=0, HORIZONTAL=1),
        ),
        raising=False,
    )
    monkeypatch.setattr(
        window_mod,
        "GLib",
        types.SimpleNamespace(
            shell_quote=lambda value: value,
            SpawnFlags=types.SimpleNamespace(DEFAULT=0),
            idle_add=fake_idle_add,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        window_mod,
        "Vte",
        types.SimpleNamespace(PtyFlags=types.SimpleNamespace(DEFAULT=0)),
        raising=False,
    )
    monkeypatch.setattr(window_mod, "Config", DummyConfig, raising=False)
    monkeypatch.setattr(window_mod, "ensure_writable_ssh_home", lambda env: None, raising=False)

    monkeypatch.setattr(
        askpass_mod,
        "get_ssh_env_with_forced_askpass",
        lambda: forced_env.copy(),
        raising=False,
    )
    monkeypatch.setattr(
        askpass_mod,
        "get_ssh_env_with_askpass",
        lambda: prefer_env.copy(),
        raising=False,
    )
    monkeypatch.setattr(askpass_mod, "lookup_passphrase", lambda *_: "", raising=False)

    private_path = tmp_path / "id_test"
    private_path.write_text("private")
    public_path = private_path.with_suffix(private_path.suffix + ".pub")
    public_path.write_text("public")

    ssh_key = types.SimpleNamespace(
        private_path=str(private_path),
        public_path=str(public_path),
    )

    connection = types.SimpleNamespace(
        username="demo",
        hostname="example.com",
        host="",
        port=22,
        auth_method=0,
        key_passphrase="",
        keyfile=str(private_path),
        key_select_mode=0,
        identity_agent_disabled=True,
        pubkey_auth_no=False,
        resolved_identity_files=[str(private_path)],
    )

    manager = DummyManager()

    window_instance = window_mod.MainWindow.__new__(window_mod.MainWindow)
    window_instance.connection_manager = manager
    window_instance.config = DummyConfig()

    window_instance._show_ssh_copy_id_terminal_using_main_widget(connection, ssh_key)

    spawned_env = DummyTerminalWidget.last_instance.vte.spawn_env
    assert spawned_env is not None
    assert "SSH_ASKPASS_REQUIRE=force" in spawned_env
