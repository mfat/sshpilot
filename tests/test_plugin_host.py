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
        self.calls = []

    def connect_to_host(self, conn, **kwargs):
        self.opened.append(conn)
        self.calls.append((conn, kwargs))


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
        self.client = None
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

    term = types.SimpleNamespace(
        connection=FakeConn("box1"),
        _daemon_tab_state=types.SimpleNamespace(session_id="daemon-session-1"),
    )
    host.dispatch_session_opened(term)
    host.dispatch_session_opened(term)  # reconnect of same terminal → no re-emit
    assert len(opened) == 1
    sid = opened[0].session_id

    host.dispatch_session_closed(term)
    assert len(closed) == 1
    assert closed[0].session_id == sid  # stable id across the pair


def test_session_opened_id_routes_directly_to_daemon_session_api():
    from sshpilot.api.models.common import AttachmentId, ClientId, ConnectionId, SessionId
    from sshpilot.api.models.sessions import (
        AttachSessionResult,
        AttachmentInfo,
        SessionState,
        SessionSummary,
    )
    from sshpilot.api.models.terminal import TerminalInput, TerminalOutput

    session = SessionSummary(
        id=SessionId("daemon-session-1"), connection_id=ConnectionId("box1"),
        state=SessionState.RUNNING,
    )

    class Client:
        def __init__(self):
            self.inputs = []
            self._receiver = None

        def attach_session(self, request):
            return AttachSessionResult(
                session=session,
                attachment=AttachmentInfo(
                    id=AttachmentId("attachment-1"),
                    session_id=session.id,
                    client_id=ClientId("client-1"),
                    input_owner=request.request_input,
                ),
            )

        def subscribe_terminal(self, session_id, receiver):
            assert session_id == session.id
            self._receiver = receiver
            return types.SimpleNamespace(close=lambda: None)

        def replay_terminal(self, request):
            assert request.session_id == session.id
            self._receiver(TerminalOutput(
                session_id=request.session_id, sequence=0, data=b"prompt\n",
                replay=True, eof=True,
            ))

        def send_terminal_input(self, value):
            assert isinstance(value, TerminalInput)
            self.inputs.append(value)

    client = Client()
    host = PluginHost(connection_manager=FakeCM())
    host.bind_window(types.SimpleNamespace(client=client))
    opened = []
    host.events.subscribe(Events.SESSION_OPENED, opened.append, plugin_id="p")
    terminal = types.SimpleNamespace(
        connection=FakeConn("box1"),
        _daemon_tab_state=types.SimpleNamespace(session_id=session.id),
    )

    host.dispatch_session_opened(terminal)
    session_id = opened[0].session_id
    assert session_id == session.id
    assert host.read_terminal(session_id) == "prompt\n"
    assert host.send_terminal(session_id, "exit\n") is True
    assert client.inputs[0].session_id == session.id


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


def test_open_command_terminal_accepts_frozen_connection_summary():
    from sshpilot.api.models.common import ConnectionId
    from sshpilot.api.models.connections import ConnectionSummary

    host, cm, window = _host_with_window()
    cm.connections.append(ConnectionSummary(
        id=ConnectionId("box1"), nickname="box1", host="example.test",
        hostname="example.test", username="user", port=22,
    ))
    # The presentation DTO is frozen; the daemon route only needs the durable
    # identity + nickname, so a one-off command must not try to mutate it.
    assert host.open_command_terminal("box1", "docker ps", title="t") is True
    conn, kwargs = window.terminal_manager.calls[-1]
    assert conn is cm.connections[0]
    assert kwargs["remote_command"] == "docker ps"
    assert kwargs["tab_title"] == "t"
    assert kwargs["force_new"] is True
    # unknown → False + toast, no crash
    assert host.open_command_terminal("nope", "echo hi") is False


