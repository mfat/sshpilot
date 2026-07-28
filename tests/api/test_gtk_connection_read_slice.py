from types import SimpleNamespace

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

