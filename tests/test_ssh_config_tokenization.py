import asyncio
import sys
import types


class _DummyGITypeMeta(type):
    def __getattr__(cls, name):
        value = _DummyGITypeMeta(name, (object,), {})
        setattr(cls, name, value)
        return value

    def __call__(cls, *args, **kwargs):
        return object()


class _DummyGIModule(types.ModuleType):
    def __getattr__(self, name):
        value = _DummyGITypeMeta(name, (object,), {})
        setattr(self, name, value)
        return value


def _ensure_gi_stub():
    gi = sys.modules.get("gi")
    if gi is None:
        gi = types.ModuleType("gi")
        gi.require_version = lambda *args, **kwargs: None
        sys.modules["gi"] = gi
    repository = getattr(gi, "repository", None)
    if not isinstance(repository, _DummyGIModule):
        repository = _DummyGIModule("gi.repository")
        gi.repository = repository
        sys.modules["gi.repository"] = repository
    for name in ["Gtk", "Adw", "Gio", "GLib", "GObject", "Gdk", "Pango", "PangoFT2"]:
        submodule = _DummyGIModule(f"gi.repository.{name}")
        setattr(repository, name, submodule)
        sys.modules[f"gi.repository.{name}"] = submodule
    glib_module = sys.modules["gi.repository.GLib"]
    glib_module.get_user_config_dir = lambda: "/tmp"
    glib_module.get_user_data_dir = lambda: "/tmp"
    glib_module.get_home_dir = lambda: "/tmp"
    glib_module.idle_add = lambda *a, **k: None
    repository.GLib = glib_module
    repository.GObject.SignalFlags = types.SimpleNamespace(RUN_FIRST=None)


_ORIGINAL_GI_MODULES = {
    name: sys.modules.get(name)
    for name in ["gi", "gi.repository"] + [f"gi.repository.{n}" for n in ["Gtk", "Adw", "Gio", "GLib", "GObject", "Gdk", "Pango", "PangoFT2"]]
}

_ensure_gi_stub()

from sshpilot.connection_dialog import SSHConnectionValidator
from sshpilot.connection_manager import Connection, ConnectionManager

for name, module in _ORIGINAL_GI_MODULES.items():
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module

if "gi" in sys.modules and "gi.repository" in sys.modules:
    setattr(sys.modules["gi"], "repository", sys.modules["gi.repository"])


def make_cm():
    return ConnectionManager.__new__(ConnectionManager)


def test_parse_host_with_quotes():
    cm = make_cm()
    config = {
        "host": "nick name",
        "hostname": "example.com",
        "user": "user",
    }
    parsed = ConnectionManager.parse_host_config(cm, config)
    assert parsed["nickname"] == "nick name"
    assert parsed["hostname"] == "example.com"
    assert parsed["aliases"] == []


def test_parse_host_without_hostname_defaults_to_alias():
    cm = make_cm()
    config = {
        "host": "localhost",
        "user": "mahdi",
    }
    parsed = ConnectionManager.parse_host_config(cm, config)
    assert parsed["hostname"] == ""
    assert parsed["host"] == "localhost"
    assert parsed["nickname"] == "localhost"


def test_connect_without_hostname_uses_alias(monkeypatch):
    cm = make_cm()
    config = {
        "host": "localhost",
        "user": "mahdi",
    }
    parsed = ConnectionManager.parse_host_config(cm, config)
    monkeypatch.setattr(
        "sshpilot.connection_manager.get_effective_ssh_config",
        lambda alias, config_file=None: {"hostname": ""},
    )
    parsed["hostname"] = ""
    connection = Connection(parsed)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        connected = loop.run_until_complete(connection.connect())
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())

    assert connected
    assert connection.ssh_cmd[-1].endswith("localhost")
    assert connection.hostname == ""
    assert connection.host == "localhost"


def test_connection_host_preserves_alias_when_hostname_blank():
    data = {
        "host": "alias",
        "hostname": "",
        "username": "user",
    }
    connection = Connection(data)
    assert connection.data["host"] == "alias"
    assert connection.host == "alias"
    assert connection.hostname == ""


def test_connection_update_preserves_alias_when_hostname_blank():
    data = {
        "host": "alias",
        "hostname": "",
        "username": "user",
    }
    connection = Connection(data)
    # Update with new username but keep hostname blank
    connection.update_data({"username": "newuser", "hostname": ""})
    assert connection.host == "alias"
    assert connection.hostname == ""



def test_connect_with_blank_hostname_uses_alias(monkeypatch):
    data = {
        "host": "myalias",
        "hostname": "",
        "username": "mahdi",
    }
    monkeypatch.setattr(
        "sshpilot.connection_manager.get_effective_ssh_config",
        lambda alias, config_file=None: {},
    )
    connection = Connection(data)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        connected = loop.run_until_complete(connection.connect())
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())

    assert connected
    assert connection.ssh_cmd[-1] == "mahdi@myalias"
    assert connection.host == "myalias"
    assert connection.hostname == ""


def test_update_connection_password_storage_uses_alias(monkeypatch):
    cm = make_cm()
    cm.config = types.SimpleNamespace()
    cm.ssh_config_path = ""
    cm.connections = []
    cm.rules = []
    cm.loop = asyncio.new_event_loop()
    cm.active_connections = {}
    cm.ssh_config = {}
    cm.known_hosts_path = ""
    cm.emit = lambda *args, **kwargs: None

    stored = {}

    def fake_store(host, user, password):
        stored["host"] = host
        stored["user"] = user
        stored["password"] = password

    def fake_delete(host, user):
        stored.setdefault("deleted", []).append((host, user))

    def fake_update_ssh_config(connection, new_data, original_nickname):
        return None

    cm.store_password = fake_store
    cm.delete_password = fake_delete
    cm.update_ssh_config_file = fake_update_ssh_config

    connection = Connection({
        "host": "alias",
        "hostname": "",
        "username": "user",
        "password": "",
    })

    new_data = {
        "password": "secret",
        "hostname": "",
    }

    try:
        result = cm.update_connection(connection, dict(new_data))
    finally:
        cm.loop.close()

    assert result
    assert stored["host"] == "alias"
    assert stored["user"] == "user"
    assert stored["password"] == "secret"


def test_format_host_requotes():
    cm = make_cm()
    data = {
        "nickname": "nick name",
        "hostname": "example.com",
        "username": "user",
    }
    entry = ConnectionManager.format_ssh_config_entry(cm, data)
    assert entry.splitlines()[0] == 'Host "nick name"'


def test_connection_name_rejects_whitespace():
    validator = SSHConnectionValidator()
    result = validator.validate_connection_name("nick name")
    assert not result.is_valid
    assert "whitespace" in result.message.lower()
