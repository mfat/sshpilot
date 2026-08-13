from types import SimpleNamespace

import pytest

from sshpilot import welcome_page
from sshpilot.api import ErrorCode, SshPilotError
from sshpilot.api.models import ConnectionSummary
from sshpilot.api.models.common import ConnectionId
from sshpilot.welcome_page import WelcomePage


class _Box:
    def __init__(self):
        self.items = []

    def get_first_child(self):
        return None

    def append(self, item):
        self.items.append(item)


class _Request:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Bridge:
    def submit(self, operation, *, on_success, on_error, on_discard=None):
        self.operation = operation
        self.on_success = on_success
        self.on_error = on_error
        self.on_discard = on_discard
        self.request = _Request()
        return self.request


class _AsyncPage:
    _finish_recent_read = WelcomePage._finish_recent_read
    _fail_recent_read = WelcomePage._fail_recent_read
    _empty_recent_message = staticmethod(WelcomePage._empty_recent_message)
    _recent_read_error_message = staticmethod(
        WelcomePage._recent_read_error_message
    )
    _render_recent_connections = WelcomePage._render_recent_connections
    schedule_connection_refresh = WelcomePage.schedule_connection_refresh
    _run_scheduled_connection_refresh = WelcomePage._run_scheduled_connection_refresh
    close = WelcomePage.close


def test_recent_listing_reads_dtos_from_client_not_manager():
    summary = ConnectionSummary(
        id=ConnectionId("test"),
        nickname="demo",
        host="demo",
        hostname="example.test",
        username="alice",
        port=22,
    )

    class _Client:
        calls = 0

        def list_connections(self):
            self.calls += 1
            return [summary]

    client = _Client()
    page = SimpleNamespace(
        _recent_box=_Box(),
        client=client,
        window=SimpleNamespace(
            connection_manager=SimpleNamespace(
                get_metadata=lambda nickname: {"last_used": 10},
            ),
        ),
        _min_row=lambda title, subtitle, callback: (title, subtitle, callback),
        _min_section=lambda title, rows: (title, rows),
        _connect_connection_summary=lambda connection_summary: None,
    )

    WelcomePage._populate_recent_box(page)

    assert client.calls == 1
    title, rows = page._recent_box.items[0]
    assert title == "Recent"
    assert rows[0][:2] == ("demo", "alice@example.test")


def test_recent_dto_is_resolved_only_when_user_activates_it():
    summary = SimpleNamespace(nickname="demo")
    internal = object()
    connected = []
    page = SimpleNamespace(
        connection_manager=SimpleNamespace(
            find_connection_by_nickname=lambda nickname: internal
        ),
        window=SimpleNamespace(
            terminal_manager=SimpleNamespace(connect_to_host=connected.append)
        ),
    )

    WelcomePage._connect_connection_summary(page, summary)

    assert connected == [internal]


def test_structured_client_error_keeps_recent_fallback_visible(
    monkeypatch,
    caplog,
):
    class _Label:
        def __init__(self, *, label):
            self.label = label
            self.css_classes = []

        def add_css_class(self, name):
            self.css_classes.append(name)

        def set_wrap(self, _value):
            pass

        def set_justify(self, _value):
            pass

        def set_halign(self, _value):
            pass

        def set_hexpand(self, _value):
            pass

        def set_margin_top(self, _value):
            pass

    class _FailingClient:
        def list_connections(self):
            raise SshPilotError(
                ErrorCode.INTERNAL_ERROR,
                "Raw backend message must remain private",
                details={"reason": "private diagnostic"},
            )

    monkeypatch.setattr(welcome_page.Gtk, "Label", _Label)
    page = SimpleNamespace(
        _recent_box=_Box(),
        client=_FailingClient(),
        config=SimpleNamespace(get_connection_meta=lambda _nickname: {}),
        _empty_recent_message=WelcomePage._empty_recent_message,
        _recent_read_error_message=WelcomePage._recent_read_error_message,
    )

    WelcomePage._populate_recent_box(page)

    fallback = page._recent_box.items[0]
    assert fallback.label == "Recent connections are temporarily unavailable"
    assert "warning" in fallback.css_classes
    assert "internal_error" in caplog.text
    assert "Raw backend message" not in caplog.text
    assert "private diagnostic" not in caplog.text


def test_clearing_client_renders_unavailable_recent_state(monkeypatch):
    class _Label:
        def __init__(self, *, label):
            self.label = label
            self.css_classes = []

        def add_css_class(self, name):
            self.css_classes.append(name)

        def set_wrap(self, _value):
            pass

        def set_justify(self, _value):
            pass

        def set_halign(self, _value):
            pass

        def set_hexpand(self, _value):
            pass

        def set_margin_top(self, _value):
            pass

    class _Page:
        set_client = WelcomePage.set_client
        refresh_recent = WelcomePage.refresh_recent
        _populate_recent_box = WelcomePage._populate_recent_box

        def __init__(self):
            self._recent_box = _Box()
            self.client = object()
            self.client_bridge = object()
            self._client_selection_pending = True
            self._recent_request = None

    monkeypatch.setattr(welcome_page.Gtk, "Label", _Label)
    page = _Page()

    page.set_client(None)

    assert page.client is None
    assert page.client_bridge is None
    assert page._client_selection_pending is False
    fallback = page._recent_box.items[0]
    assert fallback.label == "Recent connections are temporarily unavailable"
    assert "warning" in fallback.css_classes


