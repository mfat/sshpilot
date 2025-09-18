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
    repository.GObject.SignalFlags = types.SimpleNamespace(RUN_FIRST=None)
    repository.GLib.idle_add = lambda *a, **k: None


_ORIGINAL_GI_MODULES = {
    name: sys.modules.get(name)
    for name in ["gi", "gi.repository"] + [f"gi.repository.{n}" for n in ["Gtk", "Adw", "Gio", "GLib", "GObject", "Gdk", "Pango", "PangoFT2"]]
}

_ensure_gi_stub()

from sshpilot.connection_dialog import SSHConnectionValidator
from sshpilot.connection_manager import ConnectionManager

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
