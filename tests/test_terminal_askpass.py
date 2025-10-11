import importlib
import logging
import types

class DummyVte:
    def __init__(self):
        self.last_env_list = None
        self.spawn_calls = []

    def spawn_async(self, *args):
        # env_list is the 4th positional argument
        if len(args) >= 4:
            self.last_env_list = list(args[3]) if args[3] is not None else None
        self.spawn_calls.append(args)
        return None

    def grab_focus(self):
        return None


class _DummyGLib:
    class Error(Exception):
        """Placeholder for GLib.Error"""

    SpawnFlags = types.SimpleNamespace(DEFAULT=0)

    @staticmethod
    def timeout_add_seconds(*_args, **_kwargs):
        return 0

    @staticmethod
    def idle_add(*_args, **_kwargs):
        return None


def test_forced_askpass_without_passphrase(monkeypatch, caplog):
    terminal_mod = importlib.import_module("sshpilot.terminal")
    askpass_mod = importlib.import_module("sshpilot.askpass_utils")

    monkeypatch.setattr(
        terminal_mod,
        "Vte",
        types.SimpleNamespace(
            Pty=types.SimpleNamespace(new_sync=lambda *a, **k: object()),
            PtyFlags=types.SimpleNamespace(DEFAULT=0),
        ),
        raising=False,
    )
    monkeypatch.setattr(terminal_mod, "GLib", _DummyGLib, raising=False)
    monkeypatch.setattr(
        terminal_mod.Adw,
        "Application",
        types.SimpleNamespace(get_default=lambda: None),
        raising=False,
    )

    monkeypatch.delenv("SSH_ASKPASS_REQUIRE", raising=False)
    monkeypatch.delenv("SSH_ASKPASS", raising=False)

    lookup_calls = []
    manager_calls = []

    def fake_forced_env():
        return {
            "SSH_ASKPASS": "/tmp/helper",
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": ":1",
        }

    def fake_lookup(key_path):
        lookup_calls.append(key_path)
        return ""

    monkeypatch.setattr(
        askpass_mod,
        "get_ssh_env_with_forced_askpass",
        fake_forced_env,
        raising=False,
    )
    monkeypatch.setattr(
        askpass_mod,
        "lookup_passphrase",
        fake_lookup,
        raising=False,
    )
    monkeypatch.setattr(
        terminal_mod,
        "lookup_passphrase",
        fake_lookup,
        raising=False,
    )

    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)

    key_path = "/tmp/test-key"

    terminal.connection = types.SimpleNamespace(
        ssh_cmd=None,
        auth_method=2,
        password=None,
        key_passphrase="",
        keyfile=key_path,
        key_select_mode=0,
        identity_agent_disabled=True,
        quick_connect_command="",
        data={},
        forwarding_rules=[],
        hostname="example.com",
        username="demo",
        port=22,
        pubkey_auth_no=False,
        remote_command="",
        local_command="",
        extra_ssh_config="",
    )

    def fake_get_key_passphrase(path):
        manager_calls.append(path)
        return None

    terminal.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        get_password=lambda *a, **k: None,
        known_hosts_path="",
        prepare_key_for_connection=lambda *a, **k: False,
        get_key_passphrase=fake_get_key_passphrase,
        update_connection_status=lambda *a, **k: None,
    )

    terminal.config = types.SimpleNamespace(get_ssh_config=lambda: {})
    terminal.vte = DummyVte()
    terminal._enable_askpass_log_forwarding = lambda *a, **k: None
    terminal.apply_theme = lambda *a, **k: None
    terminal._set_connecting_overlay_visible = lambda *a, **k: None
    terminal._set_disconnected_banner_visible = lambda *a, **k: None
    terminal.emit = lambda *a, **k: None
    terminal.session_id = "session-123"
    terminal.is_connected = False
    terminal._is_quitting = False

    caplog.set_level(logging.DEBUG)

    terminal._setup_ssh_terminal()

    assert lookup_calls == [key_path]
    assert manager_calls == [key_path]

    assert terminal.vte.last_env_list is not None
    env_dict = dict(item.split("=", 1) for item in terminal.vte.last_env_list)

    assert "SSH_ASKPASS" in env_dict
    assert env_dict["SSH_ASKPASS"] == "/tmp/helper"
    assert "SSH_ASKPASS_REQUIRE" not in env_dict

    assert "allowing interactive prompt" in caplog.text
