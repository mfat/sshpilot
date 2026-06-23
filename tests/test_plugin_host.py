"""Tests for the public plugin SDK: event bus, UI host (deferred + drain),
PluginHost bridges, and the per-plugin PluginContext facades."""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.host import (
    ConnectionInfo,
    EventBus,
    Events,
    PluginHost,
    SessionInfo,
    UiHost,
)


# Isolate these tests from cross-test `gi` pollution: the stub's
# Gio.SimpleAction has no .connect and another test may swap Adw.Toast for a
# stub lacking .new. Install connect-able / new-able fakes so the UI paths
# (menu install, toast) behave deterministically regardless of suite order.
@pytest.fixture(autouse=True)
def _patch_gi():
    from gi.repository import Gio, Adw

    class _FakeAction:
        def __init__(self, name):
            self.name = name

        def connect(self, *_a, **_k):
            return 0

    class _FakeSimpleAction:
        @staticmethod
        def new(name, _ptype):
            return _FakeAction(name)

    class _FakeToast:
        def __init__(self, message):
            self.message = message

        @staticmethod
        def new(message):
            return _FakeToast(message)

        def set_timeout(self, _t):
            pass

    saved_action = getattr(Gio, "SimpleAction", None)
    saved_toast = getattr(Adw, "Toast", None)
    Gio.SimpleAction = _FakeSimpleAction
    Adw.Toast = _FakeToast
    try:
        yield
    finally:
        if saved_action is not None:
            Gio.SimpleAction = saved_action
        if saved_toast is not None:
            Adw.Toast = saved_toast


# --- fakes ----------------------------------------------------------------

class FakeConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


class FakeConn:
    def __init__(self, nickname, host="h", username="u", protocol="ssh", port=22):
        self.nickname = nickname
        self.hostname = host
        self.host = host
        self.username = username
        self.protocol = protocol
        self.port = port


class FakeCM:
    """Minimal ConnectionManager: records secret calls, resolves nicknames,
    and lets tests drive the bridge handlers directly."""

    def __init__(self):
        self.secrets = {}
        self.connect_calls = 0
        self.connections = []

    # GObject-ish connect_after: count subscriptions (for idempotency test).
    # (The real ConnectionManager overrides connect(), so the host uses
    # connect_after — mirror that here.)
    def connect_after(self, signal, handler):
        self.connect_calls += 1
        return self.connect_calls

    def store_plugin_secret(self, pid, key, value):
        self.secrets[(pid, key)] = value
        return True

    def get_plugin_secret(self, pid, key):
        return self.secrets.get((pid, key))

    def delete_plugin_secret(self, pid, key):
        return self.secrets.pop((pid, key), None) is not None

    def find_connection_by_nickname(self, nickname):
        for c in self.connections:
            if c.nickname == nickname:
                return c
        return None


class FakeTabPage:
    def __init__(self, child):
        self.child = child
        self.title = None
        self.icon = None

    def set_title(self, t):
        self.title = t

    def set_icon(self, i):
        self.icon = i


class FakeTabView:
    def __init__(self):
        self.pages = []

    def append(self, widget):
        page = FakeTabPage(widget)
        self.pages.append(page)
        return page

    def get_pages(self):
        return list(self.pages)

    def set_selected_page(self, page):
        self.selected = page


class FakeToastOverlay:
    def __init__(self):
        self.toasts = []

    def add_toast(self, toast):
        self.toasts.append(toast)


class FakeMenuSection:
    def __init__(self):
        self.items = []

    def append(self, label, action):
        self.items.append((label, action))


class FakeTerminalManager:
    def __init__(self):
        self.opened = []

    def connect_to_host(self, conn):
        self.opened.append(conn)


class FakeKeyManager:
    def generate_key(self, name, **kw):
        return types.SimpleNamespace(private_path=f"/keys/{name}")


