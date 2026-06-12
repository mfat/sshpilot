"""Multi-select routing for the keyboard open-new-connection paths."""

import pytest

# Import manually instead of importorskip: when sibling tests have replaced
# the Gtk stub in sys.modules, these imports raise AttributeError (not
# ImportError), which importorskip would report as a collection error.
try:
    import sshpilot.actions as actions_mod
except Exception:  # pragma: no cover - depends on test execution order
    actions_mod = None

pytestmark = pytest.mark.skipif(
    actions_mod is None,
    reason="GTK stubs unavailable or polluted by sibling tests",
)


class _FakeTerminalManager:
    def __init__(self, fail_for=()):
        self.opened = []
        self._fail_for = set(fail_for)

    def connect_to_host(self, connection, force_new=False):
        if connection in self._fail_for:
            raise RuntimeError("boom")
        self.opened.append((connection, force_new))


class _FakeWindow:
    if actions_mod is not None:
        on_open_new_connection_tab_action = (
            actions_mod.WindowActions.on_open_new_connection_tab_action
        )
        on_open_new_connection_action = (
            actions_mod.WindowActions.on_open_new_connection_action
        )
        _open_new_connection_tabs = actions_mod.WindowActions._open_new_connection_tabs

    def __init__(self, selected=(), fail_for=()):
        self._selected = list(selected)
        self.terminal_manager = _FakeTerminalManager(fail_for=fail_for)
        self.dialog_shown = False
        self._context_menu_connections = None
        self._context_menu_connection = None

    # Accessors normally provided by MainWindow
    def _get_selected_connection_rows(self):
        return list(self._selected)

    def _connections_from_rows(self, rows):
        result = []
        for row in rows:
            if row not in result:
                result.append(row)
        return result

    def show_connection_dialog(self):
        self.dialog_shown = True


def test_tab_action_opens_all_selected():
    c1, c2, c3 = object(), object(), object()
    win = _FakeWindow(selected=[c1, c2, c3])
    win.on_open_new_connection_tab_action(None, None)
    assert win.terminal_manager.opened == [(c1, True), (c2, True), (c3, True)]
    assert not win.dialog_shown


def test_tab_action_empty_selection_falls_back_to_dialog():
    win = _FakeWindow(selected=[])
    win.on_open_new_connection_tab_action(None, None)
    assert win.terminal_manager.opened == []
    assert win.dialog_shown


def test_open_tabs_isolates_per_connection_failures():
    c1, c2, c3 = object(), object(), object()
    win = _FakeWindow(fail_for=[c2])
    win._open_new_connection_tabs([c1, c2, c3])
    assert win.terminal_manager.opened == [(c1, True), (c3, True)]


def test_menu_action_routes_through_shared_helper():
    c1, c2 = object(), object()
    win = _FakeWindow()
    win._context_menu_connections = [c1, c2]
    win.on_open_new_connection_action(None, None)
    assert win.terminal_manager.opened == [(c1, True), (c2, True)]
