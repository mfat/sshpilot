"""Tests for minimal (label) sidebar row rendering — ConnectionRow/GroupRow.set_compact.

ConnectionRow.__init__ builds many real GTK widgets, so we bypass it with
__new__ and inject MagicMock widgets, exercising only the compact/restore
branch logic (text labels, folder glyph, tooltip, restore).
"""
import importlib
from unittest.mock import MagicMock

from sshpilot.connection_manager import Connection


class _Cfg:
    def get_setting(self, key, default=None):
        if key == 'ui.sidebar_show_connection_icon':
            return True
        if key == 'ui.sidebar_show_user_hostname':
            return True
        if key == 'ui.sidebar_show_group_icon':
            return True
        return default


def _make():
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.ConnectionRow.__new__(mod.ConnectionRow)
    row.connection = Connection({'nickname': 'Prod Web', 'host': 'h', 'user': 'a'})
    row.config = _Cfg()
    row._compact = False
    row._content_spacing_base = 12
    for name in ('_content_box', '_info_box', 'indicator_box', 'color_badge',
                 'color_dot', 'file_manager_button', 'status_icon',
                 'connection_icon', 'nickname_label', 'host_label'):
        setattr(row, name, MagicMock())
    row.set_tooltip_text = MagicMock()
    row.update_status = MagicMock()
    row.set_margin_start = MagicMock()
    row.apply_row_style = MagicMock()
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
    row._content_margin_base = 12
    row._content_spacing_base = 12
    for name in ('_content', '_info_box', 'color_dot', 'color_badge',
                 'split_view_button', 'edit_button', 'expand_button', 'icon',
                 'name_label', 'count_label'):
        setattr(row, name, MagicMock())
    row.set_tooltip_text = MagicMock()
    row.set_margin_start = MagicMock()
    row.apply_row_style = MagicMock()
    row._apply_group_display_mode = MagicMock()
    row._update_display = MagicMock()
    return row, mod


def test_group_compact_shows_bold_colored_text_only(monkeypatch):
    row, mod = _make_group()
    sentinel_rgba = object()
    monkeypatch.setattr(mod, '_resolve_group_color_by_id', lambda *a: sentinel_rgba)
    apply_color = MagicMock()
    set_fg = MagicMock()
    monkeypatch.setattr(mod, '_apply_row_color', apply_color)
    monkeypatch.setattr(mod, '_set_compact_fg_color', set_fg)

    row.set_compact(True)

    assert row._compact is True
    row.apply_row_style.assert_called_with(flat=True)
    row.icon.set_visible.assert_called_with(False)
    row.count_label.set_visible.assert_called_with(False)
    row._info_box.set_visible.assert_called_with(True)
    row.name_label.set_text.assert_called_with('Servers')
    row.name_label.set_max_width_chars.assert_called_with(mod.MINIMAL_LABEL_MAX_CHARS)
    row.name_label.add_css_class.assert_any_call('sidebar-compact-label')
    row.name_label.add_css_class.assert_any_call('sidebar-compact-group')
    row.set_tooltip_text.assert_called_with('Servers')
    apply_color.assert_called_once()
    assert apply_color.call_args.args[1:] == ('fill', None)
    # Colour goes on the label, not a folder icon.
    assert set_fg.call_args_list[-1].args == (row.name_label, sentinel_rgba)


def test_compact_connection_shows_text_only_label(monkeypatch):
    row, mod = _make()
    apply_color = MagicMock()
    monkeypatch.setattr(mod, '_apply_row_color', apply_color)

    row.set_compact(True)

    assert row._compact is True
    row.apply_row_style.assert_called_with(flat=True)
    row.host_label.set_visible.assert_called_with(False)
    row.connection_icon.set_visible.assert_called_with(False)
    row._info_box.set_visible.assert_called_with(True)
    row.nickname_label.set_text.assert_called_with('Prod Web')
    row.nickname_label.set_max_width_chars.assert_called_with(mod.MINIMAL_LABEL_MAX_CHARS)
    row.nickname_label.add_css_class.assert_called_with('sidebar-compact-label')
    row.set_tooltip_text.assert_called_with('Prod Web')
    # No avatar/initials path.
    assert not hasattr(row, '_avatar') or row._avatar is None
    apply_color.assert_called_once()
    assert apply_color.call_args.args[1:] == ('fill', None)
    row.set_margin_start.assert_called_with(0)


