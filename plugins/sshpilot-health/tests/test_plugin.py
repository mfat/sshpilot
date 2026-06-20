"""Tests for Host Health Dashboard. tcp_check is exercised by monkeypatching
socket; the plugin's shutdown wiring is tested against a fake PluginContext.
No GTK required (gi is imported lazily inside the page factory)."""

import importlib.util
import os

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "health_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    def __init__(self):
        self.subscribed = {}
        self.pages = []
        self.ui = self
        self.events = self

    def register_page(self, page_id, title, icon, factory):
        self.pages.append(page_id)

    def subscribe(self, event, callback):
        self.subscribed[event] = callback

    def list_connections(self):
        return []


def test_tcp_check_up(monkeypatch):
    mod = _load()

    class _Sock:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(mod.socket, "create_connection",
                        lambda addr, timeout=None: _Sock())
    assert mod.tcp_check("example.com", 22) is True


def test_tcp_check_down_on_oserror(monkeypatch):
    mod = _load()

    def _boom(addr, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(mod.socket, "create_connection", _boom)
    assert mod.tcp_check("example.com", 22) is False


def test_tcp_check_empty_host_is_down():
    mod = _load()
    assert mod.tcp_check("", 22) is False


def test_tcp_check_bad_port_defaults_to_22(monkeypatch):
    mod = _load()
    seen = {}

    class _Sock:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _capture(addr, timeout=None):
        seen["addr"] = addr
        return _Sock()

    monkeypatch.setattr(mod.socket, "create_connection", _capture)
    assert mod.tcp_check("h", None) is True
    assert seen["addr"] == ("h", 22)


def test_activate_registers_page_and_shutdown_hook():
    mod = _load()
    ctx = _Ctx()
    plugin = mod.Plugin()
    plugin.activate(ctx)
    assert "health" in ctx.pages
    assert mod.Events.APP_SHUTDOWN in ctx.subscribed


def test_shutdown_sets_stop_flag_and_is_idempotent():
    mod = _load()
    ctx = _Ctx()
    plugin = mod.Plugin()
    plugin.activate(ctx)
    assert plugin._stop.is_set() is False

    # The APP_SHUTDOWN handler stops the workers.
    ctx.subscribed[mod.Events.APP_SHUTDOWN](None)
    assert plugin._stop.is_set() is True

    # deactivate() can run too without error (idempotent).
    plugin.deactivate()
    assert plugin._stop.is_set() is True


def test_supported_flag_false_without_list_connections():
    mod = _load()

    class _Bare:
        def __init__(self):
            self.subscribed = {}
            self.pages = []
            self.ui = self
            self.events = self

        def register_page(self, *a): self.pages.append(a[0])
        def subscribe(self, event, cb): self.subscribed[event] = cb

    plugin = mod.Plugin()
    plugin.activate(_Bare())
    assert plugin._supported is False
