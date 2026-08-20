"""GUI test for the macOS native menubar model.

Builds the menubar model against real GIO and asserts every ``app.*`` /
``win.*`` action it references is actually registered on the running
``SshPilotApplication`` / ``MainWindow`` — the property that keeps the native
menubar from shipping dead items. Also asserts the window submenu carries the
``gtk-macos-special`` attribute GTK needs to inject native window management.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from sshpilot.macos_menubar import build_macos_menubar
from sshpilot.platform_utils import is_macos
from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _walk_menu(model, depth=0, max_depth=6):
    """Yield (label, detailed_action, attributes) for every item in the model."""
    for i in range(model.get_n_items()):
        item = model.get_item_link(i)
        if item is None:
            continue
        attrs = item.get_attribute_values()
        label = None
        action = None
        for j in range(attrs.get_n_items()):
            name = attrs.get_name(j)
            value = attrs.get_value(j)
            if name == "label":
                label = value.get_string()
            elif name == "action":
                action = value.get_string()
        yield label, action, {
            attrs.get_name(j): attrs.get_value(j)
            for j in range(attrs.get_n_items())
        }
        if item.get_link_value("submenu") is not None and depth < max_depth:
            yield from _walk_menu(item.get_link_value("submenu"), depth + 1, max_depth)


def _referenced_actions(gui):
    model = build_macos_menubar()
    app_actions = set()
    win_actions = set()
    for _label, action, _attrs in _walk_menu(model):
        if action is None:
            continue
        if action.startswith("app."):
            app_actions.add(action[len("app."):])
        elif action.startswith("win."):
            win_actions.add(action[len("win."):])
    return app_actions, win_actions


def test_menubar_actions_exist_on_app_and_window(gui):
    app_actions, win_actions = _referenced_actions(gui)
    assert app_actions, "menubar model references no app.* actions"
    assert win_actions, "menubar model references no win.* actions"

    missing_app = sorted(
        name for name in app_actions if gui.app.lookup_action(name) is None
    )
    missing_win = sorted(
        name for name in win_actions if gui.window.lookup_action(name) is None
    )
    assert not missing_app, f"unregistered app.* actions in menubar: {missing_app}"
    assert not missing_win, f"unregistered win.* actions in menubar: {missing_win}"


def test_menubar_window_submenu_has_macos_special_attribute(gui):
    model = build_macos_menubar()
    found = False
    for _label, _action, attrs in _walk_menu(model):
        for i in range(attrs.get_n_items()):
            name = attrs.get_name(i)
            value = attrs.get_value(i)
            if name == "gtk-macos-special" and value.get_string() == "window-submenu":
                found = True
    assert found, "Window submenu missing gtk-macos-special=window-submenu"


def test_menubar_installed_only_on_macos(gui):
    menubar = gui.app.get_menubar()
    if is_macos():
        assert menubar is not None, "menubar not installed on macOS"
    else:
        assert menubar is None, "menubar must not be installed off macOS"