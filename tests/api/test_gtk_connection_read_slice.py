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


def test_recent_listing_reads_dtos_from_client_not_manager():
    summary = ConnectionSummary(
        id=ConnectionId("connection:v1:test"),
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
        config=SimpleNamespace(
            get_connection_meta=lambda nickname: {"last_used": 10}
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
