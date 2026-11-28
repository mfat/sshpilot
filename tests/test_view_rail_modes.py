"""Settings ▸ Sidebar ▸ View Rail: when the rail is on screen, and where
Advanced Configuration opens when it is not. Stand-in widgets, no GTK."""
from __future__ import annotations

import types

import pytest

from sshpilot import sidebar_views
from sshpilot.sidebar_views import (
    CONNECTIONS,
    RAIL_ALWAYS,
    RAIL_NEVER,
    RAIL_WITH_SIDEBAR,
    SECTIONS,
)


class _Widget:
    def __init__(self, visible=True):
        self.visible = visible

    def get_visible(self):
        return self.visible

    def set_visible(self, visible):
        self.visible = bool(visible)


class _Stack:
    def __init__(self):
        self.name = CONNECTIONS

    def get_visible_child_name(self):
        return self.name


class _Config:
    def __init__(self, mode):
        self.values = {"ui.view_rail": mode}

    def get_setting(self, key, default=None):
        return self.values.get(key, default)


def _window(mode, *, sidebar=True, collapsed=False):
    win = types.SimpleNamespace()
    win.config = _Config(mode)
    win.view_rail = _Widget()
    win._sidebar_view_stack = _Stack()
    win._sidebar_visible = sidebar
    win._collapsed = collapsed
    win._sidebar_hidden_by_toggle = lambda: not win._sidebar_visible or win._collapsed
    win.fullscreen_controller = types.SimpleNamespace(terminal_presentation_active=False)
    return win


def test_always_keeps_the_rail_with_the_sidebar_hidden():
    win = _window(RAIL_ALWAYS, sidebar=False)
    sidebar_views.sync_rail_visibility(win)
    assert win.view_rail.visible
    assert not sidebar_views.opens_in_window(win)


@pytest.mark.parametrize("sidebar, collapsed, shown", [
    (True, False, True),
    (False, False, False),      # hidden by F9 / the header button
    (True, True, False),        # the window is too narrow to dock it
])
def test_with_the_sidebar_follows_the_docked_sidebar(sidebar, collapsed, shown):
    win = _window(RAIL_WITH_SIDEBAR, sidebar=sidebar, collapsed=collapsed)
    sidebar_views.sync_rail_visibility(win)
    assert win.view_rail.visible is shown
    assert sidebar_views.opens_in_window(win) is (not shown)


def test_with_the_sidebar_the_configuration_view_keeps_its_rail():
    """The rail is the way back to the connections from that view."""
    win = _window(RAIL_WITH_SIDEBAR, sidebar=False)
    win._sidebar_view_stack.name = SECTIONS
    sidebar_views.sync_rail_visibility(win)
    assert win.view_rail.visible


def test_never_hides_it_and_opens_the_window():
    win = _window(RAIL_NEVER)
    sidebar_views.sync_rail_visibility(win)
    assert not win.view_rail.visible
    assert sidebar_views.opens_in_window(win)


def test_never_leaves_the_configuration_view(monkeypatch):
    win = _window(RAIL_NEVER)
    win._sidebar_view_stack.name = SECTIONS
    shown = []
    monkeypatch.setattr(sidebar_views, "show_view", lambda w, name: shown.append(name))
    sidebar_views.on_rail_setting_changed(win)
    assert shown == [CONNECTIONS]


def test_terminal_fullscreen_owns_the_rail():
    win = _window(RAIL_ALWAYS)
    win.view_rail.visible = False
    win.fullscreen_controller.terminal_presentation_active = True
    sidebar_views.sync_rail_visibility(win)
    assert not win.view_rail.visible


def test_an_unknown_mode_is_always():
    win = _window("sometimes", sidebar=False)
    assert sidebar_views.rail_mode(win) == RAIL_ALWAYS
    sidebar_views.sync_rail_visibility(win)
    assert win.view_rail.visible


def test_the_default_is_always():
    from sshpilot.config import Config

    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults["ui"]["view_rail"] == RAIL_ALWAYS


@pytest.mark.parametrize("rail_shown, expected", [(True, "view"), (False, "window")])
def test_advanced_configuration_opens_where_the_rail_allows(monkeypatch, rail_shown, expected):
    from sshpilot.window import MainWindow

    win = _window(RAIL_WITH_SIDEBAR)
    win.view_rail.visible = rail_shown
    monkeypatch.setattr(sidebar_views, "open_sections", lambda *_a, **_k: "view")
    win._open_advanced_config = lambda: types.SimpleNamespace(
        add_saved_listener=lambda _cb: None, go_to=lambda **_k: None,
        start_block=lambda *_a: None, kind="window",
    )
    result = MainWindow.open_advanced_config(win)
    assert (result if result == "view" else result.kind) == expected