def test_unexpected_client_exception_remains_diagnosable():
    class _BrokenClient:
        def list_connections(self):
            raise RuntimeError("unexpected implementation bug")

    page = SimpleNamespace(
        _recent_box=_Box(),
        client=_BrokenClient(),
    )

    with pytest.raises(RuntimeError, match="unexpected implementation bug"):
        WelcomePage._populate_recent_box(page)


def test_daemon_backed_recent_read_is_submitted_without_blocking(monkeypatch):
    class _Label:
        def __init__(self, *, label):
            self.label = label

        def add_css_class(self, _name):
            pass

        def set_wrap(self, _value):
            pass

        def set_justify(self, _value):
            pass

        def set_halign(self, _value):
            pass

        def set_hexpand(self, _value):
            pass

        def set_margin_top(self, _value):
            pass

    summary = ConnectionSummary(
        id=ConnectionId("async"),
        nickname="async",
        host="async",
        hostname="async.example",
        username="alice",
        port=22,
    )
    calls = []
    bridge = _Bridge()
    monkeypatch.setattr(welcome_page.Gtk, "Label", _Label)
    page = _AsyncPage()
    page._recent_box = _Box()
    page.client = SimpleNamespace(
        list_connections=lambda: calls.append(True) or [summary]
    )
    page.client_bridge = bridge
    page._client_selection_pending = False
    page._recent_request = None
    page._recent_generation = 0
    page._closed = False
    page.window = SimpleNamespace(
        connection_manager=SimpleNamespace(
            get_metadata=lambda _nickname: {"last_used": 1},
        ),
    )
    page._min_row = lambda title, subtitle, callback: (title, subtitle, callback)
    page._min_section = lambda title, rows: (title, rows)
    page._connect_connection_summary = lambda _summary: None

    WelcomePage._populate_recent_box(page)

    assert calls == []
    assert page._recent_box.items[0].label == "Loading recent connections…"

    bridge.on_success(bridge.operation())

    assert calls == [True]
    title, rows = page._recent_box.items[-1]
    assert title == "Recent"
    assert rows[0][:2] == ("async", "alice@async.example")


def test_destroyed_welcome_page_ignores_late_daemon_result(monkeypatch):
    class _Label:
        def __init__(self, *, label):
            self.label = label

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    bridge = _Bridge()
    monkeypatch.setattr(welcome_page.Gtk, "Label", _Label)
    page = _AsyncPage()
    page._recent_box = _Box()
    page.client = SimpleNamespace(list_connections=list)
    page.client_bridge = bridge
    page._client_selection_pending = False
    page._recent_request = None
    page._recent_generation = 0
    page._closed = False
    page.config = SimpleNamespace(get_connection_meta=lambda _nickname: {})

    WelcomePage._populate_recent_box(page)
    page.close()
    bridge.on_success([])

    assert bridge.request.cancelled is True
    assert [item.label for item in page._recent_box.items] == [
        "Loading recent connections…"
    ]


def test_connection_event_refreshes_are_coalesced_into_one_idle(monkeypatch):
    callbacks = []
    monkeypatch.setattr(
        welcome_page.GLib,
        "idle_add",
        lambda callback: callbacks.append(callback) or 17,
    )
    page = _AsyncPage()
    page._closed = False
    page._recent_request = None
    page._event_refresh_source = None
    page._event_refresh_follow_up = False
    page._populate_recent_box = lambda: callbacks.append("refresh")

    page.schedule_connection_refresh()
    page.schedule_connection_refresh()
    page.schedule_connection_refresh()

    assert callbacks == [page._run_scheduled_connection_refresh]
    assert callbacks[0]() is False
    assert callbacks[-1] == "refresh"
    assert page._event_refresh_source is None


def test_event_during_active_refresh_schedules_only_one_follow_up(monkeypatch):
    callbacks = []
    monkeypatch.setattr(
        WelcomePage,
        "_render_recent_connections",
        lambda _self, _connections, _read_error: None,
    )
    monkeypatch.setattr(
        welcome_page.GLib,
        "idle_add",
        lambda callback: callbacks.append(callback) or 18,
    )
    page = _AsyncPage()
    page._closed = False
    page._recent_request = _Request()
    page._recent_generation = 3
    page._event_refresh_source = None
    page._event_refresh_follow_up = False
    page._recent_box = _Box()
    page.config = SimpleNamespace(get_connection_meta=lambda _nickname: {})

    page.schedule_connection_refresh()
    page.schedule_connection_refresh()

    assert page._event_refresh_follow_up is True
    assert callbacks == []

    page._finish_recent_read(3, [])

    assert page._event_refresh_follow_up is False
    assert callbacks == [page._run_scheduled_connection_refresh]
