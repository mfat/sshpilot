"""Tests for the UI template plugin. Pure logic + event handling against a fake
context; no GTK needed (gi is imported lazily in the page factory)."""

import importlib.util
import os

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "ui_template_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def test_next_count_increments_and_tolerates_junk():
    mod = _load()
    assert mod.next_count(0) == 1
    assert mod.next_count(4) == 5
    assert mod.next_count(-3) == 1
    assert mod.next_count("nope") == 1
    assert mod.next_count(None) == 1


def test_activate_registers_page_and_subscribes():
    mod = _load()
    ctx = _Ctx()
    mod.Plugin().activate(ctx)
    assert "home" in ctx.pages
    assert mod.Events.CONNECTION_CREATED in ctx.subscribed


def test_connection_created_persists_total():
    mod = _load()
    ctx = _Ctx(settings={"total_created": 2})
    mod.Plugin().activate(ctx)
    handler = ctx.subscribed[mod.Events.CONNECTION_CREATED]
    handler(None)
    handler(None)
    assert ctx.settings.get("total_created") == 4
