"""A split view unwraps its final terminal into a normal tab."""

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_removing_penultimate_pane_schedules_auto_collapse(monkeypatch):
    from sshpilot import split_view as module

    removed = object()
    survivor = SimpleNamespace(get_terminal_count=lambda: 1)
    split = SimpleNamespace(
        _panes=[removed, survivor],
        _tab_page=object(),
        _rebuild_layout=MagicMock(),
        _update_tab_title=MagicMock(),
        _collapse_pending=False,
        _collapse_single_terminal_pane=MagicMock(),
        window=SimpleNamespace(),
    )
    idle_add = MagicMock()
    monkeypatch.setattr(module.GLib, 'idle_add', idle_add)

    module.SplitViewTab.remove_pane(split, removed)

    assert split._panes == [survivor]
    assert split._collapse_pending is True
    idle_add.assert_called_once_with(split._collapse_single_terminal_pane)


def test_single_surviving_pane_collapses_without_cleaning_terminal(monkeypatch):
    from sshpilot.split_view import SplitViewTab

    terminal = MagicMock()
    terminal.connection = SimpleNamespace(nickname='Server')
    inner_page = SimpleNamespace(get_title=lambda: 'Custom pane title')
    pane = SimpleNamespace(
        get_terminals=lambda: [terminal],
        _inner_tab_view=SimpleNamespace(get_page=lambda child: inner_page),
    )
    split_page = object()

    class Page:
        def set_title(self, title):
            self.title = title

        def set_icon(self, _icon):
            pass

    normal_page = Page()
    tab_view = SimpleNamespace(
        get_page_position=lambda page: 2,
        get_n_pages=lambda: 4,
        close_page=MagicMock(),
        insert=MagicMock(return_value=normal_page),
        append=MagicMock(return_value=normal_page),
        set_selected_page=MagicMock(),
    )
    window = SimpleNamespace(
        tab_view=tab_view,
        _suppress_close_confirmation=False,
        _update_tab_button_visibility=MagicMock(),
    )
    split = SimpleNamespace(
        _collapse_pending=True,
        _panes=[pane],
        _tab_page=split_page,
        window=window,
    )
    monkeypatch.setattr(
        'sshpilot.icon_utils.new_gicon_from_icon_name', lambda _name: object()
    )

    result = SplitViewTab._collapse_single_terminal_pane(split)

    assert result is False
    terminal.unparent.assert_called_once_with()
    tab_view.close_page.assert_called_once_with(split_page)
    tab_view.insert.assert_called_once_with(terminal, 2)
    tab_view.append.assert_not_called()
    assert normal_page.title == 'Custom pane title'
    tab_view.set_selected_page.assert_called_once_with(normal_page)
    assert window._suppress_close_confirmation is False
