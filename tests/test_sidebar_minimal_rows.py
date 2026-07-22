"""Tests for minimal (icon-only) sidebar row rendering — ConnectionRow.set_compact.

ConnectionRow.__init__ builds many real GTK widgets, so we bypass it with
__new__ and inject MagicMock widgets, exercising only the compact/restore
branch logic (which style is shown, what gets hidden, tooltip, restore).
"""
import importlib
from unittest.mock import MagicMock

from sshpilot.connection_manager import Connection


class _Cfg:
    def __init__(self, style='initials'):
        self.style = style

    def get_setting(self, key, default=None):
        if key == 'ui.sidebar_minimal_row_style':
            return self.style
        if key == 'ui.sidebar_show_connection_icon':
            return True
        return default


def _make(style='initials'):
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.ConnectionRow.__new__(mod.ConnectionRow)
    row.connection = Connection({'nickname': 'Prod Web', 'host': 'h', 'user': 'a'})
    row.config = _Cfg(style)
    row._compact = False
    row._avatar = None
    for name in ('_content_box', '_info_box', 'indicator_box', 'color_badge',
                 'color_dot', 'file_manager_button', 'status_icon',
                 'connection_icon'):
        setattr(row, name, MagicMock())
    row.set_tooltip_text = MagicMock()
    row.update_status = MagicMock()
    row.set_margin_start = MagicMock()
    row._apply_group_display_mode = MagicMock()
    return row, mod


def test_compact_icon_style_hides_labels_shows_icon():
    row, _ = _make('icon')
    row.set_compact(True)

    assert row._compact is True
    row._info_box.set_visible.assert_called_with(False)
    row.connection_icon.set_visible.assert_called_with(True)
    row.set_tooltip_text.assert_called_with('Prod Web')
    # Icon style must not create an avatar.
    assert row._avatar is None
    # Nested-group indentation is flattened in the strip.
    row.set_margin_start.assert_called_with(0)


def test_restore_reapplies_nested_indentation():
    row, _ = _make('icon')
    row.set_compact(True)
    row._apply_group_display_mode.reset_mock()
    row.set_compact(False)
    row._apply_group_display_mode.assert_called_once()


def test_compact_initials_style_creates_named_avatar(monkeypatch):
    row, mod = _make('initials')
    fake_avatar = MagicMock()
    monkeypatch.setattr(mod.Adw, 'Avatar', lambda **kw: fake_avatar)

    row.set_compact(True)

    assert row._avatar is fake_avatar
    fake_avatar.set_text.assert_called_with('Prod Web')
    row._content_box.prepend.assert_called_with(fake_avatar)
    fake_avatar.set_visible.assert_called_with(True)
    row.connection_icon.set_visible.assert_called_with(False)


def test_restore_shows_labels_and_refreshes_status():
    row, _ = _make('icon')
    row.set_compact(True)
    row.update_status.reset_mock()

    row.set_compact(False)

    assert row._compact is False
    row._info_box.set_visible.assert_called_with(True)
    row.set_tooltip_text.assert_called_with(None)
    row.update_status.assert_called_once()


def test_restore_noop_when_never_compact():
    row, _ = _make('icon')
    row.set_compact(False)  # was never compact
    # Nothing to restore: update_status not called, still not compact.
    row.update_status.assert_not_called()
    assert row._compact is False
