"""Pinned Local Terminal row at the top of the connection list."""

import importlib
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def sidebar_mod():
    return importlib.import_module("sshpilot.sidebar")


@pytest.fixture
def window_mod():
    return importlib.import_module("sshpilot.window")


class _Cfg:
    def get_setting(self, key, default=None):
        return default


class _CfgWithLocalTerminalRow(_Cfg):
    def get_setting(self, key, default=None):
        if key == "ui.sidebar_show_local_terminal":
            return True
        return super().get_setting(key, default)


def test_local_terminal_row_matches_aliases(sidebar_mod):
    matches = sidebar_mod.local_terminal_row_matches
    assert matches("") is True
    assert matches("local") is True
    assert matches("terminal") is True
    assert matches("shell") is True
    assert matches("local terminal") is True
    assert matches("prod") is False
    assert matches("web db") is False


def test_rebuild_pins_local_terminal_row_first(window_mod, sidebar_mod, monkeypatch):
    """Empty search keeps the local-terminal row as the first list child."""

    class DummyList:
        def __init__(self):
            self.children = []

        def get_first_child(self):
            return self.children[0] if self.children else None

        def remove(self, child):
            self.children.remove(child)

        def append(self, child):
            self.children.append(child)

        def get_selected_rows(self):
            return []

        def get_selected_row(self):
            return None

    class DummyCM:
        def get_connections(self):
            return []

        def get_metadata(self, _nickname):
            return {}

    class DummyGM:
        groups = {}
        root_connections = []

        def get_all_groups(self):
            return []

        def get_group_hierarchy(self):
            return []

        def get_connection_groups(self, _nick):
            return []

    created = []

    class StubLocalRow:
        is_local_terminal_row = True

        def __init__(self, config=None):
            self.config = config
            created.append(self)

    monkeypatch.setattr(sidebar_mod, "LocalTerminalRow", StubLocalRow)
    # Import path used inside _add_local_terminal_row_if_visible
    import sshpilot.sidebar as sidebar_pkg

    monkeypatch.setattr(sidebar_pkg, "LocalTerminalRow", StubLocalRow)

    win = window_mod.MainWindow.__new__(window_mod.MainWindow)
    win.connection_list = DummyList()
    win.connection_rows = {}
    win.connection_scrolled = None
    win.connection_manager = DummyCM()
    win.group_manager = DummyGM()
    win.config = _CfgWithLocalTerminalRow()
    win.search_entry = types.SimpleNamespace(get_text=lambda: "")
    win._hide_hosts = False
    win._tag_filter = None
    win._search_popup = None
    win._sidebar_minimal = False
    win._attach_sidebar_forwarding_rules = lambda *_a, **_k: None
    win._refresh_sidebar_forwarding_rules = lambda *_a, **_k: None
    win._finish_rebuild = lambda *_a, **_k: None
    win._apply_sidebar_minimal_rows = lambda *_a, **_k: None
    win._apply_sidebar_row_actions = lambda *_a, **_k: None

    win.rebuild_connection_list()

    assert len(created) == 1
    assert win.connection_list.children[0] is created[0]


