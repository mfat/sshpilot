import sys
import sys
import types
from typing import Optional


def _ensure_gi_stubs():
    gi = sys.modules.get("gi")
    if gi is None:
        gi = types.ModuleType("gi")
        sys.modules["gi"] = gi
    if not hasattr(gi, "require_version"):
        gi.require_version = lambda *args, **kwargs: None

    repository = getattr(gi, "repository", None)
    if repository is None:
        repository = types.SimpleNamespace()
        gi.repository = repository
        sys.modules["gi.repository"] = repository

    def ensure_repo_module(name, default):
        module = getattr(repository, name, None)
        if module is None:
            module = default
            setattr(repository, name, module)
        sys.modules.setdefault(f"gi.repository.{name}", module)
        return module

    Gtk = ensure_repo_module(
        "Gtk",
        types.SimpleNamespace(Box=type("Box", (), {})),
    )
    if not hasattr(Gtk, "Box"):
        Gtk.Box = type("Box", (), {})

    GObject = ensure_repo_module(
        "GObject",
        types.SimpleNamespace(
            SignalFlags=types.SimpleNamespace(RUN_FIRST=0)
        ),
    )
    if not hasattr(GObject, "SignalFlags"):
        GObject.SignalFlags = types.SimpleNamespace(RUN_FIRST=0)

    GLib = ensure_repo_module(
        "GLib",
        types.SimpleNamespace(
            idle_add=lambda *a, **k: None,
            SpawnFlags=types.SimpleNamespace(DEFAULT=0),
        ),
    )
    if not hasattr(GLib, "idle_add"):
        GLib.idle_add = lambda *a, **k: None
    if not hasattr(GLib, "SpawnFlags"):
        GLib.SpawnFlags = types.SimpleNamespace(DEFAULT=0)
    elif not hasattr(GLib.SpawnFlags, "DEFAULT"):
        GLib.SpawnFlags.DEFAULT = 0

    Vte = ensure_repo_module(
        "Vte",
        types.SimpleNamespace(
            Pty=types.SimpleNamespace(new_sync=lambda *a, **k: object())
        ),
    )
    if not hasattr(Vte, "Pty"):
        Vte.Pty = types.SimpleNamespace(new_sync=lambda *a, **k: object())
    elif not hasattr(Vte.Pty, "new_sync"):
        Vte.Pty.new_sync = lambda *a, **k: object()

    ensure_repo_module("Pango", types.SimpleNamespace())
    ensure_repo_module("Gdk", types.SimpleNamespace())
    ensure_repo_module("Gio", types.SimpleNamespace())

    class _DummyApplication:
        @staticmethod
        def get_default():
            return None

    Adw = ensure_repo_module(
        "Adw",
        types.SimpleNamespace(
            Application=_DummyApplication,
            Toast=types.SimpleNamespace(new=lambda *a, **k: None),
        ),
    )
    if not hasattr(Adw, "Application"):
        Adw.Application = _DummyApplication
    elif not hasattr(Adw.Application, "get_default"):
        Adw.Application.get_default = staticmethod(lambda: None)
    if not hasattr(Adw, "Toast"):
        Adw.Toast = types.SimpleNamespace(new=lambda *a, **k: None)


_ensure_gi_stubs()


from sshpilot import askpass_utils
from sshpilot import terminal as terminal_mod


class _DummyVte:
    def __init__(self):
        self.spawn_calls = []

    def grab_focus(self):
        return None

    def spawn_async(
        self,
        _pty_flags,
        _cwd,
        argv,
        env_list,
        _spawn_flags,
        _child_setup,
        _child_setup_data,
        _timeout,
        _cancellable,
        _callback,
        _user_data,
    ):
        self.spawn_calls.append((argv, env_list))

    def feed(self, _data, _length):
        return None