def test_plugin_session_view_projects_daemon_snapshot_replay_and_input():
    from sshpilot.api.models.common import AttachmentId, ClientId, ConnectionId, SessionId
    from sshpilot.api.models.connections import ConnectionSummary
    from sshpilot.api.models.sessions import (
        AttachSessionResult,
        AttachmentInfo,
        SessionState,
        SessionSummary,
    )
    from sshpilot.api.models.terminal import TerminalInput, TerminalOutput

    connection = ConnectionSummary(
        id=ConnectionId("box1"),
        nickname="box1",
        host="example.test",
        hostname="example.test",
        username="user",
        port=22,
    )
    session = SessionSummary(
        id=SessionId("session-1"),
        connection_id=ConnectionId("box1"),
        state=SessionState.RUNNING,
    )

    class Client:
        def __init__(self):
            self.attached = []
            self.inputs = []
            self._receiver = None

        def list_connections(self):
            return [connection]

        def list_sessions(self):
            return [session]

        def attach_session(self, request):
            self.attached.append(request)
            return AttachSessionResult(
                session=session,
                attachment=AttachmentInfo(
                    id=AttachmentId("attachment-1"),
                    session_id=session.id,
                    client_id=ClientId("client-1"),
                    input_owner=request.request_input,
                ),
            )

        def subscribe_terminal(self, session_id, receiver):
            self._receiver = receiver
            return types.SimpleNamespace(close=lambda: None)

        def replay_terminal(self, request):
            self._receiver(
                TerminalOutput(
                    session_id=request.session_id,
                    sequence=0,
                    data=b"hello\n",
                    replay=True,
                    eof=True,
                )
            )

        def send_terminal_input(self, value):
            assert isinstance(value, TerminalInput)
            self.inputs.append(value)

    client = Client()
    host = PluginHost(connection_manager=FakeCM())
    host.bind_window(types.SimpleNamespace(client=client))

    sessions = host.list_sessions()
    assert [item.session_id for item in sessions] == ["session-1"]
    assert host.read_terminal("session-1", max_chars=5) == "ello\n"
    assert host.send_terminal("session-1", "exit\n") is True
    assert client.inputs[0].data == b"exit\n"
    assert [item.request_input for item in client.attached] == [False, True]


def test_plugin_remote_command_and_stream_use_daemon_client():
    from sshpilot.api.models.connections import ConnectionSummary

    connection = ConnectionSummary(
        id="box1",
        nickname="box1",
        host="example.test",
        hostname="example.test",
        username="user",
        port=22,
    )

    class Client:
        def __init__(self):
            self.inputs = []
            self.cancelled = []
            self.closed = False

        def list_connections(self):
            return [connection]

        def start_broadcast_command(self, request, *, input=None):
            self.inputs.append((request, input))
            return types.SimpleNamespace(
                operation=types.SimpleNamespace(
                    operation_id="operation-1",
                    state=types.SimpleNamespace(value="running"),
                )
            )

        def get_broadcast_command(self, _operation_id):
            return types.SimpleNamespace(
                operation=types.SimpleNamespace(state=types.SimpleNamespace(value="succeeded")),
                targets=[types.SimpleNamespace(exit_code=0, stdout="ok", stderr="")],
            )

        def subscribe_broadcast_output(self, _operation_id, on_output, _on_done):
            on_output("stdout", "followed\n")
            return types.SimpleNamespace(close=lambda: setattr(self, "closed", True))

        def cancel_broadcast_command(self, operation_id):
            self.cancelled.append(operation_id)

    client = Client()
    host = PluginHost(connection_manager=FakeCM())
    host.bind_window(types.SimpleNamespace(client=client))
    ctx = PluginContext(
        plugin_id="docker",
        app_config=FakeConfig(),
        connection_manager=FakeCM(),
        protocol_registry=registry_mod.ProtocolRegistry(),
        host=host,
    )

    result = ctx.run_command("box1", "sudo true", input="password\n")
    assert (result.exit_code, result.stdout) == (0, "ok")
    assert client.inputs[0][1] == "password\n"

    lines = []
    handle = ctx.run_command_stream("box1", "docker logs -f", on_line=lines.append)
    assert lines == ["followed"]
    assert handle.running is True
    handle.stop()
    assert client.cancelled == ["operation-1"]
    assert client.closed is True


