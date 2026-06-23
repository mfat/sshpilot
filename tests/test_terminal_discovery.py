"""Tests for terminal discovery across regular and split-view tabs."""

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _consistent_terminal_modules():
    """TerminalManager binds ``TerminalWidget``/``SplitViewTab`` at import time and
    uses them in ``isinstance`` checks. If a sibling test re-imported
    ``sshpilot.terminal`` (or ``split_view``) as a fresh module after
    ``terminal_manager`` captured the old classes, those checks silently fail and
    regular-tab terminals stop being recognised. Purge the trio so each test
    re-imports a mutually consistent set.
    """
    for name in ("sshpilot.terminal_manager", "sshpilot.terminal", "sshpilot.split_view"):
        sys.modules.pop(name, None)
    yield


def _ensure_cairo_stub():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()


def _ssh_terminal(nickname='server'):
    _ensure_cairo_stub()
    from sshpilot.terminal import TerminalWidget

    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.backend = types.SimpleNamespace(feed_child=lambda data: None)
    terminal.connection = types.SimpleNamespace(
        nickname=nickname,
        hostname=f'{nickname}.example.com',
    )
    return terminal


def _local_terminal():
    _ensure_cairo_stub()
    from sshpilot.terminal import TerminalWidget

    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.vte = types.SimpleNamespace(feed_child=lambda data: None)
    terminal.connection = types.SimpleNamespace(nickname='Local Terminal')
    return terminal


def _make_page(child):
    return types.SimpleNamespace(get_child=lambda: child)


def _make_manager(pages, selected_index=0):
    _ensure_cairo_stub()
    from sshpilot.terminal_manager import TerminalManager

    window = types.SimpleNamespace(
        tab_view=types.SimpleNamespace(
            get_n_pages=lambda: len(pages),
            get_nth_page=lambda i: pages[i],
            get_selected_page=lambda: pages[selected_index],
        ),
    )
    manager = TerminalManager.__new__(TerminalManager)
    manager.window = window
    return manager


def test_iter_ssh_terminals_includes_regular_and_split_panes():
    from sshpilot.split_view import SplitViewTab

    regular = _ssh_terminal('regular')
    pane_a = _ssh_terminal('pane-a')
    pane_b = _ssh_terminal('pane-b')
    local = _local_terminal()

    split = SplitViewTab.__new__(SplitViewTab)
    split._panes = [
        types.SimpleNamespace(get_terminals=lambda: [pane_a]),
        types.SimpleNamespace(get_terminals=lambda: [pane_b]),
    ]

    manager = _make_manager([
        _make_page(regular),
        _make_page(local),
        _make_page(split),
    ])

    nicknames = {
        t.connection.nickname
        for t in manager.iter_ssh_terminals()
    }
    assert nicknames == {'regular', 'pane-a', 'pane-b'}


def test_get_focused_terminal_returns_split_pane_terminal():
    from sshpilot.split_view import SplitViewTab

    focused = _ssh_terminal('focused')

    split = SplitViewTab.__new__(SplitViewTab)
    split._panes = [
        types.SimpleNamespace(
            get_terminal_count=lambda: 1,
            get_terminals=lambda: [focused],
            _inner_tab_view=types.SimpleNamespace(
                get_selected_page=lambda: types.SimpleNamespace(get_child=lambda: focused),
            ),
        ),
    ]
    split._get_focused_pane = lambda: split._panes[0]

    manager = _make_manager([_make_page(split)], selected_index=0)
    assert manager.get_focused_terminal() is focused


def test_get_focused_terminal_uses_last_active_pane_when_focus_is_elsewhere():
    from sshpilot.split_view import SplitViewTab

    first = _ssh_terminal('first')
    second = _ssh_terminal('second')

    pane_first = types.SimpleNamespace(
        get_terminal_count=lambda: 1,
        get_terminals=lambda: [first],
        _inner_tab_view=types.SimpleNamespace(
            get_selected_page=lambda: types.SimpleNamespace(get_child=lambda: first),
        ),
    )
    pane_second = types.SimpleNamespace(
        get_terminal_count=lambda: 1,
        get_terminals=lambda: [second],
        _inner_tab_view=types.SimpleNamespace(
            get_selected_page=lambda: types.SimpleNamespace(get_child=lambda: second),
        ),
    )

    split = SplitViewTab.__new__(SplitViewTab)
    split._panes = [pane_first, pane_second]
    split._last_active_pane = pane_second
    split._get_focused_pane = lambda: None

    manager = _make_manager([_make_page(split)], selected_index=0)
    assert manager.get_focused_terminal() is second


def test_get_focused_terminal_returns_regular_tab_terminal():
    regular = _ssh_terminal('regular')
    manager = _make_manager([_make_page(regular)], selected_index=0)
    assert manager.get_focused_terminal() is regular


def test_broadcast_command_sends_to_split_pane_terminals():
    from sshpilot.split_view import SplitViewTab

    regular = _ssh_terminal('regular')
    pane = _ssh_terminal('pane')
    sent = []

    def record_feed(data):
        sent.append(data)

    regular.backend.feed_child = record_feed
    pane.backend.feed_child = record_feed

    split = SplitViewTab.__new__(SplitViewTab)
    split._panes = [types.SimpleNamespace(get_terminals=lambda: [pane])]

    manager = _make_manager([_make_page(regular), _make_page(split)])
    sent_count, failed_count = manager.broadcast_command('uptime')

    assert sent_count == 2
    assert failed_count == 0
    assert sent == [b'uptime\n', b'uptime\n']
