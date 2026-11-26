"""Guards for the WindowTabsMixin extraction.

The tab/pane lifecycle methods were moved verbatim out of window.py into
sshpilot/window_tabs.py as a mixin. These checks ensure every method resolves
to the mixin module (so no stray copy in the window.py body silently shadows
it) and that the mixin is wired into the MRO. The hardened teardown behavior is
covered separately by tests/test_fm_tab_teardown.py.
"""

import sys
import types

import pytest


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


# Every method moved into the mixin — the full set, not a sample, so a body
# copy shadowing any one of them via MRO is caught (the dead-duplicate hazard).
_TAB_METHODS = (
    "_on_tab_close_confirmed",
    "_close_tab",
    "_on_tab_bar_pressed",
    "_on_tab_bar_capture_press",
    "_restore_focus_after_tab_click",
    "_show_tab_rename_popover",
    "_apply_tab_title",
    "_build_tab_context_menus",
    "_on_tab_bar_secondary_press",
    "_on_tab_setup_menu",
    "_file_manager_embed_for_child",
    "_teardown_file_manager_embed",
    "_teardown_embed_controller",
    "_teardown_all_file_manager_tabs",
    "_enabled_tab_actions",
    "_tab_menu_target",
    "run_tab_menu_action_for",
    "_on_tabmenu_duplicate",
    "_on_tabmenu_rename",
    "_rename_tab_page",
    "_on_tabmenu_reconnect",
    "_on_tabmenu_manage_files",
    "_on_tabmenu_reveal_sidebar",
    "_on_tabmenu_open_system_terminal",
    "_on_tabmenu_new_local",
    "_on_tabmenu_close",
    "_on_tabmenu_close_others",
    "_on_tabmenu_close_right",
    "_apply_split_layout_to_target",
    "_on_tabmenu_layout_horizontal",
    "_on_tabmenu_layout_vertical",
    "_on_tabmenu_layout_default",
    "_on_tabmenu_layout_compact",
    "_on_tabmenu_fm_new_window",
    "move_file_manager_tab_to_window",
    "_attach_file_manager_window",
    "_close_file_manager_window",
    "install_file_manager_pane_shortcut",
    "_toggle_selected_file_manager_pane",
    "_file_manager_running_transfers",
    "_confirm_file_manager_tab_close",
    "_confirm_file_manager_window_close",
    "_launch_external_file_manager",
    "_on_tabmenu_rdp_detach",
    "_on_tabmenu_hide_left_pane",
    "_on_tabmenu_show_left_pane",
    "_set_tab_file_manager_single_pane",
    "focus_rdp_tab",
    "detach_rdp_tab",
    "_attach_rdp_window",
    "_close_rdp_window",
    "_bulk_close_target_pages",
    "_count_sessions_in_pages",
    "_run_suppressed_close",
    "_on_bulk_close_response",
    "_confirm_then_bulk_close",
    "on_tab_close",
    "_on_tab_close_response",
    "_on_split_tab_close_response",
    "on_tab_attached",
    "_register_convert_to_split_drop",
    "_update_layout_toggle_state",
    "_apply_tab_layout_mode",
    "_convert_terminal_tab_to_split",
    "_update_tab_button_visibility",
    "on_tab_detached",
    "on_open_split_view_clicked",
    "on_local_terminal_button_clicked",
    "on_tab_button_clicked",
)


def test_tab_methods_resolve_to_mixin_module():
    wm = _window_module()
    for name in _TAB_METHODS:
        method = getattr(wm.MainWindow, name)
        assert method.__module__ == "sshpilot.window_tabs", (
            f"{name} resolved to {method.__module__}, expected the mixin — a stray "
            "copy in window.py is shadowing it"
        )


def test_tabs_mixin_in_mro():
    wm = _window_module()
    mro_names = [c.__name__ for c in wm.MainWindow.__mro__]
    assert "WindowTabsMixin" in mro_names


def test_enabled_tab_actions_for_plugin_like_page():
    """Non-terminal tabs (Docker Console, WebTab, …) get rename/close actions.

    Returning an empty set used to hide the whole context menu because items
    use ``hidden-when=action-disabled``.
    """
    from sshpilot.window_tabs import WindowTabsMixin

    class Stub(WindowTabsMixin):
        def _file_manager_embed_for_child(self, _child):
            return None

    enabled = Stub()._enabled_tab_actions(object())
    assert enabled == {
        "tabmenu-rename",
        "tabmenu-close",
        "tabmenu-close-others",
        "tabmenu-close-right",
    }


class _FileManagerController:
    def __init__(self, single):
        self.single = single
        self._connection = None

    def is_single_pane(self):
        return self.single

    def set_single_pane(self, single):
        self.single = single


def _file_manager_tab_stub(controller):
    from sshpilot.window_tabs import WindowTabsMixin

    embed = types.SimpleNamespace(_controller=controller)

    class Stub(WindowTabsMixin):
        def _file_manager_embed_for_child(self, _child):
            return embed

        def _tab_menu_target(self):
            return object(), object()

    return Stub()


