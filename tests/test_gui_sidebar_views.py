"""The window's Connections / Configuration dropdown, with real widgets.

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
    win.search_container.set_visible(False)       # as in the app, until search is opened
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
        ("Global Options", "All hosts"),
        ("web1", "Host"),
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


def _edit_user(view, value):
    """Type a value into the User option of the *.prod block."""
    prod = next(b for b in view.editor.blocks if b.patterns == "*.prod")
    option = next(o for o in prod.options if o.key == "User")
    row = view.rules.cards[prod.uid].rows[option.uid]
    row.value.set_text(value)
    assert _pump(lambda: view.dirty)
    return prod


def _labels(window):
    model = window._sidebar_view_switcher.get_model()
    return [model.get_string(index) for index in range(model.get_n_items())]


def test_the_sections_are_edited_and_saved_there(window):
    view = _sections(window)
    assert view.embedded
    assert view.save_button.get_visible() and not view.save_button.get_sensitive()
    assert not view.sidebar_button.get_visible()         # the list is the main sidebar's
    assert _labels(window) == ["Connections", "Configuration"]

    _edit_user(view, "deploy")
    assert view.save_button.get_sensitive()
    assert _labels(window) == ["Connections", "Configuration •"]   # unsaved, said
    assert window._sidebar_view_switcher.get_selected() == 1          # still this view
    assert window.config_path.read_text() == CONFIG                   # not yet written

    view.save()
    assert _pump(lambda: "User deploy" in window.config_path.read_text(), seconds=15)
    assert _pump(lambda: not view.dirty)
    assert _labels(window) == ["Connections", "Configuration"]


def test_a_draft_is_kept_when_the_view_is_left(window):
    from sshpilot.sidebar_views import CONNECTIONS, leave_sections, refresh_sections, show_view

    view = _sections(window)
    _edit_user(view, "deploy")
    leave_sections(window)                    # a tab was selected
    assert window._sidebar_view_stack.get_visible_child_name() == "connections"
    assert _labels(window)[1] == "Configuration •"     # said from the other view too
    # Nothing reads the file over the draft: not a rebuild of the list...
    refresh_sections(window)
    # ...and not coming back.
    show_view(window, "sections")
    _pump(lambda: False, seconds=0.5)
    assert view.dirty and "User deploy" in view.editor.text
    show_view(window, CONNECTIONS)
    assert view.dirty


def test_openings_go_to_the_view_at_the_place_asked(window):
    from sshpilot.sidebar_views import open_sections

    view = open_sections(window, host="web1")
    assert view is window._config_sections_view
    assert window._sidebar_view_stack.get_visible_child_name() == "sections"
    assert _pump(lambda: _selected_patterns(view) == "web1"), _selected_patterns(view)

    # A new block is begun there, unsaved.
    open_sections(window, new_block=("*.lab", [("User", "lab")]))
    assert _pump(lambda: _selected_patterns(view) == "*.lab")
    assert view.dirty and window.config_path.read_text() == CONFIG


def test_quitting_asks_about_unsaved_changes(window, monkeypatch):
    from sshpilot.sidebar_views import CONNECTIONS, confirm_quit, show_view

    went = []
    assert confirm_quit(window, lambda: went.append(1)) is False     # no view yet
    view = _sections(window)
    assert confirm_quit(window, lambda: went.append(1)) is False     # nothing unsaved

    _edit_user(view, "deploy")
    show_view(window, CONNECTIONS)
    asked = []
    monkeypatch.setattr(
        view, "_confirm_discard", lambda proceed, stay=None: asked.append(proceed)
    )
    assert confirm_quit(window, lambda: went.append(1)) is True      # closing waits
    assert window._sidebar_view_stack.get_visible_child_name() == "sections"  # shown what
    assert asked and not went
    asked[0]()                                # Discard
    assert went == [1]
    # The close that Discard asks for is not questioned again...
    assert confirm_quit(window, lambda: went.append(2)) is False
    # ...but that close may still be called off (running sessions): the
    # window stays, the draft is as unsaved as before and is asked about
    # at the next quit, and the editor goes on working.
    assert view.dirty and not view._closing
    assert confirm_quit(window, lambda: went.append(3)) is True
    assert len(asked) == 2 and went == [1]
    view.save()                               # its background work still reports back
    assert _pump(lambda: "User deploy" in window.config_path.read_text(), seconds=15)
    assert _pump(lambda: not view.dirty)


def test_a_block_begun_while_the_file_is_read_again_is_kept(window):
    """Coming back to the view reads the file again, off the main loop. A
    new block asked for before that read is back was added to the old
    draft and then read away with it."""
    from sshpilot.sidebar_views import CONNECTIONS, open_sections, show_view

    view = _sections(window)
    show_view(window, CONNECTIONS)
    open_sections(window, new_block=("*.lab", [("User", "lab")]))     # shows it: reloads
    assert view._reloading                    # the read is not back yet
    assert _pump(lambda: not view._reloading and view._loaded)
    assert _pump(lambda: _selected_patterns(view) == "*.lab"), _selected_patterns(view)
    _pump(lambda: False, seconds=0.5)
    assert [b.patterns for b in view.editor.blocks][-1] == "*.lab"    # still there
    assert view.dirty and window.config_path.read_text() == CONFIG


def test_an_edit_made_while_the_file_is_read_again_is_kept(window):
    from sshpilot.sidebar_views import CONNECTIONS, show_view

    view = _sections(window)
    show_view(window, CONNECTIONS)
    window.config_path.write_text(CONFIG + "\nHost other\n    User x\n")
    show_view(window, "sections")
    assert view._reloading
    _edit_user(view, "typed-meanwhile")
    assert _pump(lambda: not view._reloading and view._loaded)
    _pump(lambda: False, seconds=0.5)
    assert "User typed-meanwhile" in view.editor.text and view.dirty


def test_a_search_of_the_connections_is_not_left_filtering_out_of_sight(window):
    from sshpilot.sidebar_views import CONNECTIONS, show_view

    window.search_container.set_visible(True)
    window.search_entry.set_text("web")
    _sections(window)
    assert not window.search_container.get_visible()
    show_view(window, CONNECTIONS)
    assert window.search_container.get_visible()         # the query is in sight again
    assert window.search_entry.get_text() == "web"

    window.search_entry.set_text("")
    window.search_container.set_visible(False)           # search closed
    _sections(window)
    show_view(window, CONNECTIONS)
    assert not window.search_container.get_visible()     # and stays closed


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


def test_new_host_rule_asks_for_a_new_block(window):
    window.lookup_action("new-host-rule").activate(None)
    assert window.opened == [{"new_block": ("", ())}]


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


def test_ctrl_f_is_the_search_of_the_view_on_screen(window):
    from sshpilot.sidebar_views import CONNECTIONS, focus_sections_search, show_view

    assert focus_sections_search(window) is False        # connections: their own search
    view = _sections(window)
    assert focus_sections_search(window) is True
    assert _pump(lambda: view.target_entry.has_focus() or view.target_entry.get_focus_child())
    show_view(window, CONNECTIONS)
    assert focus_sections_search(window) is False