class FakeWindow:
    def __init__(self):
        self.tab_view = FakeTabView()
        self.toast_overlay = FakeToastOverlay()
        self._plugins_menu_section = FakeMenuSection()
        self.terminal_manager = FakeTerminalManager()
        self.key_manager = FakeKeyManager()
        self._actions = {}
        self.shown_tab_view = 0

    def show_tab_view(self):
        self.shown_tab_view += 1

    def lookup_action(self, name):
        return self._actions.get(name)

    def add_action(self, action):
        # Gio.SimpleAction is stubbed; key by identity count.
        self._actions[f"action-{len(self._actions)}"] = action


# --- EventBus -------------------------------------------------------------

def test_event_bus_dispatch_and_isolation():
    bus = EventBus()
    seen = []

    def bad(_p):
        raise RuntimeError("boom")

    def good(p):
        seen.append(p)

    bus.subscribe(Events.APP_STARTED, bad, plugin_id="a")
    bus.subscribe(Events.APP_STARTED, good, plugin_id="b")
    bus.emit(Events.APP_STARTED, "payload")  # must not raise
    assert seen == ["payload"]  # good ran despite bad raising


def test_event_bus_unknown_event_rejected():
    bus = EventBus()
    with pytest.raises(ValueError):
        bus.subscribe("not_an_event", lambda p: None, plugin_id="a")


def test_event_bus_unsubscribe_and_unsubscribe_plugin():
    bus = EventBus()
    calls = []
    cb = lambda p: calls.append(p)
    bus.subscribe(Events.APP_STARTED, cb, plugin_id="a")
    bus.unsubscribe(Events.APP_STARTED, cb, plugin_id="a")
    bus.emit(Events.APP_STARTED, 1)
    assert calls == []

    bus.subscribe(Events.APP_STARTED, cb, plugin_id="a")
    bus.subscribe(Events.APP_SHUTDOWN, cb, plugin_id="a")
    bus.unsubscribe_plugin("a")
    bus.emit(Events.APP_STARTED, 1)
    bus.emit(Events.APP_SHUTDOWN, 2)
    assert calls == []


# --- UiHost: deferred registration + drain --------------------------------

def test_ui_host_defers_then_drains_on_bind():
    ui = UiHost()
    built = []
    ui.register_page("p:deploy", "Deploy", "icon", lambda: built.append(1) or "WIDGET",
                     plugin_id="p")
    ui.open_page("p:deploy")   # before bind: queued, factory NOT called
    ui.notify("hello")         # before bind: queued
    assert built == []

    window = FakeWindow()
    ui.bind_window(window)
    assert built == [1]                      # factory called once on drain
    assert len(window.tab_view.pages) == 1   # page appended
    assert window.tab_view.pages[0].title == "Deploy"
    assert window._plugins_menu_section.items  # menu item installed
    assert len(window.toast_overlay.toasts) == 1  # queued toast drained


def test_ui_host_page_ids_for_plugin():
    ui = UiHost()
    ui.register_page("p:deploy", "Deploy", "icon", lambda: "W", plugin_id="p")
    ui.register_page("p:logs", "Logs", "icon", lambda: "W", plugin_id="p")
    ui.register_page("q:home", "Home", "icon", lambda: "W", plugin_id="q")
    assert set(ui.page_ids_for_plugin("p")) == {"p:deploy", "p:logs"}
    assert ui.page_ids_for_plugin("q") == ["q:home"]
    assert ui.page_ids_for_plugin("missing") == []


def test_ui_host_reopen_focuses_without_rebuilding():
    ui = UiHost()
    built = []
    ui.register_page("p:deploy", "Deploy", "icon", lambda: built.append(1) or "W",
                     plugin_id="p")
    window = FakeWindow()
    ui.bind_window(window)
    ui.open_page("p:deploy")  # already drained one open at bind? no — none queued
    # first real open builds:
    assert built == [1]
    assert len(window.tab_view.pages) == 1
    ui.open_page("p:deploy")  # re-open: focus existing, no rebuild
    assert built == [1]
    assert len(window.tab_view.pages) == 1


