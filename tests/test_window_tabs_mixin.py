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
    "_on_tabmenu_duplicate",
    "_on_tabmenu_rename",
    "_rename_tab_page",
    "_on_tabmenu_reconnect",
    "_on_tabmenu_manage_files",
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
    "_launch_external_file_manager",
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