@pytest.mark.parametrize(
    "single, offered, hidden",
    [
        (False, "tabmenu-hide-left-pane", "tabmenu-show-left-pane"),
        (True, "tabmenu-show-left-pane", "tabmenu-hide-left-pane"),
    ],
)
def test_file_manager_tab_menu_offers_the_left_pane_switch(single, offered, hidden):
    """#1356: the left pane can be hidden from the tab's context menu too."""
    from sshpilot.window_tabs import WindowTabsMixin

    stub = _file_manager_tab_stub(_FileManagerController(single))
    enabled = stub._enabled_tab_actions(object())
    assert offered in enabled
    assert hidden not in enabled
    assert offered in WindowTabsMixin._TAB_MENU_ACTIONS


def test_left_pane_menu_items_switch_the_tabs_file_manager():
    controller = _FileManagerController(False)
    stub = _file_manager_tab_stub(controller)
    stub._on_tabmenu_hide_left_pane(None)
    assert controller.single is True
    stub._on_tabmenu_show_left_pane(None)
    assert controller.single is False


def test_rdp_tab_context_menu_offers_move_to_new_window():
    """An RDP tab can be detached into its own window (rdp_window.RdpWindow)."""
    rdp_tab = pytest.importorskip("sshpilot.rdp_tab")
    from sshpilot.window_tabs import WindowTabsMixin

    tab = rdp_tab.RdpTab.__new__(rdp_tab.RdpTab)  # type check only, no widget
    enabled = WindowTabsMixin()._enabled_tab_actions(tab)
    assert "tabmenu-rdp-detach" in enabled
    assert "tabmenu-rdp-detach" in WindowTabsMixin._TAB_MENU_ACTIONS


def test_terminal_tab_context_menu_exposes_split_layout_actions(monkeypatch):
    from sshpilot import window_tabs

    class Terminal:
        def _is_local_terminal(self):
            return True

    monkeypatch.setattr(window_tabs, 'TerminalWidget', Terminal)
    enabled = window_tabs.WindowTabsMixin()._enabled_tab_actions(Terminal())

    assert 'tabmenu-layout-horizontal' in enabled
    assert 'tabmenu-layout-vertical' in enabled


def test_split_layout_context_action_converts_right_clicked_terminal(monkeypatch):
    from sshpilot import window_tabs

    class Terminal:
        pass

    page = object()
    terminal = Terminal()

    class Stub(window_tabs.WindowTabsMixin):
        def _tab_menu_target(self):
            return page, terminal

        def _convert_terminal_tab_to_split(self, target_page, child, mode):
            self.converted = (target_page, child, mode)

    monkeypatch.setattr(window_tabs, 'TerminalWidget', Terminal)
    stub = Stub()
    stub._apply_split_layout_to_target('vertical')

    assert stub.converted == (page, terminal, 'vertical')


def test_tab_overview_button_visibility_honors_headerbar_preference():
    from sshpilot import window_tabs

    button = types.SimpleNamespace(visible=None)
    button.set_visible = lambda visible: setattr(button, 'visible', visible)
    bar = types.SimpleNamespace(visible=None)
    bar.set_visible = lambda visible: setattr(bar, 'visible', visible)

    class Config:
        values = {'ui.headerbar_show_tab_button': True}

        def get_setting(self, key, default):
            return self.values.get(key, default)

    class Stub(window_tabs.WindowTabsMixin):
        config = Config()
        tab_button = button
        tab_bar = bar
        _user_tabs = True

        def has_user_tabs(self):
            return self._user_tabs

    stub = Stub()
    stub._update_tab_button_visibility()
    assert button.visible is True
    assert bar.visible is True

    stub.config.values['ui.headerbar_show_tab_button'] = False
    stub._update_tab_button_visibility()
    assert button.visible is False
    assert bar.visible is True

    stub._user_tabs = False
    stub.config.values['ui.headerbar_show_tab_button'] = True
    stub._update_tab_button_visibility()
    assert button.visible is False
    assert bar.visible is False

    # The Configuration view is shown in place of the tabs: they go from
    # the header bar too.
    stub._user_tabs = True
    stub._sidebar_view_stack = types.SimpleNamespace(
        get_visible_child_name=lambda: 'sections'
    )
    stub._update_tab_button_visibility()
    assert button.visible is False
    assert bar.visible is False


def test_terminal_theme_button_visibility_honors_headerbar_preference(monkeypatch):
    from sshpilot import window_tabs

    terminal = types.SimpleNamespace(get_visible=lambda: True)
    page = types.SimpleNamespace(get_child=lambda: terminal)
    button = types.SimpleNamespace(visible=None)
    button.set_visible = lambda visible: setattr(button, 'visible', visible)

    class Config:
        allowed = False

        def get_setting(self, key, default):
            assert key == 'ui.headerbar_show_terminal_theme'
            return self.allowed

    class Stub(window_tabs.WindowTabsMixin):
        config = Config()
        tab_view = types.SimpleNamespace(get_selected_page=lambda: page)
        _terminal_theme_menu_button = button

        def _is_start_tab_page(self, _page):
            return False

    monkeypatch.setattr(window_tabs, '_is_terminal_widget', lambda child: child is terminal)

    stub = Stub()
    stub._update_terminal_theme_button_visibility()
    assert button.visible is False

    stub.config.allowed = True
    stub._update_terminal_theme_button_visibility()
    assert button.visible is True


