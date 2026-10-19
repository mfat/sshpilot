"""The header-bar local-terminal button and the tab bar's new-tab plus.

The preference allows the control. With no terminal open it stays in the
header bar. Once a terminal is open, that button hides and the plus prefixed
to the tab bar (the start action) takes the same action.
"""

import types

from sshpilot.window_tabs import WindowTabsMixin


class _Flag:
    def __init__(self, visible):
        self.visible = visible

    def set_visible(self, visible):
        self.visible = bool(visible)

    def get_visible(self):
        return self.visible


class _Page:
    def __init__(self, child):
        self._child = child

    def get_child(self):
        return self._child


class _View:
    def __init__(self, pages):
        self._pages = list(pages)

    def get_pages(self):
        return list(self._pages)


def _window(*, allowed=True, pages=()):
    window = WindowTabsMixin()
    window.config = types.SimpleNamespace(
        get_setting=lambda key, default=None: (
            allowed if key == 'ui.headerbar_show_local_terminal' else default
        ),
    )
    window.tab_view = _View(pages)
    window._is_start_tab_page = lambda page: (
        page is not None and page is getattr(window, '_start_tab_page', None)
    )
    window._headerbar_local_terminal_button = _Flag(True)
    window._tab_bar_new_tab_button = _Flag(False)
    window._file_manager_embeds_for_child = (
        lambda child: list(getattr(child, 'embeds', ()) or ())
    )
    return window


def _apply(window):
    window._update_local_terminal_button_visibility()
    return (
        window._headerbar_local_terminal_button.get_visible(),
        window._tab_bar_new_tab_button.get_visible(),
    )


def test_header_button_shows_when_no_terminal_is_open(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.window_tabs._is_terminal_widget', lambda widget: False,
    )
    window = _window(pages=[_Page(types.SimpleNamespace())])

    assert _apply(window) == (True, False)


def test_plus_replaces_header_button_when_a_terminal_is_open(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.window_tabs._is_terminal_widget',
        lambda widget: getattr(widget, 'is_term', False),
    )
    window = _window(pages=[_Page(types.SimpleNamespace(is_term=True))])

    assert _apply(window) == (False, True)


def test_preference_off_hides_both_buttons(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.window_tabs._is_terminal_widget',
        lambda widget: getattr(widget, 'is_term', False),
    )
    window = _window(
        allowed=False,
        pages=[_Page(types.SimpleNamespace(is_term=True))],
    )

    assert _apply(window) == (False, False)


def test_start_page_does_not_count_as_an_open_terminal(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.window_tabs._is_terminal_widget',
        lambda widget: getattr(widget, 'is_term', False),
    )
    start = _Page(types.SimpleNamespace(is_term=True))
    window = _window(pages=[start])
    window._start_tab_page = start

    assert _apply(window) == (True, False)


def test_split_pane_with_a_terminal_counts(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.window_tabs._is_terminal_widget', lambda widget: False,
    )
    split = types.SimpleNamespace(get_all_terminals=lambda: [object()])
    window = _window(pages=[_Page(split)])

    assert window._has_open_terminal() is True


def test_empty_split_keeps_the_header_button(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.window_tabs._is_terminal_widget', lambda widget: False,
    )
    split = types.SimpleNamespace(get_all_terminals=list)
    window = _window(pages=[_Page(split)])

    assert _apply(window) == (True, False)


def test_file_manager_terminal_panel_counts(monkeypatch):
    monkeypatch.setattr(
        'sshpilot.window_tabs._is_terminal_widget', lambda widget: False,
    )
    embed = types.SimpleNamespace(has_terminal_panel=lambda: True)
    child = types.SimpleNamespace(embeds=[embed])
    window = _window(pages=[_Page(child)])

    assert window._has_open_terminal() is True