def test_rebuild_omits_local_terminal_row_when_pref_disabled(
    window_mod, sidebar_mod, monkeypatch
):
    """ui.sidebar_show_local_terminal=False skips the pinned row."""

    class OffCfg:
        def get_setting(self, key, default=None):
            if key == "ui.sidebar_show_local_terminal":
                return False
            return default

    created = []

    class StubLocalRow:
        is_local_terminal_row = True

        def __init__(self, config=None):
            created.append(self)

    import sshpilot.sidebar as sidebar_pkg

    monkeypatch.setattr(sidebar_pkg, "LocalTerminalRow", StubLocalRow)

    class DummyList:
        def __init__(self):
            self.children = []

        def get_first_child(self):
            return self.children[0] if self.children else None

        def remove(self, child):
            self.children.remove(child)

        def append(self, child):
            self.children.append(child)

        def get_selected_rows(self):
            return []

        def get_selected_row(self):
            return None

    class DummyCM:
        def get_connections(self):
            return []

        def get_metadata(self, _nickname):
            return {}

    class DummyGM:
        groups = {}
        root_connections = []

        def get_all_groups(self):
            return []

        def get_group_hierarchy(self):
            return []

        def get_connection_groups(self, _nick):
            return []

    win = window_mod.MainWindow.__new__(window_mod.MainWindow)
    win.connection_list = DummyList()
    win.connection_rows = {}
    win.connection_scrolled = None
    win.connection_manager = DummyCM()
    win.group_manager = DummyGM()
    win.config = OffCfg()
    win.search_entry = types.SimpleNamespace(get_text=lambda: "")
    win._hide_hosts = False
    win._tag_filter = None
    win._search_popup = None
    win._sidebar_minimal = False
    win._attach_sidebar_forwarding_rules = lambda *_a, **_k: None
    win._refresh_sidebar_forwarding_rules = lambda *_a, **_k: None
    win._finish_rebuild = lambda *_a, **_k: None
    win._apply_sidebar_minimal_rows = lambda *_a, **_k: None
    win._apply_sidebar_row_actions = lambda *_a, **_k: None

    win.rebuild_connection_list()

    assert created == []
    assert win.connection_list.children == []


def test_rebuild_hides_local_terminal_row_when_search_mismatches(
    window_mod, sidebar_mod, monkeypatch
):
    created = []

    class StubLocalRow:
        is_local_terminal_row = True

        def __init__(self, config=None):
            created.append(self)

    import sshpilot.sidebar as sidebar_pkg

    monkeypatch.setattr(sidebar_pkg, "LocalTerminalRow", StubLocalRow)

    class DummyList:
        def __init__(self):
            self.children = []

        def get_first_child(self):
            return self.children[0] if self.children else None

        def remove(self, child):
            self.children.remove(child)

        def append(self, child):
            self.children.append(child)

        def get_selected_rows(self):
            return []

    class DummyCM:
        def get_connections(self):
            return []

        def get_metadata(self, _nickname):
            return {}

    class DummyGM:
        groups = {}
        root_connections = []

        def get_all_groups(self):
            return []

        def get_group_hierarchy(self):
            return []

        def get_connection_groups(self, _nick):
            return []

    win = window_mod.MainWindow.__new__(window_mod.MainWindow)
    win.connection_list = DummyList()
    win.connection_rows = {}
    win.connection_scrolled = None
    win.connection_manager = DummyCM()
    win.group_manager = DummyGM()
    win.config = _CfgWithLocalTerminalRow()
    win.search_entry = types.SimpleNamespace(get_text=lambda: "prod-only")
    win._hide_hosts = False
    win._tag_filter = None
    win._search_popup = None
    win._sidebar_minimal = False
    win._attach_sidebar_forwarding_rules = lambda *_a, **_k: None
    win._refresh_sidebar_forwarding_rules = lambda *_a, **_k: None
    win._finish_rebuild = lambda *_a, **_k: None

    win.rebuild_connection_list()

    assert created == []
    assert win.connection_list.children == []


def test_activation_opens_local_terminal(window_mod):
    win = window_mod.MainWindow.__new__(window_mod.MainWindow)
    calls = []
    win._dismiss_search_popup = lambda: calls.append("dismiss")
    win._cycle_local_terminal_tabs_or_open = lambda: calls.append("cycle")

    row = types.SimpleNamespace(is_local_terminal_row=True)
    win.on_connection_activated(None, row)

    assert calls == ["dismiss", "cycle"]