def test_command_snippets_button_visibility_honors_headerbar_preference(monkeypatch):
    from sshpilot import window_tabs

    terminal = types.SimpleNamespace(get_visible=lambda: True)
    page = types.SimpleNamespace(get_child=lambda: terminal)
    button = types.SimpleNamespace(visible=None)
    button.set_visible = lambda visible: setattr(button, 'visible', visible)

    class Config:
        values = {}

        def get_setting(self, key, default):
            return self.values.get(key, default)

    class Stub(window_tabs.WindowTabsMixin):
        config = Config()
        tab_view = types.SimpleNamespace(get_selected_page=lambda: page)
        _cmd_blocks_toggle_btn = button
        _update_terminal_theme_button_visibility = None

        def _is_start_tab_page(self, _page):
            return False

    monkeypatch.setattr(window_tabs, '_is_terminal_widget', lambda child: child is terminal)

    stub = Stub()
    # Hidden by default, even on a terminal tab.
    stub._update_command_snippets_button_visibility()
    assert button.visible is False

    stub.config.values['ui.headerbar_show_commands'] = True
    stub._update_command_snippets_button_visibility()
    assert button.visible is True


class _FakeWidget:
    """Minimal stand-in for Gtk.Widget used by classify_tab_bar_hit."""

    def __init__(self, css_name='', css_classes=(), parent=None, page=None):
        self._css_name = css_name
        self._css_classes = set(css_classes)
        self._parent = parent
        self._page = page

    def get_css_name(self):
        return self._css_name

    def has_css_class(self, name):
        return name in self._css_classes

    def get_parent(self):
        return self._parent

    def get_property(self, name):
        if name == 'page':
            return self._page
        raise AttributeError(name)


def test_find_tab_page_at_returns_page_when_click_on_tab():
    from sshpilot.window_tabs import find_tab_page_at

    page = object()
    tab_bar = types.SimpleNamespace()
    tab = _FakeWidget(css_name='tab', page=page)
    label = _FakeWidget(css_name='label', parent=tab)
    tab_bar.pick = lambda x, y, flags: label

    assert find_tab_page_at(tab_bar, 10, 5) is page


def test_find_tab_page_at_ignores_empty_bar_space():
    from sshpilot.window_tabs import find_tab_page_at

    tab_bar = types.SimpleNamespace()
    empty = _FakeWidget(css_name='tabbox', parent=tab_bar)
    tab_bar.pick = lambda x, y, flags: empty

    assert find_tab_page_at(tab_bar, 200, 5) is None


def test_find_tab_page_at_ignores_close_button():
    from sshpilot.window_tabs import find_tab_page_at

    page = object()
    tab_bar = types.SimpleNamespace()
    tab = _FakeWidget(css_name='tab', page=page, parent=tab_bar)
    close_btn = _FakeWidget(
        css_name='button', css_classes=('tab-close-button',), parent=tab
    )
    icon = _FakeWidget(css_name='image', parent=close_btn)
    tab_bar.pick = lambda x, y, flags: icon

    assert find_tab_page_at(tab_bar, 40, 5) is None


def test_find_tab_page_at_ignores_end_action_widget():
    from sshpilot.window_tabs import find_tab_page_at

    tab_bar = types.SimpleNamespace()
    end_action = _FakeWidget(
        css_name='widget', css_classes=('end-action',), parent=tab_bar
    )
    button = _FakeWidget(css_name='button', parent=end_action)
    tab_bar.pick = lambda x, y, flags: button

    assert find_tab_page_at(tab_bar, 500, 5) is None


def test_classify_tab_bar_hit_distinguishes_empty_close_and_action():
    from sshpilot.window_tabs import classify_tab_bar_hit

    page = object()
    tab_bar = types.SimpleNamespace()

    tab = _FakeWidget(css_name='tab', page=page, parent=tab_bar)
    tab_bar.pick = lambda x, y, flags: tab
    assert classify_tab_bar_hit(tab_bar, 1, 1) == ('tab', page)

    empty = _FakeWidget(css_name='tabbox', parent=tab_bar)
    tab_bar.pick = lambda x, y, flags: empty
    assert classify_tab_bar_hit(tab_bar, 1, 1) == ('empty',)

    close_btn = _FakeWidget(
        css_name='button', css_classes=('tab-close-button',), parent=tab
    )
    tab_bar.pick = lambda x, y, flags: close_btn
    assert classify_tab_bar_hit(tab_bar, 1, 1) == ('close',)

    end_action = _FakeWidget(
        css_name='widget', css_classes=('end-action',), parent=tab_bar
    )
    tab_bar.pick = lambda x, y, flags: end_action
    assert classify_tab_bar_hit(tab_bar, 1, 1) == ('action',)


def test_tab_bar_double_click_empty_opens_local_terminal():
    from sshpilot.window_tabs import WindowTabsMixin

    calls = []

    class Stub(WindowTabsMixin):
        def __init__(self):
            self.tab_bar = types.SimpleNamespace()
            empty = _FakeWidget(css_name='tabbox', parent=self.tab_bar)
            self.tab_bar.pick = lambda x, y, flags: empty
            self.terminal_manager = types.SimpleNamespace(
                show_local_terminal=lambda: calls.append('local')
            )

    Stub()._on_tab_bar_pressed(None, 2, 100, 5)
    assert calls == ['local']