def test_ui_host_open_page_with_on_activate_delegates():
    ui = UiHost()
    built = []
    activated = []
    ui.register_page(
        "p:redirect", "Redirect", "icon", lambda: built.append(1),
        plugin_id="p", on_activate=lambda: activated.append(1),
    )
    window = FakeWindow()
    ui.bind_window(window)
    ui.open_page("p:redirect")
    assert activated == [1]
    assert built == []
    assert len(window.tab_view.pages) == 0


def test_ui_host_factory_returning_none_shows_toast():
    ui = UiHost()
    ui.register_page("p:empty", "Empty", "icon", lambda: None, plugin_id="p")
    window = FakeWindow()
    ui.bind_window(window)
    ui.open_page("p:empty")
    assert len(window.tab_view.pages) == 0
    assert len(window.toast_overlay.toasts) == 1


def test_ui_host_activate_time_calls_do_not_crash():
    ui = UiHost()
    # No window bound: these must queue, never raise.
    ui.notify("x")
    ui.open_page("unknown")  # unknown id just logs
    # binding later is still fine
    ui.bind_window(FakeWindow())


# --- PluginHost: bridges, sessions, lifecycle, services -------------------

def _host_with_window():
    cm = FakeCM()
    host = PluginHost(connection_manager=cm)
    window = FakeWindow()
    host.bind_window(window)
    return host, cm, window


def test_connection_bridges_emit_stable_payloads():
    host, cm, _ = _host_with_window()
    events = []
    host.events.subscribe(Events.CONNECTION_CREATED, lambda i: events.append(("c", i)), plugin_id="p")
    host.events.subscribe(Events.CONNECTION_UPDATED, lambda i: events.append(("u", i)), plugin_id="p")
    host.events.subscribe(Events.CONNECTION_DELETED, lambda i: events.append(("d", i)), plugin_id="p")

    conn = FakeConn("box1", host="1.2.3.4", port=2222)
    host._on_cm_updated(cm, conn)   # persist step
    host._on_cm_added(cm, conn)     # creation (documented: update then created)
    host._on_cm_removed(cm, conn)

    kinds = [k for k, _ in events]
    assert kinds == ["u", "c", "d"]
    info = events[1][1]
    assert isinstance(info, ConnectionInfo)
    assert info.nickname == "box1" and info.host == "1.2.3.4" and info.port == 2222


def test_session_dispatch_and_reconnect_dedupe():
    host, _, _ = _host_with_window()
    opened, closed = [], []
    host.events.subscribe(Events.SESSION_OPENED, lambda i: opened.append(i), plugin_id="p")
    host.events.subscribe(Events.SESSION_CLOSED, lambda i: closed.append(i), plugin_id="p")

    term = types.SimpleNamespace(connection=FakeConn("box1"))
    host.dispatch_session_opened(term)
    host.dispatch_session_opened(term)  # reconnect of same terminal → no re-emit
    assert len(opened) == 1
    sid = opened[0].session_id

    host.dispatch_session_closed(term)
    assert len(closed) == 1
    assert closed[0].session_id == sid  # stable id across the pair


def test_app_lifecycle_events():
    host, _, _ = _host_with_window()
    fired = []
    host.events.subscribe(Events.APP_STARTED, lambda p: fired.append("start"), plugin_id="p")
    host.events.subscribe(Events.APP_SHUTDOWN, lambda p: fired.append("stop"), plugin_id="p")
    host.dispatch_app_started()
    host.dispatch_app_shutdown()
    assert fired == ["start", "stop"]


def test_open_connection_resolution():
    host, cm, window = _host_with_window()
    cm.connections.append(FakeConn("box1"))
    assert host.open_connection("box1") is True
    assert len(window.terminal_manager.opened) == 1
    # unknown → False + toast, no crash
    assert host.open_connection("nope") is False
    assert window.toast_overlay.toasts  # notified


def test_generate_key_returns_path():
    host, _, _ = _host_with_window()
    assert host.generate_key("k1") == "/keys/k1"


def test_run_on_ui_thread_runs_inline_on_main_thread():
    host, _, _ = _host_with_window()
    result = []
    host.run_on_ui_thread(lambda x: result.append(x), 42)
    assert result == [42]


