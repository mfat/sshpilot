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
    prepare_calls = []

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

    def fake_prepare_key_for_connection(path, *_args, **_kwargs):
        prepare_calls.append(path)
        return True

    terminal.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        get_password=lambda *a, **k: None,
        known_hosts_path="",
        prepare_key_for_connection=fake_prepare_key_for_connection,
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
    assert prepare_calls == []

    assert terminal.vte.last_env_list is not None
    env_dict = dict(item.split("=", 1) for item in terminal.vte.last_env_list)

    assert "SSH_ASKPASS" in env_dict
    assert env_dict["SSH_ASKPASS"] == "/tmp/helper"
    assert "SSH_ASKPASS_REQUIRE" not in env_dict

    assert "allowing interactive prompt" in caplog.text


def test_forced_askpass_with_resolved_identity_passphrase(monkeypatch, caplog):
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

    resolved_key_path = "/tmp/resolved-key"

    def fake_lookup(key_path):
        lookup_calls.append(key_path)
        if key_path == resolved_key_path:
            return "secret"
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

    terminal.connection = types.SimpleNamespace(
        ssh_cmd=None,
        auth_method=2,
        password=None,
        key_passphrase="",
        keyfile="",
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
        resolved_identity_files=[resolved_key_path],
    )

    def fake_get_key_passphrase(path):
        manager_calls.append(path)
        return None

    terminal.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        get_password=lambda *a, **k: None,
        known_hosts_path="",
        prepare_key_for_connection=lambda *a, **k: True,
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
    terminal.session_id = "session-456"
    terminal.is_connected = False
    terminal._is_quitting = False
    terminal._resolve_native_identity_candidates = lambda: []

    caplog.set_level(logging.DEBUG)

    terminal._setup_ssh_terminal()

    assert lookup_calls == [resolved_key_path]
    assert manager_calls == []

    assert terminal.vte.last_env_list is not None
    env_dict = dict(item.split("=", 1) for item in terminal.vte.last_env_list)

    assert env_dict["SSH_ASKPASS"] == "/tmp/helper"
    assert env_dict["SSH_ASKPASS_REQUIRE"] == "force"

    assert "allowing interactive prompt" not in caplog.text


def test_prepare_key_skipped_when_identity_agent_disabled(tmp_path):
    terminal_mod = importlib.import_module("sshpilot.terminal")

    terminal_cls = terminal_mod.TerminalWidget
    terminal = terminal_cls.__new__(terminal_cls)

    key_path = tmp_path / "id_test_key"
    key_path.write_text("dummy")

    prepare_calls = []

    terminal.connection_manager = types.SimpleNamespace(
        prepare_key_for_connection=lambda path: prepare_calls.append(path) or True
    )

    connection = types.SimpleNamespace(
        key_select_mode=1,
        keyfile=str(key_path),
        identity_agent_disabled=True,
    )

    terminal.connection = connection
    terminal._resolve_native_identity_candidates = lambda: []

    terminal._prepare_key_for_native_mode()
    assert prepare_calls == []

    connection.identity_agent_disabled = False
    terminal._prepare_key_for_native_mode()
    assert prepare_calls == [str(key_path)]


def test_identity_agent_disabled_with_key_auth_uses_forced_askpass(monkeypatch, tmp_path):
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

    candidate_key = tmp_path / "id_disabled_agent"
    candidate_key.write_text("dummy")

    def fake_forced_env():
        return {
            "SSH_ASKPASS": "/tmp/helper",
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": ":1",
        }

    def fake_lookup(_key_path):
        return "secret"

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

    terminal.connection = types.SimpleNamespace(
        ssh_cmd=None,
        auth_method=0,
        password=None,
        key_passphrase="",
        keyfile=str(candidate_key),
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
        resolved_identity_files=[str(candidate_key)],
    )

    terminal.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        get_password=lambda *a, **k: None,
        known_hosts_path="",
        get_key_passphrase=lambda *_a, **_k: None,
        store_key_passphrase=lambda *_a, **_k: None,
        prepare_key_for_connection=lambda *_a, **_k: False,
        update_connection_status=lambda *a, **k: None,
    )

    terminal.config = types.SimpleNamespace(get_ssh_config=lambda: {})
    terminal.vte = DummyVte()
    terminal._enable_askpass_log_forwarding = lambda *a, **k: None
    terminal.apply_theme = lambda *a, **k: None
    terminal._set_connecting_overlay_visible = lambda *a, **k: None
    terminal._set_disconnected_banner_visible = lambda *a, **k: None
    terminal.emit = lambda *a, **k: None
    terminal.session_id = "session-forced"
    terminal.is_connected = False
    terminal._is_quitting = False

    terminal._setup_ssh_terminal()

    assert terminal.vte.last_env_list is not None
    env_dict = dict(item.split("=", 1) for item in terminal.vte.last_env_list)
    assert env_dict.get("SSH_ASKPASS_REQUIRE") == "force"
    assert env_dict.get("SSH_ASKPASS") == "/tmp/helper"