def test_custom_titlebar_double_click_empty_does_not_open_local_terminal():
    from sshpilot.window_tabs import WindowTabsMixin

    calls = []

    class Stub(WindowTabsMixin):
        def __init__(self):
            self._tab_bar_in_custom_titlebar = True
            self.tab_bar = types.SimpleNamespace()
            empty = _FakeWidget(css_name='tabbox', parent=self.tab_bar)
            self.tab_bar.pick = lambda x, y, flags: empty
            self.terminal_manager = types.SimpleNamespace(
                show_local_terminal=lambda: calls.append('local')
            )

    Stub()._on_tab_bar_pressed(None, 2, 100, 5)
    assert calls == []


def test_tab_bar_double_click_close_button_does_not_open_local():
    from sshpilot.window_tabs import WindowTabsMixin

    calls = []

    class Stub(WindowTabsMixin):
        def __init__(self):
            self.tab_bar = types.SimpleNamespace()
            page = object()
            tab = _FakeWidget(css_name='tab', page=page, parent=self.tab_bar)
            close_btn = _FakeWidget(
                css_name='button', css_classes=('tab-close-button',), parent=tab
            )
            self.tab_bar.pick = lambda x, y, flags: close_btn
            self.terminal_manager = types.SimpleNamespace(
                show_local_terminal=lambda: calls.append('local')
            )

        def _is_start_tab_page(self, _page):
            return False

        def _show_tab_rename_popover(self, page, x, y):
            calls.append('rename')

    Stub()._on_tab_bar_pressed(None, 2, 40, 5)
    assert calls == []


class _FocusStub:
    """A widget that records grab_focus and knows its ancestor."""

    def __init__(self, calls, name, ancestor=None):
        self.calls, self.name, self.ancestor = calls, name, ancestor

    def is_ancestor(self, widget):
        return widget is self.ancestor

    def get_mapped(self):
        return True

    def grab_focus(self):
        self.calls.append(('grab', self.name))

    def child_focus(self, _direction):
        self.calls.append(('child_focus', self.name))


def _focus_window(calls, *, selected_before, selected_after, focus_before):
    from sshpilot.window_tabs import WindowTabsMixin

    class Stub(WindowTabsMixin):
        def __init__(self):
            self.tab_view = types.SimpleNamespace(get_selected_page=lambda: self._selected)
            self._selected = selected_before

        def get_focus(self):
            return focus_before

        def _is_start_tab_page(self, _page):
            return False

        def _focus_terminal_widget(self, terminal):
            calls.append(('terminal', terminal))

    stub = Stub()
    stub._on_tab_bar_capture_press(None, 1, 0, 0)
    stub._selected = selected_after
    return stub


def test_click_on_selected_tab_gives_focus_back_to_the_pane():
    calls = []
    page_child = object()
    page = types.SimpleNamespace(get_child=lambda: page_child)
    vte = _FocusStub(calls, 'vte', ancestor=page_child)
    stub = _focus_window(calls, selected_before=page, selected_after=page,
                         focus_before=vte)

    stub._restore_focus_after_tab_click(page)

    assert calls == [('grab', 'vte')]


def test_click_on_selected_tab_with_focus_elsewhere_focuses_its_content():
    calls = []
    child = _FocusStub(calls, 'pane')
    page = types.SimpleNamespace(get_child=lambda: child)
    sidebar = _FocusStub(calls, 'sidebar')
    stub = _focus_window(calls, selected_before=page, selected_after=page,
                         focus_before=sidebar)

    stub._restore_focus_after_tab_click(page)

    assert calls == [('child_focus', 'pane')]


def test_click_on_another_tab_is_left_to_on_tab_selected():
    calls = []
    old = types.SimpleNamespace(get_child=lambda: object())
    new = types.SimpleNamespace(get_child=lambda: object())
    stub = _focus_window(calls, selected_before=old, selected_after=new,
                         focus_before=None)

    stub._restore_focus_after_tab_click(new)

    assert calls == []


@pytest.mark.parametrize("path, quoted", [
    ("/srv/my files", "'/srv/my files'"),
    ("/srv/it's", "'/srv/it'\"'\"'s'"),
    ("~", "~"),
    ("~/a b", "~/'a b'"),
])
def test_open_in_terminal_quotes_the_remote_path(path, quoted):
    from sshpilot.file_manager_integration import quote_remote_path

    assert quote_remote_path(path) == quoted


class _Node:
    def __init__(self, parent=None):
        self._parent = parent

    def get_parent(self):
        return self._parent


class _Terminal(_Node):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fed = []

    def feed_child_data_when_shell_ready(self, data):
        self.fed.append(data)


def _open_in_terminal_fixture(monkeypatch, *, under_terminal=False, panels=None,
                              fm_config=None):
    from sshpilot import file_manager_integration, window_tabs

    class Embed(_Node):
        def __init__(self, parent):
            super().__init__(parent)
            self.panels = dict(panels or {})
            self._controller = types.SimpleNamespace(
                _connection="conn",
                _left_pane=types.SimpleNamespace(_current_path="/home/me/src"),
                _right_pane=types.SimpleNamespace(_current_path="/srv/app"),
            )

        def get_terminal_panel(self, side="remote"):
            return self.panels.get(side)

        def has_terminal_panel(self):
            return bool(self.panels)

        def set_terminal_panel(self, terminal, teardown=None, side="remote"):
            self.panels[side] = terminal

    monkeypatch.setattr(window_tabs, 'TerminalWidget', _Terminal)
    monkeypatch.setattr(file_manager_integration, 'FileManagerTabEmbed', Embed)
    host = _Terminal() if under_terminal else None
    embed = Embed(_Node(host))
    content = _Node(_Node(embed))
    created = []

    def _create(conn):
        created.append(("remote", conn))
        return _Terminal()

    def _create_local(cwd=None):
        created.append(("local", cwd))
        return _Terminal()

    win = window_tabs.WindowTabsMixin()
    win.terminal_manager = types.SimpleNamespace(
        create_terminal_for_pane=_create,
        create_local_terminal_for_pane=_create_local,
    )
    config = dict(fm_config or {})
    win.config = types.SimpleNamespace(get_file_manager_config=lambda: config)
    win._tab_menu_target = lambda: (None, embed)
    win._file_manager_embed_for_child = lambda child: child
    return win, content, embed, host, created


