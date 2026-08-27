"""Start page Recent/Pinned rows follow the sidebar privacy toggle."""

from types import SimpleNamespace

from sshpilot.connection_display import HIDDEN_HOST_PLACEHOLDER
from sshpilot.welcome_page import WelcomePage


class _Box:
    def __init__(self):
        self.items = []

    def get_first_child(self):
        return None

    def append(self, item):
        self.items.append(item)


def _recent_page(hide):
    return SimpleNamespace(
        _recent_box=_Box(),
        window=SimpleNamespace(
            _hide_hosts=hide,
            connection_manager=SimpleNamespace(
                get_metadata=lambda nickname: {"last_used": 10},
            ),
        ),
        _min_row=lambda title, subtitle, callback: (title, subtitle),
        _min_section=lambda title, rows: (title, rows),
        _connect_connection_summary=lambda connection_summary: None,
    )


def _pinned_page(hide):
    conn = SimpleNamespace(
        nickname="demo", host="demo", hostname="example.test", username="alice"
    )
    return SimpleNamespace(
        _pinned_box=_Box(),
        connection_manager=SimpleNamespace(connections=[conn]),
        window=SimpleNamespace(
            _hide_hosts=hide,
            connection_manager=SimpleNamespace(
                metadata=[
                    SimpleNamespace(connection_id="demo", values={"pinned": True})
                ],
            ),
            terminal_manager=SimpleNamespace(connect_to_host=lambda c: None),
        ),
        _conn_target=WelcomePage._conn_target,
        _min_row=lambda title, subtitle, callback: (title, subtitle),
        _min_section=lambda title, rows: (title, rows),
        _attach_pinned_context_menu=lambda row, conn_: None,
    )


def test_recent_rows_mask_the_host_when_hosts_are_hidden():
    summary = SimpleNamespace(nickname="demo", display_target="alice@example.test")
    page = _recent_page(True)

    WelcomePage._render_recent_connections(page, [summary], False)

    _title, rows = page._recent_box.items[0]
    assert rows[0] == ("demo", HIDDEN_HOST_PLACEHOLDER)


def test_recent_rows_show_the_host_when_hosts_are_visible():
    summary = SimpleNamespace(nickname="demo", display_target="alice@example.test")
    page = _recent_page(False)

    WelcomePage._render_recent_connections(page, [summary], False)

    _title, rows = page._recent_box.items[0]
    assert rows[0] == ("demo", "alice@example.test")


def test_pinned_rows_mask_the_host_when_hosts_are_hidden():
    page = _pinned_page(True)

    WelcomePage._populate_pinned_box(page)

    _title, rows = page._pinned_box.items[0]
    assert rows[0] == ("demo", HIDDEN_HOST_PLACEHOLDER)


def test_pinned_rows_show_the_host_when_hosts_are_visible():
    page = _pinned_page(False)

    WelcomePage._populate_pinned_box(page)

    _title, rows = page._pinned_box.items[0]
    assert rows[0] == ("demo", "alice@example.test")


def test_toggle_re_renders_recent_from_the_last_snapshot():
    summary = SimpleNamespace(nickname="demo", display_target="alice@example.test")
    page = _recent_page(False)
    page._populate_pinned_box = lambda: None
    page.apply_hide_hosts = lambda hide=None: WelcomePage.apply_hide_hosts(page, hide)

    WelcomePage._render_recent_connections(page, [summary], False)
    page.window._hide_hosts = True
    page.apply_hide_hosts(True)

    _title, rows = page._recent_box.items[-1]
    assert rows[0] == ("demo", HIDDEN_HOST_PLACEHOLDER)
