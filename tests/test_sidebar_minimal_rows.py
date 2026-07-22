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
    row._resolve_group_color = MagicMock(return_value=None)
    return row, mod


def _make_group():
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.GroupRow.__new__(mod.GroupRow)
    row.group_info = {'id': 'g1', 'name': 'Servers'}
    row.group_id = 'g1'
    row.group_manager = MagicMock()
    row.group_manager.config = _Cfg()
    row._compact = False
    row._avatar = None
    for name in ('_content', '_info_box', 'color_dot', 'color_badge',
                 'split_view_button', 'edit_button', 'expand_button', 'icon'):
        setattr(row, name, MagicMock())
    row.set_tooltip_text = MagicMock()
    row.set_margin_start = MagicMock()
    row._apply_group_display_mode = MagicMock()
    row._update_display = MagicMock()
    return row, mod


def test_group_compact_creates_folder_icon_avatar(monkeypatch):
    row, mod = _make_group()
    fake_avatar = MagicMock()
    make_avatar = MagicMock(return_value=fake_avatar)
    monkeypatch.setattr(mod, '_make_avatar', make_avatar)
    # Color styling touches real GTK CSS on the widget; stub it out.
    sentinel_rgba = object()
    monkeypatch.setattr(mod, '_resolve_group_color_by_id', lambda *a: sentinel_rgba)
    apply_color = MagicMock()
    set_avatar_color = MagicMock()
    monkeypatch.setattr(mod, '_apply_row_color', apply_color)
    monkeypatch.setattr(mod, '_set_avatar_color', set_avatar_color)

    row.set_compact(True)

    assert row._avatar is fake_avatar
    # Groups use a folder icon, never initials.
    assert make_avatar.call_args.kwargs == {'icon_name': 'folder-symbolic'}
    row._content.prepend.assert_called_with(fake_avatar)
    row.icon.set_visible.assert_called_with(False)
    row.set_tooltip_text.assert_called_with('Servers')
    # The row keeps no color treatment; the group color goes on the avatar.
    apply_color.assert_called_once()
    assert apply_color.call_args.args[1:] == ('fill', None)
    set_avatar_color.assert_called_once_with(fake_avatar, sentinel_rgba)


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
    make_avatar = MagicMock(return_value=fake_avatar)
    monkeypatch.setattr(mod, '_make_avatar', make_avatar)

    row.set_compact(True)

    assert row._avatar is fake_avatar
    # Initials derived from the nickname, not a hashed AdwAvatar.
    assert make_avatar.call_args.kwargs == {'initials': 'PW'}
    row._content_box.prepend.assert_called_with(fake_avatar)
    fake_avatar.set_visible.assert_called_with(True)
    row.connection_icon.set_visible.assert_called_with(False)


def test_compact_avatar_filled_with_group_color(monkeypatch):
    row, mod = _make('initials')
    monkeypatch.setattr(mod, '_make_avatar', MagicMock(return_value=MagicMock()))
    rgba = object()
    row._resolve_group_color = MagicMock(return_value=rgba)
    set_avatar_color = MagicMock()
    monkeypatch.setattr(mod, '_set_avatar_color', set_avatar_color)

    row.set_compact(True)

    # The group color fills the avatar regardless of the color display mode.
    set_avatar_color.assert_called_once_with(row._avatar, rgba)


def test_avatar_initials():
    mod = importlib.import_module('sshpilot.sidebar')
    assert mod._avatar_initials('Prod Web') == 'PW'
    assert mod._avatar_initials('sdf') == 'SD'
    assert mod._avatar_initials('x') == 'X'
    assert mod._avatar_initials('  ') == '?'
    assert mod._avatar_initials('alpha beta gamma') == 'AB'


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