def test_open_in_terminal_shows_the_terminal_pane(monkeypatch):
    win, content, embed, _host, created = _open_in_terminal_fixture(monkeypatch)
    assert win.open_file_manager_terminal_at(content, "/srv/my dir")
    assert created == [("remote", "conn")]
    assert embed.panels["remote"].fed == [b"cd '/srv/my dir'\n"]


def test_open_in_terminal_reuses_the_open_terminal_pane(monkeypatch):
    panel = _Terminal()
    win, content, _embed, _host, created = _open_in_terminal_fixture(
        monkeypatch, panels={"remote": panel},
    )
    assert win.open_file_manager_terminal_at(content, "/srv")
    assert created == []
    assert panel.fed == [b"cd /srv\n"]


def test_open_in_terminal_from_a_files_panel_uses_its_terminal(monkeypatch):
    win, content, embed, host, created = _open_in_terminal_fixture(
        monkeypatch, under_terminal=True,
    )
    assert win.open_file_manager_terminal_at(content, "/srv")
    assert created == [] and embed.panels == {}
    assert host.fed == [b"cd /srv\n"]


def test_open_in_terminal_starts_a_local_shell_in_the_folder(monkeypatch):
    win, content, embed, _host, created = _open_in_terminal_fixture(
        monkeypatch, panels={"remote": _Terminal()},
    )
    assert win.open_file_manager_terminal_at(content, "/home/me/src", "local")
    # A new shell starts there (spawn cwd), so nothing is typed into it.
    assert created == [("local", "/home/me/src")]
    assert embed.panels["local"].fed == []
    assert embed.panels["remote"].fed == []


def test_open_in_terminal_cds_a_running_local_shell(monkeypatch):
    local = _Terminal()
    win, content, _embed, _host, created = _open_in_terminal_fixture(
        monkeypatch, panels={"local": local},
    )
    assert win.open_file_manager_terminal_at(content, "/tmp", "local")
    assert created == [] and local.fed == [b"cd /tmp\n"]


@pytest.mark.parametrize("side", ["local", "remote"])
def test_open_in_terminal_never_types_control_characters(monkeypatch, side):
    panel = _Terminal()
    win, content, _embed, _host, _created = _open_in_terminal_fixture(
        monkeypatch, panels={side: panel},
    )
    # Typed, Ctrl+U erases the opening quote and ``id>pwned #'`` runs.
    assert not win.open_file_manager_terminal_at(
        content, "/srv/reports\x15id>pwned #", side,
    )
    assert panel.fed == []


def test_open_in_terminal_starts_a_local_shell_in_any_folder(monkeypatch):
    win, content, embed, _host, created = _open_in_terminal_fixture(monkeypatch)
    path = "/home/me/reports\x15id>pwned #"
    assert win.open_file_manager_terminal_at(content, path, "local")
    assert created == [("local", path)] and embed.panels["local"].fed == []


@pytest.mark.parametrize("setting, sides", [
    ("remote", ["remote"]),
    ("local", ["local"]),
    ("both", ["local", "remote"]),
    ("none", ["remote"]),
])
def test_show_terminal_pane_opens_the_configured_terminals(monkeypatch, setting, sides):
    win, _content, embed, _host, created = _open_in_terminal_fixture(
        monkeypatch, fm_config={"terminal_panes": setting},
    )
    win._on_tabmenu_show_terminal_panel(None)
    assert sorted(embed.panels) == sides
    # Not following navigation: shells start at home, nothing is typed.
    assert all(cwd is None for kind, cwd in created if kind == "local")
    assert all(t.fed == [] for t in embed.panels.values())


def test_show_terminal_pane_starts_in_the_pane_folders_when_following(monkeypatch):
    win, _content, embed, _host, created = _open_in_terminal_fixture(
        monkeypatch,
        fm_config={"terminal_panes": "both", "terminal_follows_navigation": True},
    )
    win._on_tabmenu_show_terminal_panel(None)
    assert ("local", "/home/me/src") in created
    assert embed.panels["remote"].fed == [b" cd /srv/app\n"]


def test_navigation_sync_is_off_by_default(monkeypatch):
    remote = _Terminal()
    win, content, _embed, _host, _created = _open_in_terminal_fixture(
        monkeypatch, panels={"remote": remote},
    )
    win.sync_file_manager_terminal(content, "/srv", "remote")
    assert remote.fed == []


