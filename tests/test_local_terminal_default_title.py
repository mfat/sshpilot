"""Local terminal tabs use a neutral default title."""

from types import SimpleNamespace


def test_local_terminal_uses_terminal_as_default_title(monkeypatch):
    from sshpilot import terminal_manager as module

    class Page:
        def set_title(self, title):
            self.title = title

        def set_icon(self, _icon):
            pass

    class TabView:
        def __init__(self):
            self.page = Page()

        def append(self, _child):
            return self.page

        def set_selected_page(self, _page):
            pass

    class Terminal:
        def __init__(self, connection, *_args):
            self.connection = connection

        def setup_local_shell(self):
            pass

        def show(self):
            pass

        def show_terminal(self):
            pass

    tab_view = TabView()
    window = SimpleNamespace(
        tab_view=tab_view,
        config=object(),
        connection_manager=object(),
        connection_to_terminals={},
        terminal_to_connection={},
        active_terminals={},
        show_tab_view=lambda: None,
    )
    monkeypatch.setattr(module, 'TerminalWidget', Terminal)
    monkeypatch.setattr(module.GLib, 'idle_add', lambda *_args: 0)
    monkeypatch.setattr(
        'sshpilot.icon_utils.new_gicon_from_icon_name', lambda _name: object()
    )

    assert module.TerminalManager(window).show_local_terminal() is True
    assert tab_view.page.title == 'Terminal'
    terminal = next(iter(window.terminal_to_connection))
    assert terminal.connection.nickname == 'Terminal'


def test_broadcast_discovery_uses_local_terminal_predicate():
    from sshpilot.terminal_manager import TerminalManager

    terminal = SimpleNamespace(
        backend=object(),
        connection=SimpleNamespace(nickname='Custom title', hostname='localhost'),
        _is_local_terminal=lambda: True,
    )

    assert TerminalManager._is_broadcastable_ssh_terminal(None, terminal) is False
