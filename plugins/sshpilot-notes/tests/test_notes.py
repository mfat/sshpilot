"""Tests for Connection Notes. The NoteStore is pure Python; the plugin's
prune-on-delete behavior is tested against a fake PluginContext. No GTK needed
(gi is imported lazily inside the page factory)."""

import importlib.util
import os

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "notes_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Info:
    def __init__(self, nickname):
        self.nickname = nickname


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

    def notify(self, message, timeout=3):
        pass

    def subscribe(self, event, callback):
        self.subscribed[event] = callback


def test_note_store_set_get_and_clear():
    mod = _load()
    store = mod.NoteStore()
    assert store.get("web") == ""
    assert store.set("web", "hello") is True
    assert store.get("web") == "hello"
    assert store.set("web", "hello") is False        # unchanged
    assert store.set("web", "") is True              # empty clears
    assert store.get("web") == ""


def test_note_store_ignores_bad_initial_data():
    mod = _load()
    store = mod.NoteStore({"web": "ok", "bad": 123, 5: "x", "empty": ""})
    assert store.as_dict() == {"web": "ok"}


def test_note_store_prune_and_prune_missing():
    mod = _load()
    store = mod.NoteStore({"a": "1", "b": "2", "c": "3"})
    assert store.prune("b") is True
    assert store.prune("b") is False
    removed = store.prune_missing(["a"])
    assert removed == 1
    assert store.as_dict() == {"a": "1"}


def test_activate_registers_page_and_subscribes():
    mod = _load()
    ctx = _Ctx()
    mod.Plugin().activate(ctx)
    assert "notes" in ctx.pages
    assert mod.Events.CONNECTION_DELETED in ctx.subscribed


def test_connection_deleted_prunes_note_and_persists():
    mod = _load()
    ctx = _Ctx(settings={"notes": {"web": "keep", "db": "drop"}})
    mod.Plugin().activate(ctx)
    handler = ctx.subscribed[mod.Events.CONNECTION_DELETED]
    handler(_Info("db"))
    assert ctx.settings.get("notes") == {"web": "keep"}