def test_navigation_sync_cds_the_terminal_under_the_pane(monkeypatch):
    remote, local = _Terminal(), _Terminal()
    win, content, _embed, _host, created = _open_in_terminal_fixture(
        monkeypatch, panels={"remote": remote, "local": local},
        fm_config={"terminal_follows_navigation": True},
    )
    win.sync_file_manager_terminal(content, "/srv/a b", "remote")
    win.sync_file_manager_terminal(content, "/tmp", "local")
    assert remote.fed == [b" cd '/srv/a b'\n"]
    assert local.fed == [b" cd /tmp\n"]
    assert created == []


def test_navigation_sync_never_types_control_characters(monkeypatch):
    remote, local = _Terminal(), _Terminal()
    win, content, _embed, _host, _created = _open_in_terminal_fixture(
        monkeypatch, panels={"remote": remote, "local": local},
        fm_config={"terminal_follows_navigation": True},
    )
    win.sync_file_manager_terminal(content, "/srv/a\x03id #", "remote")
    win.sync_file_manager_terminal(content, "/tmp/a\tb", "local")
    assert remote.fed == [] and local.fed == []


def test_navigation_sync_never_opens_a_terminal(monkeypatch):
    win, content, embed, _host, created = _open_in_terminal_fixture(
        monkeypatch, fm_config={"terminal_follows_navigation": True},
    )
    win.sync_file_manager_terminal(content, "/srv", "remote")
    assert created == [] and embed.panels == {}


def test_navigation_sync_moves_an_unspawned_local_shell(monkeypatch):
    local = _Terminal()
    local._local_shell_spawned = False
    win, content, _embed, _host, _created = _open_in_terminal_fixture(
        monkeypatch, panels={"local": local},
        fm_config={"terminal_follows_navigation": True},
    )
    win.sync_file_manager_terminal(content, "/tmp", "local")
    assert local.fed == [] and local._local_shell_cwd == "/tmp"


def test_terminal_button_opens_the_pane_terminal_in_its_folder(monkeypatch):
    win, content, embed, _host, created = _open_in_terminal_fixture(monkeypatch)
    assert win.toggle_file_manager_terminal(content, "remote", True, "/srv/app")
    assert win.toggle_file_manager_terminal(content, "local", True, "/home/me")
    assert created == [("remote", "conn"), ("local", "/home/me")]
    assert embed.panels["remote"].fed == [b" cd /srv/app\n"]
    assert embed.panels["local"].fed == []


def test_terminal_button_opens_at_home_when_the_folder_cannot_be_typed(monkeypatch):
    win, content, embed, _host, created = _open_in_terminal_fixture(monkeypatch)
    assert win.toggle_file_manager_terminal(content, "remote", True, "/srv/a\x15id #")
    assert created == [("remote", "conn")]
    assert embed.panels["remote"].fed == []


def test_terminal_button_closes_only_its_side(monkeypatch):
    win, content, embed, _host, _created = _open_in_terminal_fixture(
        monkeypatch, panels={"remote": _Terminal(), "local": _Terminal()},
    )
    cleared = []
    embed.clear_terminal_panel = lambda side=None: cleared.append(side)
    assert win.toggle_file_manager_terminal(content, "local", False, "/home/me")
    assert cleared == ["local"]


def test_terminal_button_under_a_terminal_opens_a_pane_not_the_host(monkeypatch):
    win, content, embed, host, created = _open_in_terminal_fixture(
        monkeypatch, under_terminal=True,
    )
    assert win.toggle_file_manager_terminal(content, "remote", True, "/srv")
    assert created == [("remote", "conn")] and host.fed == []


@pytest.mark.parametrize("setting, sides", [
    ("none", []),
    ("remote", ["remote"]),
    ("local", ["local"]),
    ("both", ["local", "remote"]),
])
def test_new_file_manager_tab_opens_the_configured_terminals(monkeypatch, setting, sides):
    win, _content, embed, _host, _created = _open_in_terminal_fixture(
        monkeypatch, fm_config={"terminal_panes": setting},
    )
    win.open_configured_file_manager_terminals(embed)
    assert sorted(embed.panels) == sides


def test_pane_menu_runs_tab_handler_on_the_pane_terminal():
    """A split pane's header menu reuses the tab handlers on its terminal."""
    from sshpilot.window_tabs import WindowTabsMixin

    seen = []

    class Stub(WindowTabsMixin):
        _tab_menu_page = object()  # a stale right-clicked tab must not win

        def _on_tabmenu_reconnect(self, action, param=None):
            seen.append(self._tab_menu_target())

    stub = Stub()
    terminal = object()
    stub.run_tab_menu_action_for(terminal, 'tabmenu-reconnect')

    assert seen == [(None, terminal)]
    # The override ends with the action; the tab menu targets tabs again.
    assert stub._tab_menu_target()[0] is Stub._tab_menu_page