def test_plugin_command_stream_reassembles_broadcast_chunks():
    from sshpilot.api.models.connections import ConnectionSummary

    connection = ConnectionSummary(
        id="box1", nickname="box1", host="example.test",
        hostname="example.test", username="user", port=22,
    )

    class Client:
        def list_connections(self):
            return [connection]

        def start_broadcast_command(self, _request, *, input=None):
            assert input is None
            return types.SimpleNamespace(
                operation=types.SimpleNamespace(operation_id="operation-1"),
            )

        def subscribe_broadcast_output(self, _operation_id, on_output, on_done):
            self.on_output = on_output
            self.on_done = on_done
            return types.SimpleNamespace(close=lambda: None)

        def cancel_broadcast_command(self, _operation_id):
            pass

    client = Client()
    host = PluginHost(connection_manager=FakeCM())
    host.bind_window(types.SimpleNamespace(client=client))
    ctx = PluginContext(
        plugin_id="docker", app_config=FakeConfig(),
        connection_manager=FakeCM(), protocol_registry=registry_mod.ProtocolRegistry(),
        host=host,
    )
    lines = []
    done = []
    ctx.run_command_stream("box1", "docker logs -f", on_line=lines.append,
                           on_done=done.append)

    client.on_output("stdout", "first\nsecond\n")
    client.on_output("stdout", "split")
    client.on_output("stdout", " line\n")
    client.on_output("stderr", "final unterminated")
    client.on_done(0)

    assert lines == ["first", "second", "split line", "final unterminated"]
    assert done == [0]


def test_generate_key_returns_path():
    host, _, _ = _host_with_window()
    assert host.generate_key("k1") == "/keys/k1"


def test_delete_key_resolves_legacy_path_to_daemon_key_identity():
    host, _, window = _host_with_window()
    key = types.SimpleNamespace(
        key_id="key-1",
        private_path="/daemon/keys/id_ed25519",
    )
    deleted = []

    class _DaemonKeyManager:
        def discover_keys(self):
            return [key]

        def delete_key(self, value):
            deleted.append(value)
            return True

    window.key_manager = _DaemonKeyManager()

    assert host.delete_key("/daemon/keys/id_ed25519") is True
    assert deleted == [key]
    assert host.delete_key("/outside/unmanaged") is False


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
    settings = {}
    host._window = types.SimpleNamespace(
        client=types.SimpleNamespace(
            get_plugin_setting=lambda _p, key, default=None: settings.get(key, default),
            set_plugin_setting=lambda _p, key, value: settings.__setitem__(key, value),
        )
    )
    ctx = PluginContext(plugin_id="acme", app_config=cfg, connection_manager=cm,
                        protocol_registry=registry_mod.ProtocolRegistry(), host=host)
    assert ctx.plugin_id == "acme"

    ctx.secrets.set("token", "v")
    assert cm.secrets == {("acme", "token"): "v"}
    assert ctx.secrets.get("token") == "v"
    assert ctx.secrets.delete("token") is True

    ctx.settings.set("region", "fra1")
    assert settings == {"region": "fra1"}
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


# --- identity facade (daemon-owned) ---------------------------------------

class FakeDaemonClient:
    """Minimal SshPilotClient used by the identity facade tests."""

    def __init__(self, registry=None, agent_keys=None):
        self.registry = registry
        self.agent_keys = agent_keys
        self.calls = []

    def get_identity_providers(self):
        self.calls.append("get_identity_providers")
        return self.registry

    def list_provider_agent_keys(self, request):
        self.calls.append(("list_provider_agent_keys", request.provider_id))
        return self.agent_keys


def _identity_ctx(fake_client):
    cm = FakeCM()
    host = PluginHost(connection_manager=cm)
    window = FakeWindow()
    window.client = fake_client
    host.bind_window(window)
    return PluginContext(plugin_id="acme", app_config=FakeConfig(),
                         connection_manager=cm,
                         protocol_registry=registry_mod.ProtocolRegistry(),
                         host=host)


