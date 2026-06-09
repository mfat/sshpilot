"""Tests for ConnectionRow tooltip behaviour (sidebar.py).

Strategy: ConnectionRow.__init__ creates many GTK widgets and cannot run in the
test environment without a full GTK stack.  We use __new__ to bypass __init__ and
set only the attributes required by the methods under test.
"""
import importlib
import inspect
from unittest.mock import MagicMock

from sshpilot.connection_manager import Connection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _DummyConfig:
    def get_setting(self, key, default=None):
        return default


class _FakeRoot:
    """Minimal stand-in for the Gtk window returned by get_root()."""
    def __init__(self, hide_hosts: bool = False):
        self._hide_hosts = hide_hosts


def _make_row(connection: Connection):
    """Return a (row, sidebar_module) pair built via __new__ with minimal setup."""
    sidebar_mod = importlib.import_module('sshpilot.sidebar')
    row = sidebar_mod.ConnectionRow.__new__(sidebar_mod.ConnectionRow)
    row.connection = connection
    row.host_label = MagicMock()
    row.config = _DummyConfig()
    return row, sidebar_mod


# ---------------------------------------------------------------------------
# host_label tooltip — tested through _apply_host_label_text()
# ---------------------------------------------------------------------------

def test_host_label_tooltip_shows_formatted_host(monkeypatch):
    """_apply_host_label_text sets host_label tooltip to the formatted host display."""
    conn = Connection({'nickname': 'prod', 'host': 'prod.example.com', 'user': 'alice'})
    row, sidebar_mod = _make_row(conn)
    monkeypatch.setattr(
        sidebar_mod, '_format_connection_host_display',
        lambda c, **kw: 'alice@prod.example.com',
    )
    row.get_root = lambda: _FakeRoot(hide_hosts=False)

    row._apply_host_label_text()

    row.host_label.set_tooltip_text.assert_called_once_with('alice@prod.example.com')
    row.host_label.set_text.assert_called_once_with('alice@prod.example.com')


def test_host_label_tooltip_cleared_when_hide_hosts_active(monkeypatch):
    """_apply_host_label_text clears host_label tooltip when the window is in hide-hosts mode."""
    conn = Connection({'nickname': 'prod', 'host': 'prod.example.com'})
    row, _ = _make_row(conn)
    row.get_root = lambda: _FakeRoot(hide_hosts=True)

    row._apply_host_label_text()

    row.host_label.set_tooltip_text.assert_called_once_with('')
    row.host_label.set_text.assert_called_once_with('••••••••••')


def test_host_label_tooltip_empty_when_no_host_display(monkeypatch):
    """_apply_host_label_text sets an empty tooltip when the host display string is empty."""
    conn = Connection({'nickname': 'local', 'host': ''})
    row, sidebar_mod = _make_row(conn)
    monkeypatch.setattr(
        sidebar_mod, '_format_connection_host_display',
        lambda c, **kw: '',
    )
    row.get_root = lambda: _FakeRoot(hide_hosts=False)

    row._apply_host_label_text()

    row.host_label.set_tooltip_text.assert_called_once_with('')


# ---------------------------------------------------------------------------
# nickname_label tooltip — verified via source inspection
#
# ConnectionRow.__init__ constructs too many GTK widgets to run headlessly.
# The source-inspection test ensures the tooltip line survives future edits.
# ---------------------------------------------------------------------------

def test_nickname_label_tooltip_set_in_init():
    """ConnectionRow.__init__ configures nickname_label tooltip with connection.nickname."""
    sidebar_mod = importlib.import_module('sshpilot.sidebar')
    source = inspect.getsource(sidebar_mod.ConnectionRow.__init__)
    assert 'nickname_label.set_tooltip_text' in source
    assert 'connection.nickname' in source


def test_update_display_refreshes_nickname_tooltip(monkeypatch):
    """update_display refreshes nickname_label tooltip so a rename is not left stale."""
    conn = Connection({'nickname': 'renamed', 'host': 'prod.example.com'})
    row, sidebar_mod = _make_row(conn)
    row.nickname_label = MagicMock()
    monkeypatch.setattr(
        sidebar_mod, '_format_connection_host_display',
        lambda c, **kw: 'prod.example.com',
    )
    row.get_root = lambda: _FakeRoot(hide_hosts=False)
    row._update_forwarding_indicators = lambda: None
    row.update_status = lambda: None

    row.update_display()

    row.nickname_label.set_tooltip_text.assert_called_once_with('renamed')