def _detach_stub(monkeypatch):
    """A tab mixin with fake tab views and a fake detached window."""
    from sshpilot import detached_tab_window
    from sshpilot.window_tabs import WindowTabsMixin

    events = []

    class TabView:
        def __init__(self, name):
            self.name, self.pages = name, []

        def transfer_page(self, page, other, position):
            events.append(("transfer", self.name, other.name, stub.__dict__.get("_transferring_tab_out")))
            self.pages.remove(page)
            other.pages.insert(position, page)

        def get_selected_page(self):
            return self.pages[0] if self.pages else None

        def get_n_pages(self):
            return len(self.pages)

        def set_selected_page(self, page):
            events.append(("select", self.name))

    class Window:
        def __init__(self, main, title, *, on_move_back, on_close, confirm_close=None):
            self.title, self.on_move_back, self.on_close = title, on_move_back, on_close
            self.confirm_close = confirm_close
            self.tab_view = TabView("detached")

        def set_default_size(self, w, h):
            self.size = (w, h)

        def add_controller(self, controller):
            events.append(("controller", controller))

        def present(self):
            events.append(("present", self.title))

    monkeypatch.setattr(detached_tab_window, "DetachedTabWindow", Window)

    class Stub(WindowTabsMixin):
        def __init__(self):
            self.tab_view = TabView("main")

        def connect(self, *_a):
            return 1

        def present(self):
            events.append(("present", "main"))

        def show_tab_view(self):
            pass

        def _launch_external_file_manager(self, _connection):  # pragma: no cover
            raise AssertionError("must not open a second connection")

    stub = Stub()
    stub.install_file_manager_pane_shortcut = (
        lambda window, view: window.add_controller(("F3", view))
    )
    return stub, events


def test_open_in_new_window_moves_the_tab_page_and_back(monkeypatch):
    """The page moves between tab views, as a detached RDP tab does: the
    file manager, its connections and terminal panels carry on."""
    stub, events = _detach_stub(monkeypatch)
    embed = types.SimpleNamespace(get_width=lambda: 900, get_height=lambda: 600)
    page = types.SimpleNamespace(get_title=lambda: "oracle Files", get_child=lambda: embed)
    stub.tab_view.pages.append(page)

    assert stub.move_file_manager_tab_to_window(page, embed) is True
    window = stub._file_manager_windows[0]
    # Moved, not closed: on_tab_detached sees the flag and leaves it alive.
    assert events[0] == ("transfer", "main", "detached", True)
    # F3 follows the file manager into its window.
    assert ("controller", ("F3", window.tab_view)) in events
    assert stub._transferring_tab_out is False
    assert window.tab_view.pages == [page] and window.title == "oracle Files"
    assert window.size == (900, 647)

    window.on_move_back(window)
    assert stub.tab_view.pages == [page]
    assert stub._file_manager_windows == []
    assert ("select", "main") in events


def test_closing_the_detached_window_ends_the_file_manager(monkeypatch):
    stub, _events = _detach_stub(monkeypatch)
    torn = []
    embed = types.SimpleNamespace(
        get_width=lambda: 0, get_height=lambda: 0,
        clear_terminal_panel=lambda: torn.append("terminals"),
    )
    page = types.SimpleNamespace(get_title=lambda: "", get_child=lambda: embed)
    stub.tab_view.pages.append(page)
    stub._file_manager_embeds_for_child = lambda child: [child] if child is embed else []
    stub._teardown_file_manager_embed = lambda e: torn.append(("teardown", e))

    stub.move_file_manager_tab_to_window(page, embed)
    window = stub._file_manager_windows[0]
    assert window.title  # untitled tabs still get a window title
    window.on_close(window)

    assert torn == ["terminals", ("teardown", embed)]
    assert stub._file_manager_windows == []


def test_tab_detach_keeps_a_transferred_file_manager_alive(monkeypatch):
    """on_tab_detached tears down file managers of closed tabs, not of a tab
    moving into its own window."""
    from sshpilot.window_tabs import WindowTabsMixin

    torn = []

    class Stub(WindowTabsMixin):
        _transferring_tab_out = True

        def _is_start_tab_page(self, _page):
            return False

        def _update_command_snippets_button_visibility(self):
            pass

        def _update_tab_button_visibility(self):
            pass

        def has_user_tabs(self):
            return True

        def _teardown_file_manager_embed(self, embed):  # pragma: no cover
            torn.append(embed)

    Stub().on_tab_detached(None, types.SimpleNamespace(get_child=lambda: object()), 0)
    assert torn == []


class _Mapped:
    def __init__(self, mapped=True):
        self.mapped = mapped

    def get_mapped(self):
        return self.mapped


def _f3_stub(child, controller):
    from sshpilot.window_tabs import WindowTabsMixin

    class Stub(WindowTabsMixin):
        def _file_manager_embed_for_child(self, _child):
            return types.SimpleNamespace(_controller=controller)

    page = types.SimpleNamespace(get_child=lambda: child)
    view = types.SimpleNamespace(get_selected_page=lambda: page)
    return Stub(), view


def test_window_f3_toggles_the_selected_file_manager_tab():
    """F3 that no focused widget took toggles the selected file manager."""
    controller = _FileManagerController(False)
    stub, view = _f3_stub(_Mapped(), controller)
    assert stub._toggle_selected_file_manager_pane(view) is True
    assert controller.single is True
    assert stub._toggle_selected_file_manager_pane(view) is True
    assert controller.single is False


def test_window_f3_leaves_terminal_tabs_and_hidden_tabs_alone():
    """Not a file manager tab, or not on screen: F3 is not handled, so it
    carries on to whatever else wants it."""
    from sshpilot.terminal import TerminalWidget

    controller = _FileManagerController(False)
    terminal = TerminalWidget.__new__(TerminalWidget)
    for child in (terminal, _Mapped(mapped=False), None):
        stub, view = _f3_stub(child, controller)
        assert stub._toggle_selected_file_manager_pane(view) is False
    assert controller.single is False