def test_cycle_local_terminal_tabs_wraps(window_mod):
    """Activating the row focuses the next local shell tab, then wraps."""
    from sshpilot.window_tabs import WindowTabsMixin

    class FakePage:
        def __init__(self, child):
            self._child = child

        def get_child(self):
            return self._child

    class FakeTabView:
        def __init__(self, pages):
            self._pages = list(pages)
            self.selected = None

        def get_n_pages(self):
            return len(self._pages)

        def get_nth_page(self, i):
            return self._pages[i]

        def get_selected_page(self):
            return self.selected

        def set_selected_page(self, page):
            self.selected = page

    term_a = types.SimpleNamespace(_is_local_terminal=lambda: True)
    term_b = types.SimpleNamespace(_is_local_terminal=lambda: True)
    ssh = types.SimpleNamespace(_is_local_terminal=lambda: False)
    pages = [FakePage(term_a), FakePage(ssh), FakePage(term_b)]
    tab_view = FakeTabView(pages)
    tab_view.selected = pages[0]

    win = window_mod.MainWindow.__new__(window_mod.MainWindow)
    win.tab_view = tab_view
    win._return_to_tab_view_if_welcome = lambda: None
    win._close_search_if_open = lambda: None
    win._page_for_child = lambda child: next(
        (p for p in pages if p.get_child() is child), None
    )
    focused = []
    win._focus_terminal_widget = lambda term: focused.append(term)
    opened = []
    win.terminal_manager = types.SimpleNamespace(
        show_local_terminal=lambda: opened.append(True) or True
    )

    WindowTabsMixin._cycle_local_terminal_tabs_or_open(win)
    assert tab_view.selected is pages[2]
    assert focused == [term_b]
    assert opened == []

    WindowTabsMixin._cycle_local_terminal_tabs_or_open(win)
    assert tab_view.selected is pages[0]
    assert focused == [term_b, term_a]


def test_cycle_local_terminal_opens_when_none(window_mod):
    from sshpilot.window_tabs import WindowTabsMixin

    class FakeTabView:
        def get_n_pages(self):
            return 0

        def get_selected_page(self):
            return None

    win = window_mod.MainWindow.__new__(window_mod.MainWindow)
    win.tab_view = FakeTabView()
    win._return_to_tab_view_if_welcome = lambda: None
    win._close_search_if_open = lambda: None
    opened = []
    win.terminal_manager = types.SimpleNamespace(
        show_local_terminal=lambda: opened.append(True) or True
    )

    WindowTabsMixin._cycle_local_terminal_tabs_or_open(win)
    assert opened == [True]


def test_cycle_skips_ssh_to_localhost(window_mod):
    """Saved SSH hosts named localhost must not join the local-shell cycle."""
    from sshpilot.window_tabs import WindowTabsMixin

    class FakePage:
        def __init__(self, child):
            self._child = child

        def get_child(self):
            return self._child

    class FakeTabView:
        def __init__(self, pages):
            self._pages = list(pages)
            self.selected = None

        def get_n_pages(self):
            return len(self._pages)

        def get_nth_page(self, i):
            return self._pages[i]

        def get_selected_page(self):
            return self.selected

        def set_selected_page(self, page):
            self.selected = page

    ssh_localhost = types.SimpleNamespace(
        _is_local_terminal=lambda: False,  # Connection has protocol
    )
    local_shell = types.SimpleNamespace(_is_local_terminal=lambda: True)
    pages = [FakePage(ssh_localhost), FakePage(local_shell)]
    tab_view = FakeTabView(pages)
    tab_view.selected = pages[0]

    win = window_mod.MainWindow.__new__(window_mod.MainWindow)
    win.tab_view = tab_view
    win._return_to_tab_view_if_welcome = lambda: None
    win._close_search_if_open = lambda: None
    win._page_for_child = lambda child: next(
        (p for p in pages if p.get_child() is child), None
    )
    focused = []
    win._focus_terminal_widget = lambda term: focused.append(term)
    win.terminal_manager = types.SimpleNamespace(
        show_local_terminal=lambda: False
    )

    WindowTabsMixin._cycle_local_terminal_tabs_or_open(win)
    assert focused == [local_shell]
    assert tab_view.selected is pages[1]


def test_is_local_terminal_rejects_ssh_localhost():
    from sshpilot.terminal import TerminalWidget
    from sshpilot.connection_manager import Connection

    term = TerminalWidget.__new__(TerminalWidget)
    term.connection = Connection(
        {"nickname": "box", "hostname": "localhost", "username": "me"}
    )
    assert term._is_local_terminal() is False

    term.connection = types.SimpleNamespace(
        hostname="localhost", is_local_shell=True
    )
    assert term._is_local_terminal() is True

    term.connection = types.SimpleNamespace(hostname="localhost")
    assert term._is_local_terminal() is True



