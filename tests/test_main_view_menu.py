"""Functional commands exposed by the in-window View submenu."""

import types


class _Menu:
    def __init__(self):
        self.entries = []

    def append(self, label, action):
        self.entries.append(('item', label, action))

    def append_section(self, label, section):
        self.entries.append(('section', label, section))

    def append_submenu(self, label, submenu):
        self.entries.append(('submenu', label, submenu))


def test_view_submenu_exposes_titlebar_commands(monkeypatch):
    from sshpilot import window as window_module

    monkeypatch.setattr(window_module.Gio, 'Menu', _Menu)
    monkeypatch.setattr(window_module, 'should_hide_file_manager_options', lambda: True)

    theme_menu = _Menu()
    theme_menu.append('Follow System', 'win.set-app-theme::default')
    theme_menu.append('Light', 'win.set-app-theme::light')
    theme_menu.append('Dark', 'win.set-app-theme::dark')
    stub = types.SimpleNamespace(
        _plugins_menu_section=None,
        _create_theme_menu=lambda: theme_menu,
    )

    menu = window_module.MainWindow.create_menu(stub)
    submenus = [
        child
        for kind, _label, section in menu.entries
        if kind == 'section'
        for child in section.entries
        if child[0] == 'submenu'
    ]
    view_menu = next(submenu for _kind, label, submenu in submenus if label == 'View')

    assert view_menu.entries == [
        ('item', 'Toggle Sidebar', 'win.toggle_sidebar'),
        ('item', 'Commands', 'win.toggle-command-blocks'),
        ('submenu', 'Theme', theme_menu),
    ]

    root_items = [
        entry
        for kind, _label, section in menu.entries
        if kind == 'section'
        for entry in section.entries
        if entry[0] == 'item'
    ]
    assert ('item', 'New Group', 'win.create-group') in root_items
    assert ('item', 'New Local Terminal', 'app.local-terminal') in root_items
    assert ('item', 'New Split View', 'win.new-split-view') in root_items