def test_closing_a_file_manager_tab_with_running_transfers_asks_first(monkeypatch):
    """Closing cancels the transfers; ask, and finish the close per answer."""
    from sshpilot import file_manager_window as fmw
    from sshpilot.window_tabs import WindowTabsMixin

    asked = []
    monkeypatch.setattr(fmw, "_quit_confirmed", False)
    monkeypatch.setattr(
        fmw, "confirm_cancel_transfers",
        lambda parent, count, **kw: asked.append((count, kw)),
    )
    controller = types.SimpleNamespace(running_transfer_count=lambda: 2)
    embed = types.SimpleNamespace(_controller=controller)

    class Stub(WindowTabsMixin):
        def _file_manager_embeds_for_child(self, _child):
            return [embed]

    finished = []
    view = types.SimpleNamespace(close_page_finish=lambda page, ok: finished.append(ok))
    page = types.SimpleNamespace(get_child=lambda: object())
    stub = Stub()

    assert stub._confirm_file_manager_tab_close(view, page) is True
    count, kw = asked[0]
    assert count == 2
    kw["on_keep"]()
    kw["on_confirm"]()
    assert finished == [False, True]

    # Confirming ends the tab as the ordinary close does: its terminal is
    # disconnected before the page goes.
    disconnected = []
    terminal = types.SimpleNamespace(disconnect=lambda: disconnected.append(True))
    stub.terminal_to_connection = {}
    stub._close_tab_after_transfers_confirmed(
        view, types.SimpleNamespace(get_child=lambda: terminal)
    )
    assert not disconnected  # not a tracked terminal: nothing to end
    stub.terminal_to_connection = _IdentityMap(terminal)
    stub._close_tab_after_transfers_confirmed(
        view, types.SimpleNamespace(get_child=lambda: terminal)
    )
    assert disconnected == [True]
    assert finished == [False, True, True, True]

    controller.running_transfer_count = lambda: 0
    assert stub._confirm_file_manager_tab_close(view, page) is False
    monkeypatch.setattr(fmw, "_quit_confirmed", True)
    controller.running_transfer_count = lambda: 2
    assert stub._confirm_file_manager_tab_close(view, page) is False


def test_a_terminal_tab_counts_the_transfers_of_its_files_panel():
    """The files panel below a terminal is torn down with the tab, which
    cancels its transfers: the tab close must ask for it too."""
    from sshpilot.terminal import TerminalWidget
    from sshpilot.window_tabs import WindowTabsMixin

    controller = types.SimpleNamespace(running_transfer_count=lambda: 3)
    embed = types.SimpleNamespace(_controller=controller)

    class Stub(WindowTabsMixin):
        def _file_manager_embeds_for_child(self, _child):
            return [embed]

    terminal = TerminalWidget.__new__(TerminalWidget)
    assert Stub()._file_manager_running_transfers(terminal) == 3
    assert Stub()._file_manager_running_transfers(None) == 0


class _IdentityMap:
    """Stands in for terminal_to_connection with an unhashable test double."""

    def __init__(self, *members):
        self._members = members

    def __contains__(self, item):
        return any(item is member for member in self._members)


def test_reveal_sidebar_expands_memberships_and_clears_filters(monkeypatch):
    from unittest.mock import Mock
    from sshpilot import sidebar, sidebar_views
    from sshpilot.window_tabs import WindowTabsMixin
    from sshpilot import window_tabs
    monkeypatch.setattr(window_tabs.GLib, "idle_add", lambda fn: fn())

    class Group:
        def __init__(self, group_id):
            self.group_id = group_id
            self.group_info = {"expanded": False}
        def _toggle_expand(self):
            self.group_info["expanded"] = True

    class Section:
        _expanded = False
        def _toggle_expand(self):
            self._expanded = True

    monkeypatch.setattr(sidebar, "GroupRow", Group)
    monkeypatch.setattr(sidebar, "SectionHeaderRow", Section)
    monkeypatch.setattr(sidebar_views, "show_view", Mock())
    child, connection = object(), object()
    root, nested, unrelated, section = Group("root"), Group("nested"), Group("other"), Section()
    selected = Mock()
    window = types.SimpleNamespace(
        _tab_menu_target=lambda: (None, child),
        terminal_to_connection={child: connection},
        _search_popup=types.SimpleNamespace(visible=True, hide=Mock()),
        _tag_filter="production", search_entry=Mock(),
        lookup_action=lambda name: None,
        _apply_sidebar_visible=Mock(), rebuild_connection_list=Mock(),
        complete_pending_connection_rows=Mock(),
        group_manager=types.SimpleNamespace(
            get_connection_groups=lambda conn: ["nested"],
            groups={"nested": {"parent_id": "root"}, "root": {}},
        ),
        connection_list=[section, root, nested, unrelated],
        _rows_for_connection=lambda conn: [selected],
        _select_only_row=Mock(),
    )
    WindowTabsMixin._on_tabmenu_reveal_sidebar(window, None)
    assert window._tag_filter is None
    window.search_entry.set_text.assert_called_once_with("")
    window._search_popup.hide.assert_called_once_with(animate=False)
    assert root.group_info["expanded"] and nested.group_info["expanded"]
    assert not unrelated.group_info["expanded"]
    assert section._expanded
    window._select_only_row.assert_called_once_with(selected)
    selected.grab_focus.assert_called_once()
