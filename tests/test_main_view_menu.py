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

    def append_item(self, item):
        self.entries.append(('item-object', item))


class _MenuItem:
    def __init__(self):
        self.attributes = {}

    def set_attribute_value(self, name, value):
        self.attributes[name] = value


class _StringVariant:
    def __init__(self, _type, value):
        self.value = value


def test_view_submenu_exposes_titlebar_commands(monkeypatch):
    from sshpilot import window as window_module

    monkeypatch.setattr(window_module.Gio, 'Menu', _Menu)
    monkeypatch.setattr(window_module.Gio, 'MenuItem', _MenuItem)
    monkeypatch.setattr(window_module.GLib, 'Variant', _StringVariant)
    monkeypatch.setattr(window_module, 'should_hide_file_manager_options', lambda: True)

    stub = types.SimpleNamespace(
        _plugins_menu_section=None,
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
        ('item', 'Toggle Full Screen', 'win.toggle-fullscreen'),
        ('item', 'Sidebar Toggle Button', 'win.headerbar-sidebar-toggle'),
        ('item', 'Split View Button', 'win.headerbar-split-view'),
        ('item', 'Command Snippets Button', 'win.headerbar-commands'),
        ('item', 'Terminal Theme Button', 'win.headerbar-terminal-theme'),
        ('item', 'Theme Menu', 'win.headerbar-theme-menu'),
        ('item', 'Local Terminal Button', 'win.headerbar-local-terminal'),
    ]

    first_section = menu.entries[0][2]
    custom_item = first_section.entries[0][1]
    assert custom_item.attributes['custom'].value == 'theme-selector'

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


def test_headerbar_menu_action_updates_shared_preference(monkeypatch):
    from sshpilot import actions

    class Variant:
        def __init__(self, value):
            self.value = value

        def get_boolean(self):
            return self.value

    class Action:
        def __init__(self, name, state):
            self.name = name
            self.state = state
            self.callback = None

        @classmethod
        def new_stateful(cls, name, _parameter_type, state):
            return cls(name, state)

        def connect(self, _signal, callback):
            self.callback = callback

        def get_name(self):
            return self.name

        def get_state(self):
            return self.state

        def set_state(self, state):
            self.state = state

        def change_state(self, state):
            self.callback(self, state)

    monkeypatch.setattr(actions.Gio, 'SimpleAction', Action)
    monkeypatch.setattr(
        actions.GLib,
        'Variant',
        types.SimpleNamespace(new_boolean=Variant),
    )

    class Config:
        def __init__(self):
            self.values = {}

        def get_setting(self, key, default=None):
            return self.values.get(key, default)

        def set_setting(self, key, value):
            self.values[key] = value

    class Window:
        def __init__(self):
            self.config = Config()
            self.actions = {}
            self.update_count = 0

        def add_action(self, action):
            self.actions[action.get_name()] = action

        def update_headerbar_buttons(self):
            self.update_count += 1

    window = Window()
    actions._register_headerbar_visibility_actions(window)
    action = window.actions['headerbar-sidebar-toggle']

    assert action.get_state().get_boolean() is False
    action.change_state(Variant(True))
    assert window.config.values['ui.headerbar_show_sidebar_toggle'] is True
    assert action.get_state().get_boolean() is True
    assert window.update_count == 1

    theme_action = window.actions['headerbar-terminal-theme']
    assert theme_action.get_state().get_boolean() is True
    theme_action.change_state(Variant(False))
    assert window.config.values['ui.headerbar_show_terminal_theme'] is False
    assert theme_action.get_state().get_boolean() is False
    assert window.update_count == 2