def test_middle_click_opens_local_terminal(sidebar_mod, monkeypatch):
    from sshpilot.sidebar import Gdk

    class FakeGesture:
        instances = []

        def __init__(self):
            self.button = None
            self.state = None
            self.handlers = {}
            FakeGesture.instances.append(self)

        def set_button(self, button):
            self.button = button

        def set_propagation_phase(self, phase):
            pass

        def set_state(self, state):
            self.state = state

        def connect(self, signal, handler):
            self.handlers[signal] = handler

        def press(self, x=0.0, y=0.0, n_press=1):
            self.handlers["pressed"](self, n_press, x, y)

    FakeGesture.instances = []
    monkeypatch.setattr(sidebar_mod.Gtk, "GestureClick", FakeGesture)

    calls = []
    row = types.SimpleNamespace(is_local_terminal_row=True)
    window = types.SimpleNamespace(
        connection_list=types.SimpleNamespace(
            add_controller=lambda c: None,
            get_selected_row=lambda: None,
        ),
        add_controller=lambda c: None,
        _pick_connection_list_row=lambda x, y: row,
        terminal_manager=types.SimpleNamespace(
            show_local_terminal=lambda: calls.append("local") or True
        ),
        on_open_new_connection_action=lambda *_a: calls.append("conn"),
        on_open_group_in_tabs_action=lambda *_a: calls.append("group"),
    )
    sidebar_mod._attach_connection_list_context_menu(window)

    gesture = next(g for g in FakeGesture.instances if g.button == Gdk.BUTTON_MIDDLE)
    gesture.press()

    assert calls == ["local"]
    assert gesture.state is sidebar_mod.Gtk.EventSequenceState.CLAIMED


def test_local_terminal_row_compact_hides_icon_and_subtitle(sidebar_mod, monkeypatch):
    row = sidebar_mod.LocalTerminalRow.__new__(sidebar_mod.LocalTerminalRow)
    row.config = _Cfg()
    row._compact = False
    row._content_spacing_base = 12
    for name in ("_content_box", "_info_box", "connection_icon", "nickname_label", "host_label"):
        setattr(row, name, MagicMock())
    row.apply_row_style = MagicMock()
    row._apply_group_color_style = MagicMock()

    configure = MagicMock()
    monkeypatch.setattr(sidebar_mod, "_configure_compact_label", configure)

    row.set_compact(True, max_chars=8)

    assert row._compact is True
    row.connection_icon.set_visible.assert_called_with(False)
    row.host_label.set_visible.assert_called_with(False)
    configure.assert_called_once()
    assert configure.call_args.args[1] == "Local Terminal"
    row._apply_group_color_style.assert_called_once()


def test_local_terminal_row_reserves_connection_row_action_height(sidebar_mod):
    """Placeholder action slot mirrors ConnectionRow's height-only Manage Files park."""
    import inspect

    src = inspect.getsource(sidebar_mod.LocalTerminalRow.__init__)
    assert "_make_row_action_slot" in src
    assert "ROW_ACTION_SLOT_EMPTY" in src
    assert "_height_slot" in src
    assert "file-manager-button" in src


def test_local_terminal_row_gets_color_bar_in_bar_mode(sidebar_mod):
    """Accent-bar mode must put .color-bar on the pinned row for selection CSS."""

    class BarCfg:
        def get_setting(self, key, default=None):
            if key == "ui.group_color_display":
                return "bar"
            return default

    row = sidebar_mod.LocalTerminalRow.__new__(sidebar_mod.LocalTerminalRow)
    row.config = BarCfg()
    row._compact = False
    classes = set()
    row.add_css_class = classes.add
    row.remove_css_class = classes.discard

    row._apply_group_color_style()
    assert "color-bar" in classes

    row.config = _Cfg()
    row._apply_group_color_style()
    assert "color-bar" not in classes

    row.config = BarCfg()
    row._compact = True
    row._apply_group_color_style()
    assert "color-bar" not in classes

