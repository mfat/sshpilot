"""The window's Connections / Config Sections dropdown, with real widgets.

Built directly, without the application: a plain window stands in for the
main window, laid out as it is (a sidebar box, and a content box holding the
tabs), with the client the Advanced Configuration tests use.
"""
from __future__ import annotations

import shutil
import types

import pytest

from tests.test_gui_advanced_config import _Client, _pump, gtk  # noqa: F401 - fixture

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the OpenSSH client"),
]

CONFIG = """\
ServerAliveInterval 30

Host web1
    HostName 192.0.2.77

Host *.prod
    User ops
    IdentityFile ~/.ssh/prod

Match host *.corp
    ProxyJump gate

Host *
    ForwardAgent no
"""


class _Capable(_Client):
    def get_capabilities(self):
        return types.SimpleNamespace(supports=lambda _capability: True)


@pytest.fixture
def window(gtk, tmp_path):  # noqa: F811
    from sshpilot.sidebar_views import build_sidebar_switcher, build_sidebar_view_stack

    path = tmp_path / "config"
    path.write_text(CONFIG)
    win = gtk.ApplicationWindow()       # has actions, as the main window
    win.config_path = path
    win.client = _Capable(path)
    win.opened = []
    win.toasts = []
    win.open_advanced_config = lambda **kw: win.opened.append(kw) or None
    win.show_toast = win.toasts.append

    sidebar = gtk.Box(orientation=gtk.Orientation.VERTICAL)
    win.search_button = gtk.Button()
    win.search_container = gtk.Box()
    win.search_entry = gtk.SearchEntry()
    win.search_container.append(win.search_entry)
    win.connection_scrolled = gtk.ScrolledWindow()
    win._sidebar_toolbar_clip = gtk.Box()
    sidebar.append(win.search_button)
    build_sidebar_switcher(win, sidebar)
    sidebar.append(win.search_container)
    build_sidebar_view_stack(win, sidebar)
    sidebar.append(win._sidebar_toolbar_clip)

    content = gtk.Box(orientation=gtk.Orientation.VERTICAL)
    win.tab_overview = gtk.Label(label="tabs")      # stands for the tabs
    win.tab_overview.set_vexpand(True)
    content.append(win.tab_overview)

    paned = gtk.Paned()
    paned.set_start_child(sidebar)
    paned.set_end_child(content)
    paned.set_position(260)
    win.set_child(paned)
    win.set_default_size(1400, 760)
    win.present()
    _pump(lambda: False, seconds=0.2)
    yield win
    view = getattr(win, "_config_sections_view", None)
    if view is not None:
        view._closing = True
    win.destroy()


def _sections(window, blocks=5):
    from sshpilot.sidebar_views import SECTIONS, show_view

    show_view(window, SECTIONS)
    view = window._config_sections_view
    assert view is not None
    assert _pump(lambda: len(view._block_rows) == blocks), len(view._block_rows)
    return view


def _rows(view):
    return [view._block_rows[block.uid] for block in view.editor.blocks]


def _selected_patterns(view):
    return getattr(view.editor.find_block(view.selected_uid), "patterns", None)


def test_the_dropdown_switches_the_whole_window(window):
    stack = window._sidebar_view_stack
    assert stack.get_visible_child_name() == "connections"
    assert window._sidebar_view_switcher.get_selected_item().get_string() == "Connections"
    assert window._config_sections_view is None          # made on first use

    window._sidebar_view_switcher.set_selected(1)
    view = window._config_sections_view
    assert stack.get_visible_child_name() == "sections"
    assert _pump(lambda: len(view._block_rows) == 5)
    # The sidebar lists the blocks, each said to be what it is.
    assert view.sidebar_column.get_parent() is window._sidebar_sections_holder
    assert [(r.title.get_label(), r.caption.get_label()) for r in _rows(view)] == [
        ("Options Above All Blocks", "All hosts"),
        ("web1", "Connection"),
        ("*.prod", "Host pattern"),
        ("Match host *.corp", "Match rule"),
        ("Host *", "All hosts"),
    ]
    # The content area shows the configuration in the tabs' place.
    assert view.get_visible() and not window.tab_overview.get_visible()
    assert view.get_parent() is window.tab_overview.get_parent()
    for widget in (window._sidebar_toolbar_clip, window.search_button, window.search_container):
        assert not widget.get_visible()                  # the connections' own

    window._sidebar_view_switcher.set_selected(0)
    assert stack.get_visible_child_name() == "connections"
    assert window.tab_overview.get_visible() and not view.get_visible()
    assert window._sidebar_toolbar_clip.get_visible() and window.search_button.get_visible()


