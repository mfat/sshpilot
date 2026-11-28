"""Closing an embedded RDP tab asks first, as a terminal tab does.

The remote desktop runs in the tab itself, so closing it ends the session.
A connected tab gets the same "Close connection to …" question (and the
same "Don't ask me again" setting) as an SSH tab; one that is not
connected closes straight away.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("gi")
if "cairo" not in sys.modules:
    sys.modules["cairo"] = types.SimpleNamespace()

from sshpilot.rdp_tab import RdpTab  # noqa: E402
from sshpilot.window import MainWindow  # noqa: E402


class _Config:
    def __init__(self, confirm=True):
        self.values = {"confirm-disconnect": confirm}

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


def _rdp_tab(connected):
    tab = RdpTab.__new__(RdpTab)
    tab._connected = connected
    tab.connection = SimpleNamespace(nickname="desk", display_name="desk")
    tab.close_session = Mock()
    return tab


def _window(tab, *, confirm=True):
    win = MainWindow.__new__(MainWindow)
    win.__dict__.update(
        config=_Config(confirm),
        _rdp_tabs={"desk": tab},
        _is_quitting=False,
        terminal_to_connection={},
    )
    asked = []
    win._ask_close_rdp_session = lambda parent, t, *, on_close, on_cancel=None: asked.append(
        (t, on_close, on_cancel)
    )
    win._is_start_tab_page = lambda _page: False
    win._update_tab_button_visibility = Mock()
    win.has_user_tabs = lambda: True
    return win, asked


def _page(child):
    return SimpleNamespace(get_child=lambda: child)


@pytest.fixture(autouse=True)
def _no_quit_confirmed(monkeypatch):
    from sshpilot import file_manager_window as fmw

    monkeypatch.setattr(fmw, "_quit_confirmed", False, raising=False)


def test_connected_tab_asks_and_closes_on_confirm():
    tab = _rdp_tab(connected=True)
    win, asked = _window(tab)
    tab_view = Mock()
    page = _page(tab)

    assert win.on_tab_close(tab_view, page) is True
    tab.close_session.assert_not_called()
    assert len(asked) == 1

    _tab, on_close, _on_cancel = asked[0]
    on_close()
    tab.close_session.assert_called_once()
    tab_view.close_page_finish.assert_called_once_with(page, True)
    assert win._rdp_tabs == {}


def test_cancel_keeps_the_session():
    tab = _rdp_tab(connected=True)
    win, asked = _window(tab)
    tab_view = Mock()
    page = _page(tab)
    win.on_tab_close(tab_view, page)

    _tab, _on_close, on_cancel = asked[0]
    on_cancel()

    tab.close_session.assert_not_called()
    tab_view.close_page_finish.assert_called_once_with(page, False)
    assert win._rdp_tabs == {"desk": tab}


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda win, tab: setattr(tab, "_connected", False), id="not-connected"),
        pytest.param(lambda win, tab: win.config.set_setting("confirm-disconnect", False),
                     id="opted-out"),
        pytest.param(lambda win, tab: setattr(win, "_suppress_close_confirmation", True),
                     id="bulk-close-already-confirmed"),
        pytest.param(lambda win, tab: setattr(win, "_is_quitting", True), id="quitting"),
    ],
)
def test_closes_without_asking(setup):
    tab = _rdp_tab(connected=True)
    win, asked = _window(tab)
    setup(win, tab)

    assert win.on_tab_close(Mock(), _page(tab)) is False
    assert asked == []
    tab.close_session.assert_called_once()


def test_detached_window_asks_then_closes():
    tab = _rdp_tab(connected=True)
    win, asked = _window(tab)
    window = SimpleNamespace(tab=tab, close=Mock())

    assert win._confirm_rdp_window_close(window) is True
    _tab, on_close, _on_cancel = asked[0]
    on_close()
    window.close.assert_called_once()
    # The second close-request, now confirmed, goes through.
    assert win._confirm_rdp_window_close(window) is False


def test_bulk_close_counts_a_live_remote_desktop():
    live, idle = _rdp_tab(connected=True), _rdp_tab(connected=False)
    win, _asked = _window(live)

    assert win._count_sessions_in_pages([_page(live), _page(idle)]) == 1


def test_connecting_records_recent_use():
    """The start page's Recent list, as for a terminal that connects."""
    tab = RdpTab.__new__(RdpTab)
    manager = Mock()
    tab.window = SimpleNamespace(terminal_manager=manager)
    tab.connection = SimpleNamespace(id="desk", nickname="desk")
    tab.data = {}
    tab._connected = False
    tab._pending_password = None
    tab._password_button = Mock()
    tab._status = Mock()
    display = Mock()
    tab._display = display

    tab._on_connected(display)

    manager.record_last_used.assert_called_once_with(tab.connection)


def test_record_last_used_stamps_metadata_and_refreshes_recent():
    from sshpilot.terminal_manager import TerminalManager

    manager = TerminalManager.__new__(TerminalManager)
    welcome = Mock()
    manager.window = SimpleNamespace(client=Mock(), welcome_view=welcome)

    manager.record_last_used(SimpleNamespace(nickname="desk"))

    nickname, metadata = manager.window.client.update_connection_metadata.call_args.args
    assert nickname == "desk" and "last_used" in metadata
    welcome.refresh_recent.assert_called_once()