def test_compact_connection_uses_display_name(monkeypatch):
    row, mod = _make()
    monkeypatch.setattr(mod, '_apply_row_color', MagicMock())
    row.connection.display_name = 'Production Web'

    row.set_compact(True)

    row.nickname_label.set_text.assert_called_with('Production Web')
    row.set_tooltip_text.assert_called_with('Production Web')


def test_compact_refreshes_label_when_display_name_changes(monkeypatch):
    row, mod = _make()
    monkeypatch.setattr(mod, '_apply_row_color', MagicMock())

    row.set_compact(True)
    row.connection.display_name = 'Production Database'
    row.set_compact(True)

    row.nickname_label.set_text.assert_called_with('Production Database')


def test_configure_compact_label_defaults():
    mod = importlib.import_module('sshpilot.sidebar')
    assert mod.MINIMAL_LABEL_MAX_CHARS == 10
    label = MagicMock()
    mod._configure_compact_label(label, 'abcdefghijklmnop')
    label.set_text.assert_called_with('abcdefghijklmnop')
    label.set_max_width_chars.assert_called_with(10)
    label.add_css_class.assert_called_with('sidebar-compact-label')


def test_minimal_label_max_chars_scales_with_strip_width():
    mod = importlib.import_module('sshpilot.sidebar')
    assert mod.minimal_label_max_chars(112) == 10
    assert mod.minimal_label_max_chars(224) == 20
    assert mod.minimal_label_max_chars(56) == 5
    assert mod.minimal_label_max_chars(0) == 1
    assert mod.minimal_label_max_chars(200) == 17  # 200*10//112


def test_compact_connection_honours_max_chars(monkeypatch):
    row, mod = _make()
    monkeypatch.setattr(mod, '_apply_row_color', MagicMock())

    row.set_compact(True, max_chars=18)

    row.nickname_label.set_max_width_chars.assert_called_with(18)
    # Re-apply without an explicit budget keeps the last width-driven value.
    row.nickname_label.reset_mock()
    row.set_compact(True)
    row.nickname_label.set_max_width_chars.assert_called_with(18)


def test_compact_group_honours_max_chars(monkeypatch):
    row, mod = _make_group()
    monkeypatch.setattr(mod, '_resolve_group_color_by_id', lambda *a: None)
    monkeypatch.setattr(mod, '_apply_row_color', MagicMock())
    monkeypatch.setattr(mod, '_set_compact_fg_color', MagicMock())

    row.set_compact(True, max_chars=14)

    row.name_label.set_max_width_chars.assert_called_with(14)


def test_restore_shows_labels_and_refreshes_status(monkeypatch):
    row, mod = _make()
    monkeypatch.setattr(mod, '_apply_row_color', MagicMock())
    row.set_compact(True)
    row.update_status.reset_mock()

    row.set_compact(False)

    assert row._compact is False
    row.apply_row_style.assert_called_with()
    row._info_box.set_visible.assert_called_with(True)
    row.host_label.set_visible.assert_called_with(True)
    row.nickname_label.set_markup.assert_called()
    row.set_tooltip_text.assert_called_with(None)
    row.update_status.assert_called_once()


def test_restore_reapplies_nested_indentation(monkeypatch):
    row, mod = _make()
    monkeypatch.setattr(mod, '_apply_row_color', MagicMock())
    row.set_compact(True)
    row._apply_group_display_mode.reset_mock()
    row.set_compact(False)
    row._apply_group_display_mode.assert_called_once()


def test_restore_noop_when_never_compact():
    row, _ = _make()
    row.set_compact(False)  # was never compact
    row.update_status.assert_not_called()
    assert row._compact is False


def test_compact_online_tints_label(monkeypatch):
    row, mod = _make()
    monkeypatch.setattr(mod, '_apply_row_color', MagicMock())
    row._is_online = MagicMock(return_value=True)
    row.set_compact(True)
    row.nickname_label.add_css_class.assert_any_call('sidebar-compact-online')