def test_the_sections_are_to_read_and_edit_opens_the_window(window):
    view = _sections(window)
    assert view.read_only and view.embedded
    for button in (view.undo_button, view.redo_button, view.save_button):
        assert not button.get_visible()
    assert not view.sidebar_button.get_visible()
    card = next(c for c in view.rules.cards.values() if c.patterns is not None)
    assert not card.patterns.get_editable()
    assert not card.footer.get_visible()                 # Add Option
    assert all(not b.get_visible() for b in view.rules._add_buttons)
    assert not view.results.raw_view.get_editable()

    # A block picked in the sidebar is the one Edit opens the window at.
    prod = next(b for b in view.editor.blocks if b.patterns == "*.prod")
    view._block_list.select_row(view._block_rows[prod.uid])
    assert _pump(lambda: view.selected_uid == prod.uid)
    view.edit_button.emit("clicked")
    lines = CONFIG.splitlines()
    assert len(window.opened) == 1
    assert lines[window.opened[0]["line"] - 1] == "Host *.prod"
    assert window.opened[0]["file"] == ""
    assert window.config_path.read_text() == CONFIG      # nothing was written


def test_a_tab_selected_brings_the_connections_back(window):
    from sshpilot.sidebar_views import current_view, leave_sections

    view = _sections(window)
    leave_sections(window)                    # what selecting a tab calls
    assert current_view(window) == "connections"
    assert window._sidebar_view_switcher.get_selected() == 0
    assert window.tab_overview.get_visible() and not view.get_visible()
    leave_sections(window)                    # already there: nothing to do
    assert current_view(window) == "connections"


def test_the_sections_follow_the_file(window):
    from sshpilot.sidebar_views import CONNECTIONS, refresh_sections, show_view

    view = _sections(window)
    prod = next(b for b in view.editor.blocks if b.patterns == "*.prod")
    view._block_list.select_row(view._block_rows[prod.uid])
    assert _pump(lambda: view.selected_uid == prod.uid)

    window.config_path.write_text(CONFIG + "\nHost *.new\n    User fresh\n")
    refresh_sections(window)
    assert _pump(lambda: len(view._block_rows) == 6)
    # The block that was selected still is.
    assert _pump(lambda: _selected_patterns(view) == "*.prod"), _selected_patterns(view)

    show_view(window, CONNECTIONS)
    window.config_path.write_text(CONFIG)
    refresh_sections(window)                  # not shown: nothing is read
    _pump(lambda: False, seconds=0.4)
    assert len(view._block_rows) == 6
    _sections(window)                         # shown again: read again (5 blocks)


def test_new_host_rule_begins_a_block_in_the_window(window):
    window.lookup_action("new-host-rule").activate(None)
    assert window.opened == [{"new_block": ("", ())}]
    assert window._config_sections_view is None          # the view is not needed for it


def test_without_the_background_service_the_connections_stay(window):
    window.client = object()                  # no capabilities to ask
    window._sidebar_view_switcher.set_selected(1)
    assert window._sidebar_view_stack.get_visible_child_name() == "connections"
    assert window._sidebar_view_switcher.get_selected() == 0
    assert window.toasts and "background service" in window.toasts[0]
    assert window.tab_overview.get_visible()


def test_the_dropdown_is_as_tall_as_a_connection_row(window, gtk):  # noqa: F811
    from sshpilot.sidebar_views import sync_switcher_height

    rows = gtk.ListBox()
    for name in ("web1", "db1"):
        row = gtk.ListBoxRow()
        box = gtk.Box(orientation=gtk.Orientation.VERTICAL)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.append(gtk.Label(label=name))
        box.append(gtk.Label(label="user@192.0.2.1"))
        row.set_child(box)
        row.connection = object()
        rows.append(row)
    window.connection_scrolled.set_child(rows)
    window.connection_list = rows
    _pump(lambda: False, seconds=0.3)
    sync_switcher_height(window)
    _pump(lambda: False, seconds=0.3)
    switcher = window._sidebar_view_switcher
    assert switcher.has_css_class("sidebar-view-switcher")      # the accent fill
    assert switcher.get_height() == rows.get_first_child().get_height()