def test_bind_window_idempotent():
    cm = FakeCM()
    host = PluginHost(connection_manager=cm)
    host.bind_window(FakeWindow())
    host.bind_window(FakeWindow())
    assert cm.connect_calls == 3  # exactly one bind connected the 3 CM signals


# --- ConnectionInfo decoupling --------------------------------------------

def test_connection_info_is_frozen_snapshot():
    import dataclasses
    conn = FakeConn("box1", host="1.1.1.1")
    info = ConnectionInfo.from_connection(conn)
    conn.nickname = "changed"          # mutate source afterwards
    assert info.nickname == "box1"     # snapshot unaffected
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.nickname = "x"


# --- PluginContext facades scoping ----------------------------------------

def test_context_facades_are_scoped_by_plugin_id():
    cm = FakeCM()
    cfg = FakeConfig()
    host = PluginHost(connection_manager=cm)
    ctx = PluginContext(plugin_id="acme", app_config=cfg, connection_manager=cm,
                        protocol_registry=registry_mod.ProtocolRegistry(), host=host)
    assert ctx.plugin_id == "acme"

    ctx.secrets.set("token", "v")
    assert cm.secrets == {("acme", "token"): "v"}
    assert ctx.secrets.get("token") == "v"
    assert ctx.secrets.delete("token") is True

    ctx.settings.set("region", "fra1")
    assert cfg.settings == {"plugins.acme.region": "fra1"}
    assert ctx.settings.get("region") == "fra1"
    assert ctx.settings.get("missing", "d") == "d"

    # ui/events facades route to the shared host with namespaced page ids.
    seen = []
    ctx.events.subscribe(Events.APP_STARTED, lambda p: seen.append(p))
    host.dispatch_app_started()
    assert seen == [None]

    ctx.ui.register_page("deploy", "Deploy", "icon", lambda: "W")
    host.bind_window(FakeWindow())
    ctx.ui.open_page("deploy")  # namespaced internally to "acme:deploy"


def test_context_without_host_is_safe():
    cm = FakeCM()
    ctx = PluginContext(plugin_id="x", app_config=FakeConfig(), connection_manager=cm,
                        protocol_registry=registry_mod.ProtocolRegistry(), host=None)
    assert ctx.events is None and ctx.ui is None
    assert ctx.open_connection("any") is False
    assert ctx.generate_key("k") is None
    ran = []
    ctx.run_on_ui_thread(lambda: ran.append(1))  # falls back to inline
    assert ran == [1]


# --- loader builds per-plugin contexts ------------------------------------

def test_loader_builds_per_plugin_context(monkeypatch):
    monkeypatch.setattr(registry_mod, "_registry", None)
    from sshpilot.plugins import loader as loader_mod

    seen_ids = []

    class _RecordingPlugin:
        def __init__(self, pid):
            self._pid = pid

        def activate(self, ctx):
            seen_ids.append((self._pid, ctx.plugin_id, ctx.events is not None))

    # Drive _load_builtin with a fake make_ctx + monkeypatched discovery.
    cm = FakeCM()
    host = PluginHost(connection_manager=cm)
    cfg = FakeConfig()

    captured = {}

    def fake_load_builtin(make_ctx, disabled):
        for pid in ("ssh", "telnet"):
            ctx = make_ctx(pid)
            captured[pid] = ctx
            _RecordingPlugin(pid).activate(ctx)
        return []

    monkeypatch.setattr(loader_mod, "_load_builtin", fake_load_builtin)
    monkeypatch.setattr(loader_mod, "_load_user", lambda make_ctx, enabled: [])
    # Avoid the ssh-required RuntimeError by registering a dummy ssh backend.
    monkeypatch.setattr(loader_mod, "protocol_registry",
                        lambda: types.SimpleNamespace(get_or_none=lambda n: object()))

    loader_mod.load_plugins(app_config=cfg, connection_manager=cm, plugin_host=host)

    assert ("ssh", "ssh", True) in seen_ids
    assert ("telnet", "telnet", True) in seen_ids
    assert captured["ssh"] is not captured["telnet"]  # distinct contexts
