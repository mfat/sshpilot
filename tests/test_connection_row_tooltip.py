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
# host_label — no longer carries its own tooltip (row markup owns hover)
# ---------------------------------------------------------------------------

def test_host_label_tooltip_cleared_when_shown(monkeypatch):
    """_apply_host_label_text clears host_label tooltip so the row markup wins."""
    conn = Connection({'nickname': 'prod', 'host': 'prod.example.com', 'user': 'alice'})
    row, sidebar_mod = _make_row(conn)
    monkeypatch.setattr(
        sidebar_mod, '_format_connection_host_display',
        lambda c, **kw: 'alice@prod.example.com',
    )
    row.get_root = lambda: _FakeRoot(hide_hosts=False)

    row._apply_host_label_text()

    row.host_label.set_tooltip_text.assert_called_once_with('')
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
# Row markup tooltip
# ---------------------------------------------------------------------------

def test_refresh_row_tooltip_sets_markup(monkeypatch):
    """_refresh_row_tooltip installs composed Pango markup on the row."""
    conn = Connection({
        'nickname': 'prod',
        'hostname': 'prod.example.com',
        'username': 'alice',
    })
    row, sidebar_mod = _make_row(conn)
    row.set_tooltip_markup = MagicMock()
    row.set_tooltip_text = MagicMock()
    row.get_root = lambda: _FakeRoot(hide_hosts=False)
    monkeypatch.setattr(
        sidebar_mod, '_format_connection_row_tooltip_markup',
        lambda c, hide_hosts=False: '<b>prod</b>\n<span alpha=\'70%\'>alice@prod.example.com</span>',
    )

    row._refresh_row_tooltip()

    row.set_tooltip_markup.assert_called_once()
    assert '<b>prod</b>' in row.set_tooltip_markup.call_args.args[0]
    row.set_tooltip_text.assert_not_called()


def test_refresh_row_tooltip_respects_hide_hosts(monkeypatch):
    """_refresh_row_tooltip passes hide_hosts through to the markup helper."""
    conn = Connection({'nickname': 'prod', 'hostname': 'prod.example.com'})
    row, sidebar_mod = _make_row(conn)
    row.set_tooltip_markup = MagicMock()
    row.get_root = lambda: _FakeRoot(hide_hosts=True)
    seen = {}

    def _capture(c, hide_hosts=False):
        seen['hide_hosts'] = hide_hosts
        return '<b>prod</b>'

    monkeypatch.setattr(
        sidebar_mod, '_format_connection_row_tooltip_markup', _capture,
    )

    row._refresh_row_tooltip()

    assert seen['hide_hosts'] is True
    row.set_tooltip_markup.assert_called_once_with('<b>prod</b>')


def test_nickname_label_has_no_own_tooltip_in_init():
    """ConnectionRow.__init__ does not set a nickname_label tooltip.

    The composed row markup tooltip covers the nickname/host hover area.
    """
    sidebar_mod = importlib.import_module('sshpilot.sidebar')
    source = inspect.getsource(sidebar_mod.ConnectionRow.__init__)
    assert 'nickname_label.set_tooltip_text' not in source
    assert '_refresh_row_tooltip' in source


def test_update_display_refreshes_row_tooltip(monkeypatch):
    """update_display refreshes the row markup tooltip after a rename."""
    conn = Connection({'nickname': 'renamed', 'host': 'prod.example.com'})
    row, sidebar_mod = _make_row(conn)
    row.nickname_label = MagicMock()
    row.set_tooltip_markup = MagicMock()
    monkeypatch.setattr(
        sidebar_mod, '_format_connection_host_display',
        lambda c, **kw: 'prod.example.com',
    )
    monkeypatch.setattr(
        sidebar_mod, '_format_connection_row_tooltip_markup',
        lambda c, hide_hosts=False: '<b>renamed</b>',
    )
    row.get_root = lambda: _FakeRoot(hide_hosts=False)
    row._update_forwarding_indicators = lambda: None
    row.update_status = lambda: None
    row._update_accessible_identity = lambda: None

    row.update_display()

    row.nickname_label.set_text.assert_called_once_with('renamed')
    row.set_tooltip_markup.assert_called_once_with('<b>renamed</b>')
