"""Tests for Runbook Snippets. Substitution + store are pure Python; the
plugin's prune-on-delete is tested against a fake context. No GTK needed."""

import importlib.util
import os
import sys

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "runbook_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Info:
    def __init__(self, nickname, host="", username="", port=22):
        self.nickname = nickname
        self.host = host
        self.username = username
        self.port = port


class _Settings:
    def __init__(self, data=None):
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _Ctx:
    def __init__(self, settings=None):
        self.settings = _Settings(settings)
        self.subscribed = {}
        self.pages = []
        self.ui = self
        self.events = self

    def register_page(self, page_id, title, icon, factory):
        self.pages.append(page_id)

    def subscribe(self, event, callback):
        self.subscribed[event] = callback


def test_substitute_known_and_unknown_tokens():
    mod = _load()
    out = mod.substitute("ssh {user}@{host} -p {port} # {nope}",
                         {"user": "root", "host": "h", "port": 22})
    assert out == "ssh root@h -p 22 # {nope}"


def test_substitute_from_connection_vars():
    mod = _load()
    variables = mod.connection_vars(_Info("web", host="10.0.0.1", username="deploy"))
    assert mod.substitute("{user}@{host} ({nickname})", variables) == \
        "deploy@10.0.0.1 (web)"


def test_store_add_get_remove():
    mod = _load()
    store = mod.SnippetStore()
    assert store.add("web", "logs", "journalctl -u {svc}") is True
    assert store.add("web", "", "  ") is False        # empty command ignored
    assert [s["label"] for s in store.get("web")] == ["logs"]
    assert store.remove("web", 0) is True
    assert store.get("web") == []                      # emptied -> key dropped


def test_store_ignores_bad_initial_data():
    mod = _load()
    store = mod.SnippetStore({
        "web": [{"label": "a", "command": "x"}, {"command": ""}, "junk"],
        "bad": "not-a-list",
        5: [],
    })
    assert list(store.as_dict()) == ["web"]
    assert len(store.get("web")) == 1


def test_store_prune_and_prune_missing():
    mod = _load()
    store = mod.SnippetStore({"a": [{"label": "x", "command": "x"}],
                              "b": [{"label": "y", "command": "y"}]})
    assert store.prune("a") is True
    assert store.prune_missing(["b"]) == 0
    assert store.prune_missing([]) == 1
    assert store.as_dict() == {}


def test_activate_registers_and_subscribes():
    mod = _load()
    ctx = _Ctx()
    mod.Plugin().activate(ctx)
    assert "runbook" in ctx.pages
    assert mod.Events.CONNECTION_DELETED in ctx.subscribed


def test_connection_deleted_prunes():
    mod = _load()
    ctx = _Ctx(settings={"snippets": {"web": [{"label": "x", "command": "x"}]}})
    mod.Plugin().activate(ctx)
    ctx.subscribed[mod.Events.CONNECTION_DELETED](_Info("web"))
    assert ctx.settings.get("snippets") == {}
