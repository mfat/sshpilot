"""The sidebar's Connections / Rules dropdown, with real widgets.

Built directly, without the application: a plain window stands in for the
main window and a fake client hands out the configuration text.
"""
from __future__ import annotations

import types

import pytest

from tests.test_gui_auth_section_widgets import gtk  # noqa: F401 - fixture
from tests.test_gui_login_profile_dialogs import _pump

pytestmark = pytest.mark.gui

ROOT = """\
ServerAliveInterval 30

Host viewhost
    HostName 192.0.2.77

Host *.prod
    User ops
    IdentityFile ~/.ssh/prod

Match host *.corp
    ProxyJump gate

Host *
    ForwardAgent no
"""
INCLUDED = "Host *.lab\n    Port 2200\n"


class _Client:
    def __init__(self):
        self.texts = {"": ROOT, "inc-1": INCLUDED}
        self.fail = False

    def get_ssh_config_text(self, file=""):
        if self.fail:
            raise RuntimeError("no daemon")
        refs = (
            types.SimpleNamespace(file="", display_name="~/.ssh/config"),
            types.SimpleNamespace(file="inc-1", display_name="conf.d/lab"),
        )
        return types.SimpleNamespace(text=self.texts[file], files=refs)


@pytest.fixture
def window(gtk):  # noqa: F811
    from sshpilot.sidebar_views import build_sidebar_switcher, build_sidebar_view_stack

    win = gtk.ApplicationWindow()       # has actions, as the main window
    win.client = _Client()
    win.opened = []
    win.open_advanced_config = lambda **kw: win.opened.append(kw) or None
    box = gtk.Box(orientation=gtk.Orientation.VERTICAL)
    win.search_entry = gtk.SearchEntry()
    win.connection_scrolled = gtk.ScrolledWindow()
    win._sidebar_toolbar_clip = gtk.Box()
    build_sidebar_switcher(win, box)
    box.append(win.search_entry)
    build_sidebar_view_stack(win, box)
    box.append(win._sidebar_toolbar_clip)
    win.set_child(box)
    win.set_default_size(260, 500)
    win.present()
    _pump(seconds=0.2)
    yield win
    win.destroy()


def _titles(view):
    out, row = [], view.listbox.get_first_child()
    while row is not None:
        out.append(row.get_title())
        row = row.get_next_sibling()
    return out


def _click(view, index):
    # What a click or Enter on a row does.
    view.listbox.emit("row-activated", view.listbox.get_row_at_index(index))


ALL = ["Global Options", "*.prod", "Match host *.corp", "*", "*.lab"]


def test_the_switcher_shows_the_rules_and_brings_the_connections_back(window):
    stack, view = window._sidebar_view_stack, window._sidebar_rules_view
    assert stack.get_visible_child_name() == "connections"
    assert window._sidebar_view_switcher.get_selected() == 0
    assert window._sidebar_view_switcher.get_selected_item().get_string() == "Connections"

    window._sidebar_view_switcher.set_selected(1)
    assert stack.get_visible_child_name() == "rules"
    assert _pump(lambda: _titles(view) == ALL), _titles(view)
    assert view.get_visible_child_name() == "list"
    assert not window._sidebar_toolbar_clip.get_visible()    # connection actions
    assert window.search_entry.get_placeholder_text() == "Search rules"
    assert view.listbox.get_row_at_index(1).get_subtitle() == "User, IdentityFile"
    # A rule of an included file says which file.
    assert view.listbox.get_row_at_index(4).get_subtitle() == "conf.d/lab · Port"

    window._sidebar_view_switcher.set_selected(0)
    assert stack.get_visible_child_name() == "connections"
    assert window._sidebar_toolbar_clip.get_visible()
    assert window.search_entry.get_placeholder_text() == "Search connections"


def test_the_search_field_filters_the_rules(window):
    from sshpilot.sidebar_views import RULES, show_view

    view = window._sidebar_rules_view
    show_view(window, RULES)
    assert _pump(lambda: len(view.rules) == 5)
    window.search_entry.set_text("proxyjump")
    assert _pump(lambda: _titles(view) == ["Match host *.corp"]), _titles(view)
    window.search_entry.set_text("nothing-like-this")
    assert _pump(lambda: view.get_visible_child_name() == "empty")
    assert view.empty.get_title() == "No Results"
    window.search_entry.set_text("")
    assert _pump(lambda: _titles(view) == ALL)


def test_a_rule_opens_in_advanced_configuration_at_its_block(window):
    from sshpilot.sidebar_views import RULES, show_view

    view = window._sidebar_rules_view
    show_view(window, RULES)
    assert _pump(lambda: len(view.rules) == 5)
    lines = ROOT.splitlines()
    _click(view, 1)
    assert _pump(lambda: len(window.opened) == 1)
    assert lines[window.opened[0]["line"] - 1] == "Host *.prod"
    assert window.opened[0]["file"] == ""
    # The global options go to their first line, which their block owns.
    _click(view, 0)
    assert _pump(lambda: len(window.opened) == 2)
    assert lines[window.opened[1]["line"] - 1] == "ServerAliveInterval 30"
    # A rule of an included file is opened in that file.
    _click(view, 4)
    assert _pump(lambda: len(window.opened) == 3)
    assert window.opened[2] == {"line": 1, "file": "inc-1"}


def test_new_host_rule_begins_an_unsaved_block(window):
    window.lookup_action("new-host-rule").activate(None)
    assert window.opened == [{"new_block": ("", ())}]


def test_the_rules_are_read_again_when_asked_and_only_while_shown(window):
    from sshpilot.sidebar_views import CONNECTIONS, RULES, refresh_rules, show_view

    view = window._sidebar_rules_view
    show_view(window, RULES)
    assert _pump(lambda: len(view.rules) == 5)
    window.client.texts[""] = ROOT + "\nHost *.new\n    User fresh\n"
    refresh_rules(window)
    assert _pump(lambda: "*.new" in _titles(view)), _titles(view)

    show_view(window, CONNECTIONS)
    window.client.texts[""] = ROOT
    refresh_rules(window)                    # not shown: nothing is read
    _pump(seconds=0.3)
    assert len(view.rules) == 6
    show_view(window, RULES)                 # shown again: read again
    assert window._sidebar_view_switcher.get_selected() == 1     # the dropdown follows
    assert _pump(lambda: len(view.rules) == 5)


def test_no_rules_and_no_configuration_each_say_so(window):
    from sshpilot.sidebar_views import RULES, show_view

    view = window._sidebar_rules_view
    window.client.texts = {"": "Host a\n    HostName h\n", "inc-1": ""}
    show_view(window, RULES)
    assert _pump(lambda: view.empty.get_title() == "No Rules"), view.empty.get_title()
    assert view.get_visible_child_name() == "empty"
    window.client.fail = True
    view.refresh()
    assert _pump(lambda: view.empty.get_title() == "Rules Unavailable")