def _registry(auto_available, *, selected="auto"):
    from sshpilot.api.models.identity import IdentityProviderDescriptor, IdentityProviderRegistry

    def _descriptor(provider_id, label, available, selected):
        return IdentityProviderDescriptor(
            provider_id=provider_id,
            label=label,
            available=available,
            selected=selected,
            effective_agent_socket="/run/user/1000/agent.sock",
            custom_socket_required=False,
            capabilities=("ssh_auth_sock",),
        )

    return IdentityProviderRegistry(
        providers=(
            _descriptor("auto", "Automatic (system ssh-agent)", auto_available,
                        selected == "auto"),
            _descriptor("onepassword", "1Password", True, selected == "onepassword"),
        ),
        revision="rev-1",
    )


def test_identities_is_agent_available_comes_from_daemon_state():
    """ctx.identities.is_agent_available() reflects daemon-owned provider
    state: the 'auto' (system ssh-agent) descriptor's flag, even when another
    provider is selected."""
    client = FakeDaemonClient(registry=_registry(True, selected="onepassword"))
    ctx = _identity_ctx(client)
    assert ctx.identities.is_agent_available() is True

    unavailable = FakeDaemonClient(registry=_registry(False))
    ctx = _identity_ctx(unavailable)
    assert ctx.identities.is_agent_available() is False

    ctx = _identity_ctx(FakeDaemonClient(registry=None))
    assert ctx.identities.is_agent_available() is False


def test_identities_list_routes_through_daemon_provider_agent_keys():
    from sshpilot.api.models.identity import AgentKey, AgentKeyList

    client = FakeDaemonClient(
        registry=_registry(True),
        agent_keys=AgentKeyList(
            keys=(
                AgentKey(fingerprint="SHA256:abc", comment="user@host", key_type="ed25519"),
                AgentKey(fingerprint="SHA256:def", comment="", key_type="rsa"),
            )
        ),
    )
    ctx = _identity_ctx(client)
    identities = ctx.identities.list()
    assert client.calls == [("list_provider_agent_keys", "auto")]
    assert [identity.fingerprint for identity in identities] == ["SHA256:abc", "SHA256:def"]
    assert identities[0].provider_name == "system-agent"
    assert identities[0].display_name == "user@host"
    assert identities[1].display_name == "rsa"  # falls back to key type


def test_identities_facade_never_uses_frontend_identity_manager(monkeypatch):
    """The facade answers from the daemon client even when the frontend
    IdentityManager is unavailable — proving there is no frontend fallback."""
    from sshpilot.api.models.identity import AgentKey, AgentKeyList
    from sshpilot import identity as identity_module

    def boom(*_a, **_k):
        raise AssertionError("frontend identity manager must not be used")

    monkeypatch.setattr(identity_module, "get_identity_manager", boom)

    client = FakeDaemonClient(
        registry=_registry(True),
        agent_keys=AgentKeyList(
            keys=(AgentKey(fingerprint="SHA256:abc", comment="user@host", key_type="ed25519"),)
        ),
    )
    ctx = _identity_ctx(client)
    assert ctx.identities.list()[0].fingerprint == "SHA256:abc"
    assert ctx.identities.is_agent_available() is True


def test_identities_facade_client_error_returns_empty_not_raise():
    class _Broken:
        def get_identity_providers(self):
            raise OSError("no socket")

        def list_provider_agent_keys(self, _request):
            raise RuntimeError("boom")

    ctx = _identity_ctx(_Broken())
    assert ctx.identities.list() == []
    assert ctx.identities.is_agent_available() is False


def test_identities_facade_hostless_is_inert():
    cm = FakeCM()
    ctx = PluginContext(plugin_id="x", app_config=FakeConfig(),
                        connection_manager=cm,
                        protocol_registry=registry_mod.ProtocolRegistry(), host=None)
    assert ctx.identities.list() == []
    assert ctx.identities.is_agent_available() is False
