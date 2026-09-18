"""Local shell must not spawn until the terminal has real geometry.

Spawning while still unmapped / 0x0 (first Start-page drop after a cold
start) lets the shell write its prompt into a dead PTY — the user only sees
a blinking cursor until Enter redraws the prompt.
"""

import types

import sshpilot.terminal as terminal_mod
from sshpilot.terminal import TerminalWidget


def _fake_geometry_widget(*, mapped=False, width=0, height=0):
    return types.SimpleNamespace(
        get_mapped=lambda: mapped,
        get_width=lambda: width,
        get_height=lambda: height,
        connect=lambda *_a, **_k: 99,
        disconnect=lambda *_a, **_k: None,
    )


def _bare_local_terminal(monkeypatch, widget):
    term = TerminalWidget.__new__(TerminalWidget)
    term.terminal_widget = widget
    term._is_quitting = False
    spawned = []
    monkeypatch.setattr(terminal_mod, "is_flatpak", lambda: False)
    monkeypatch.setattr(
        term, "_setup_local_shell_direct", lambda: spawned.append("direct")
    )
    return term, spawned


def test_try_spawn_waits_for_mapped_geometry(monkeypatch):
    term, spawned = _bare_local_terminal(
        monkeypatch, _fake_geometry_widget(mapped=False, width=0, height=0)
    )

    assert term._try_spawn_local_shell(force=False) is False
    assert spawned == []

    term.terminal_widget = _fake_geometry_widget(mapped=True, width=0, height=600)
    assert term._try_spawn_local_shell(force=False) is False
    assert spawned == []

    term.terminal_widget = _fake_geometry_widget(mapped=True, width=800, height=600)
    assert term._try_spawn_local_shell(force=False) is True
    assert spawned == ["direct"]
    # Idempotent — never double-spawn.
    assert term._try_spawn_local_shell(force=True) is True
    assert spawned == ["direct"]


def test_try_spawn_force_bypasses_geometry(monkeypatch):
    term, spawned = _bare_local_terminal(
        monkeypatch, _fake_geometry_widget(mapped=False, width=0, height=0)
    )

    assert term._try_spawn_local_shell(force=True) is True
    assert spawned == ["direct"]


def test_show_local_terminal_adds_tab_before_shell_setup(monkeypatch):
    """Cold-start prompt loss: spawn must not run while the tab is parentless."""
    from sshpilot import terminal_manager as module

    order = []

    class Page:
        def set_title(self, title):
            self.title = title

        def set_icon(self, _icon):
            pass

    class TabView:
        def __init__(self):
            self.page = Page()

        def append(self, _child):
            order.append("append")
            return self.page

        def set_selected_page(self, _page):
            pass

    class Terminal:
        def __init__(self, connection, *_args):
            self.connection = connection

        def setup_local_shell(self):
            order.append("setup_local_shell")

        def show(self):
            pass

        def show_terminal(self):
            pass

    window = types.SimpleNamespace(
        tab_view=TabView(),
        config=object(),
        connection_manager=object(),
        connection_to_terminals={},
        terminal_to_connection={},
        active_terminals={},
        show_tab_view=lambda: None,
    )
    monkeypatch.setattr(module, "TerminalWidget", Terminal)
    monkeypatch.setattr(module.GLib, "idle_add", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        "sshpilot.icon_utils.new_gicon_from_icon_name", lambda _name: object()
    )

    assert module.TerminalManager(window).show_local_terminal() is True
    assert order == ["append", "setup_local_shell"]
