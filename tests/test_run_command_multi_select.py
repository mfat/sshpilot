"""Routing tests for Run Command on multi-selected hosts."""

import pytest

# Import manually instead of importorskip: when sibling tests have replaced
# the Gtk stub in sys.modules, these imports raise AttributeError (not
# ImportError), which importorskip would report as a collection error.
try:
    import sshpilot.command_blocks as command_blocks
    import sshpilot.window as window_mod
except Exception:  # pragma: no cover - depends on test execution order
    command_blocks = window_mod = None

pytestmark = pytest.mark.skipif(
    command_blocks is None or window_mod is None,
    reason="GTK stubs unavailable or polluted by sibling tests",
)


def _panel_with_recorders():
    panel = command_blocks.CommandBlocksPanel.__new__(command_blocks.CommandBlocksPanel)
    calls = []
    panel._connect_and_feed = lambda conn, text, cmd_id=None: calls.append(
        ('single', conn, text, cmd_id))
    panel._feed_connections_in_split_view = lambda conns, title, text, cmd_id=None: calls.append(
        ('split', list(conns), title, text, cmd_id))
    panel._feed_group_in_split_view = lambda group, text, cmd_id=None: calls.append(
        ('group', group, text, cmd_id))
    return panel, calls


def test_dispatch_multiple_connections_opens_split_view():
    panel, calls = _panel_with_recorders()
    c1, c2 = object(), object()
    panel._dispatch_to_target('uptime', 'cmd1', connections=[c1, c2])
    assert len(calls) == 1
    kind, conns, title, text, cmd_id = calls[0]
    assert kind == 'split'
    assert conns == [c1, c2]
    assert title
    assert text == 'uptime'
    assert cmd_id == 'cmd1'


def test_dispatch_single_item_list_degrades_to_one_tab():
    panel, calls = _panel_with_recorders()
    c1 = object()
    panel._dispatch_to_target('uptime', None, connections=[c1])
    assert calls == [('single', c1, 'uptime', None)]


def test_dispatch_single_connection_unchanged():
    panel, calls = _panel_with_recorders()
    c1 = object()
    panel._dispatch_to_target('uptime', 'cmd1', connection=c1)
    assert calls == [('single', c1, 'uptime', 'cmd1')]


def test_dispatch_group_unchanged():
    panel, calls = _panel_with_recorders()
    group = {'name': 'prod', 'connections': ['a', 'b']}
    panel._dispatch_to_target('uptime', None, group=group)
    assert calls == [('group', group, 'uptime', None)]


def test_dispatch_empty_connections_falls_through_to_connection():
    panel, calls = _panel_with_recorders()
    c1 = object()
    panel._dispatch_to_target('uptime', None, connections=[], connection=c1)
    assert calls == [('single', c1, 'uptime', None)]


class _FakePanel:
    def __init__(self):
        self.calls = []

    def show_command_picker_for_target(self, anchor, **kwargs):
        self.calls.append((anchor, kwargs))


class _FakeWindow:
    if window_mod is not None:
        on_run_command_action = window_mod.MainWindow.on_run_command_action

    def __init__(self):
        self.command_blocks_panel = _FakePanel()
        self._context_menu_row = object()
        self._context_menu_connection = None
        self._context_menu_connections = None
        self._context_menu_group_row = None


def test_action_prefers_multi_snapshot_over_clicked_connection():
    win = _FakeWindow()
    c1, c2 = object(), object()
    win._context_menu_connections = [c1, c2]
    # Right-click in a multi-selection still sets the singular attribute.
    win._context_menu_connection = c1

    win.on_run_command_action()

    assert len(win.command_blocks_panel.calls) == 1
    anchor, kwargs = win.command_blocks_panel.calls[0]
    assert anchor is win._context_menu_row
    assert kwargs == {'connections': [c1, c2]}


def test_action_snapshot_is_copied_by_value():
    win = _FakeWindow()
    c1, c2 = object(), object()
    win._context_menu_connections = [c1, c2]

    win.on_run_command_action()
    # Simulate the context-menu popover closing and clearing the snapshot.
    win._context_menu_connections = None

    _, kwargs = win.command_blocks_panel.calls[0]
    assert kwargs['connections'] == [c1, c2]


def test_action_single_connection_path_unchanged():
    win = _FakeWindow()
    c1 = object()
    win._context_menu_connection = c1

    win.on_run_command_action()

    _, kwargs = win.command_blocks_panel.calls[0]
    assert kwargs == {'connection': c1}
