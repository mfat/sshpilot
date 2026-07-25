"""Checks for the files panel below the terminal (set_file_panel/clear_file_panel).

Duck-typed like test_fm_tab_teardown.py: verifies teardown ordering and state
bookkeeping only — real paned geometry stays in manual QA on a GTK build.
"""

import sys
import types


def _terminal_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import terminal
    return terminal


def _fake_terminal(order):
    paned = types.SimpleNamespace(
        set_end_child=lambda c: order.append(('end_child', c)),
        set_start_child=lambda c: order.append(('start_child', c)),
    )
    fake = types.SimpleNamespace(
        _file_panel=object(),
        _file_panel_paned=paned,
        _file_panel_teardown=lambda: order.append('teardown'),
        container_box=object(),
        remove=lambda w: order.append(('remove', w)),
        append=lambda w: order.append(('append', w)),
    )
    return fake, paned


def test_clear_file_panel_runs_teardown_before_detach():
    tm = _terminal_module()
    order = []
    fake, paned = _fake_terminal(order)

    tm.TerminalWidget.clear_file_panel(fake)

    # Controller disposal must happen while the tree is intact (segfault guard),
    # then the paned is dismantled and the terminal-only layout restored.
    assert order[0] == 'teardown'
    assert ('remove', paned) in order
    assert order[-1] == ('append', fake.container_box)
    assert fake._file_panel is None
    assert fake._file_panel_paned is None
    assert fake._file_panel_teardown is None


def test_clear_file_panel_is_idempotent():
    tm = _terminal_module()
    order = []
    fake, _paned = _fake_terminal(order)

    tm.TerminalWidget.clear_file_panel(fake)
    order.clear()
    tm.TerminalWidget.clear_file_panel(fake)

    assert order == []


def test_has_file_panel_reflects_state():
    tm = _terminal_module()
    fake = types.SimpleNamespace(_file_panel=None)
    assert tm.TerminalWidget.has_file_panel(fake) is False
    fake._file_panel = object()
    assert tm.TerminalWidget.has_file_panel(fake) is True