def _build_terminal_widget(monkeypatch, tmp_path, passphrase: Optional[str]):
    terminal_cls = terminal_mod.TerminalWidget
    widget = terminal_cls.__new__(terminal_cls)

    key_file = tmp_path / "id_rsa"
    key_file.write_text("dummy-key", encoding="utf-8")

    connection = types.SimpleNamespace()
    connection.auth_method = 0
    connection.password = None
    connection.key_select_mode = 1
    connection.keyfile = str(key_file)
    connection.certificate = None
    connection.x11_forwarding = False
    connection.pubkey_auth_no = False
    connection.remote_command = ""
    connection.local_command = ""
    connection.quick_connect_command = ""
    connection.username = "test-user"
    connection.hostname = "example.com"
    connection.host = "example.com"
    connection.nickname = "example"
    connection.data = {}
    connection.get_effective_host = lambda: "example.com"
    connection.resolve_host_identifier = lambda: "example.com"

    widget.connection = connection

    def fake_get_password(*_args, **_kwargs):
        return None

    def fake_prepare_key_for_connection(_path):
        return True

    lookup_calls = []

    def get_key_passphrase(path):
        lookup_calls.append(path)
        if passphrase is None:
            return None
        resolved = str(key_file)
        return passphrase if path == resolved else None

    widget.connection_manager = types.SimpleNamespace(
        native_connect_enabled=False,
        known_hosts_path="",
        get_password=fake_get_password,
        prepare_key_for_connection=fake_prepare_key_for_connection,
        get_key_passphrase=get_key_passphrase,
    )

    widget.config = types.SimpleNamespace(get_ssh_config=lambda: {})
    widget.vte = _DummyVte()
    widget._is_quitting = False
    widget._passphrase_lookup_calls = lookup_calls

    monkeypatch.setattr(
        terminal_mod.Vte.Pty,
        "new_sync",
        lambda *_args, **_kwargs: object(),
        raising=False,
    )
    monkeypatch.setattr(
        terminal_cls,
        "_enable_askpass_log_forwarding",
        lambda self, include_existing=False: None,
        raising=False,
    )

    return widget


def test_saved_passphrase_forces_askpass(monkeypatch, tmp_path):
    forced_calls = []

    def fake_forced():
        forced_calls.append(True)
        return {"SSH_ASKPASS": "/tmp/mock", "SSH_ASKPASS_REQUIRE": "force"}

    def fake_prefer():
        return {"SSH_ASKPASS": "/tmp/mock", "SSH_ASKPASS_REQUIRE": "prefer"}

    monkeypatch.setattr(askpass_utils, "get_ssh_env_with_forced_askpass", fake_forced)
    monkeypatch.setattr(askpass_utils, "get_ssh_env_with_askpass", fake_prefer)

    widget = _build_terminal_widget(monkeypatch, tmp_path, passphrase="secret")

    widget._setup_ssh_terminal()

    assert widget._passphrase_lookup_calls
    assert str((tmp_path / "id_rsa")) in widget._passphrase_lookup_calls
    assert widget.vte.spawn_calls, "spawn_async should be invoked"
    assert forced_calls, "Forced askpass env should be requested when passphrase is saved"
    _argv, env_list = widget.vte.spawn_calls[-1]
    env_dict = {}
    for item in env_list:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            env_dict[key] = value

    assert env_dict.get("SSH_ASKPASS_REQUIRE") == "force"


def test_no_saved_passphrase_uses_preferred(monkeypatch, tmp_path):
    forced_calls = []

    def fake_forced():
        forced_calls.append(True)
        return {"SSH_ASKPASS": "/tmp/mock", "SSH_ASKPASS_REQUIRE": "force"}

    prefer_calls = []

    def fake_prefer():
        prefer_calls.append(True)
        return {"SSH_ASKPASS": "/tmp/mock", "SSH_ASKPASS_REQUIRE": "prefer"}

    monkeypatch.setattr(askpass_utils, "get_ssh_env_with_forced_askpass", fake_forced)
    monkeypatch.setattr(askpass_utils, "get_ssh_env_with_askpass", fake_prefer)

    widget = _build_terminal_widget(monkeypatch, tmp_path, passphrase=None)

    widget._setup_ssh_terminal()

    assert widget._passphrase_lookup_calls
    assert str((tmp_path / "id_rsa")) in widget._passphrase_lookup_calls
    assert widget.vte.spawn_calls, "spawn_async should be invoked"
    assert not forced_calls, "Forced askpass env should not be requested without passphrase"
    assert not prefer_calls, "Interactive prompts should be preserved when no credentials are stored"
    _argv, env_list = widget.vte.spawn_calls[-1]
    env_dict = {}
    for item in env_list:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            env_dict[key] = value

    assert "SSH_ASKPASS_REQUIRE" not in env_dict
