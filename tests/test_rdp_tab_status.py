"""The embedded RDP tab shows on the sidebar and in the quit check.

Its FreeRDP session runs in the GTK process, so daemon status never
describes it: the host showed no indicator while a remote desktop was open,
and quitting ended it without asking.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("gi")

from sshpilot.connection_model import ConnectionState  # noqa: E402
from sshpilot.daemon_quit_policy import (  # noqa: E402
    count_embedded_rdp_sessions,
    has_daemon_active_work,
)
from sshpilot.gtk.connection_runtime_status import ConnectionRuntimeStatusStore  # noqa: E402
from sshpilot.rdp_tab import RdpTab  # noqa: E402


class _Client:
    server_instance_id = "daemon-a"

    def subscribe_events(self, _callback):
        return Mock()

    def list_sessions(self):
        return []

    def list_sftp_services(self):
        return []


def _bare_tab(store):
    """An RdpTab without its widgets: only the session bookkeeping runs."""
    tab = RdpTab.__new__(RdpTab)
    tab.window = SimpleNamespace(connection_runtime_status=store)
    tab.connection = SimpleNamespace(id="rdp-host", nickname="rdp-host", data={})
    tab.data = {"host": "desk.example", "port": 3389}
    tab._display = object()
    tab._connected = False
    tab._closing = False
    tab._last_error = None
    tab._pending_password = None
    tab._failure_kind = None
    tab._failure_was_authentication = False
    tab._password_button = Mock()
    tab._status = Mock()
    tab._show_status = Mock()
    return tab


@pytest.fixture
def store():
    store = ConnectionRuntimeStatusStore()
    store.attach_client(_Client())
    return store


def test_connected_tab_lights_the_host(store):
    tab = _bare_tab(store)
    tab._on_connected(Mock(get_width=lambda: 1, get_height=lambda: 1, grab_focus=lambda: None))
    # _on_connected ignores a display that is not the tab's own
    assert store.status_for("rdp-host").state is ConnectionState.UNKNOWN

    display = Mock()
    tab._display = display
    tab._on_connected(display)
    assert store.status_for("rdp-host").state is ConnectionState.CONNECTED


def test_closing_the_tab_clears_the_host(store):
    tab = _bare_tab(store)
    display = Mock()
    tab._display = display
    tab._on_connected(display)

    tab.close_session()

    assert store.status_for("rdp-host").state is ConnectionState.UNKNOWN
    assert tab.connected is False


def test_remote_end_reports_disconnected(store):
    tab = _bare_tab(store)
    display = Mock()
    tab._display = display
    tab._on_connected(display)

    tab._on_disconnected(display)

    assert store.status_for("rdp-host").state is ConnectionState.DISCONNECTED


def test_lost_session_reports_disconnected_not_failed(store):
    """A logoff or dropped link after the desktop was up: the host went down."""
    tab = _bare_tab(store)
    display = Mock()
    tab._display = display
    tab._on_connected(display)
    tab._record_failure(display, "ERRINFO_LOGOFF_BY_USER", authentication=False)

    tab._on_disconnected(display)

    assert store.status_for("rdp-host").state is ConnectionState.DISCONNECTED


def test_failed_logon_reports_failed_with_its_reason(store):
    from sshpilot.rdp_failure import RdpFailureKind

    tab = _bare_tab(store)
    display = Mock()
    tab._display = display
    tab._record_failure(display, "ERRCONNECT_LOGON_FAILURE", authentication=True)
    tab._failure_kind = RdpFailureKind.AUTHENTICATION

    tab._on_disconnected(display)

    status = store.status_for("rdp-host")
    assert status.state is ConnectionState.FAILED
    assert "ERRCONNECT_LOGON_FAILURE" in status.reason


def test_tab_without_a_status_store_still_works():
    tab = _bare_tab(None)
    tab.window = SimpleNamespace()
    display = Mock()
    tab._display = display
    tab._on_connected(display)
    assert tab.connected is True


def _window(*tabs):
    window = SimpleNamespace(connection_to_terminals={}, client=None)
    window._rdp_tabs = {f"conn-{i}": tab for i, tab in enumerate(tabs)}
    return window


def test_quit_asks_while_an_embedded_rdp_tab_is_connected():
    live = SimpleNamespace(connected=True)
    idle = SimpleNamespace(connected=False)

    assert count_embedded_rdp_sessions(_window(live, idle)) == 1
    assert has_daemon_active_work(_window(live, idle)) is True
    assert has_daemon_active_work(_window(idle)) is False
    assert count_embedded_rdp_sessions(SimpleNamespace()) == 0


def _closing_window(monkeypatch, *rdp_tabs):
    """A MainWindow reduced to what on_close_request reads."""
    from sshpilot import file_manager_window as fmw
    from sshpilot import sidebar_views
    from sshpilot.window import MainWindow

    monkeypatch.setattr(sidebar_views, "confirm_quit", lambda *_a: False)
    monkeypatch.setattr(fmw, "running_transfer_count_everywhere", lambda: 0)
    win = MainWindow.__new__(MainWindow)
    win.__dict__.update(
        _is_quitting=False,
        session_manager=None,
        connection_to_terminals={},
        _rdp_tabs={f"conn-{i}": tab for i, tab in enumerate(rdp_tabs)},
        client=None,
    )
    win.show_quit_confirmation_dialog = Mock()
    win._teardown_ssh_config_monitor = Mock()
    win._invalidate_api_window_callbacks = Mock()
    return win


def test_closing_the_window_asks_while_a_remote_desktop_is_open(monkeypatch):
    win = _closing_window(monkeypatch, SimpleNamespace(connected=True))

    assert win.on_close_request(win) is True
    win.show_quit_confirmation_dialog.assert_called_once()


def test_closing_the_window_does_not_ask_for_an_ended_rdp_tab(monkeypatch):
    win = _closing_window(monkeypatch, SimpleNamespace(connected=False))

    assert win.on_close_request(win) is False
    win.show_quit_confirmation_dialog.assert_not_called()
