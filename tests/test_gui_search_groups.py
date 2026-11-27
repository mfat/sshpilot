from time import monotonic
from types import SimpleNamespace

import pytest

from tests._gui_harness import requires_gui

requires_gui()
pytestmark = pytest.mark.gui


def test_group_context_markup_is_safe_and_keeps_multiple_memberships():
    from sshpilot.search_groups import make_group_context_label

    manager = SimpleNamespace(
        get_connection_groups=lambda conn: ["a", "b"],
        groups={
            "a": {"name": "<Production> & API", "color": "#ff0000"},
            "b": {"name": "Shared", "color": "invalid"},
        },
    )
    label = make_group_context_label(manager, object())
    assert label.get_text() == "● <Production> & API · Shared"
    assert 'foreground="#ff0000"' in label.get_label()
    assert label.get_tooltip_text() == "<Production> & API\nShared"


def test_omni_connection_row_displays_group_context():
    from sshpilot.omni_search import OmniResult, OmniSearchController

    manager = SimpleNamespace(
        get_connection_groups=lambda conn: ["prod"],
        groups={"prod": {"name": "Production", "color": "#00ff00"}},
    )
    controller = SimpleNamespace(window=SimpleNamespace(group_manager=manager))
    result = OmniResult("connection", "web", "user@host",
                        "network-server-symbolic", 100, object())
    row = OmniSearchController._make_row(controller, result)
    labels = row.get_child().get_last_child()
    assert labels.get_last_child().get_text() == "● Production"


def test_opened_search_match_stays_selected_when_filter_clears():
    from gi.repository import Gtk
    from sshpilot.sidebar import SectionHeaderRow
    from sshpilot.window import MainWindow

    sidebar = Gtk.ListBox()
    other = Gtk.ListBoxRow()
    section = SectionHeaderRow("ungrouped", "Ungrouped")
    match = Gtk.ListBoxRow()
    for row in (other, section, match):
        sidebar.append(row)
    section.add_child_row(match)
    section._toggle_expand()
    sidebar.select_row(other)
    assert not match.get_visible()

    window = SimpleNamespace(
        connection_list=sidebar,
        complete_pending_connection_rows=lambda: None,
        _rows_for_connection=lambda connection: [match],
        _select_only_row=sidebar.select_row,
        rebuild_connection_list=lambda: None,
    )
    MainWindow._select_opened_connection(window, object())
    MainWindow.on_search_changed(window, Gtk.SearchEntry())

    assert match.get_visible()
    assert match.is_selected()


@pytest.mark.parametrize("collapsed", [False, True])
def test_opened_offscreen_match_scrolls_into_view_without_taking_focus(gui, collapsed):
    from gi.repository import Gtk
    from sshpilot.window import MainWindow

    window = Gtk.Window(default_height=180, default_width=320)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    entry = Gtk.Entry()
    scroll = Gtk.ScrolledWindow(vexpand=True)
    sidebar = Gtk.ListBox()
    for index in range(30):
        sidebar.append(Gtk.Label(label=str(index), height_request=32))
    from sshpilot.sidebar import SectionHeaderRow

    section = SectionHeaderRow("ungrouped", "Ungrouped")
    sidebar.append(section)
    target = Gtk.ListBoxRow(child=Gtk.Label(label="Match", height_request=32))
    sidebar.append(target)
    section.add_child_row(target)
    if collapsed:
        section.set_expanded(False)
    scroll.set_child(sidebar)
    content.append(entry)
    content.append(scroll)
    window.set_child(content)
    try:
        window.present()
        entry.grab_focus()
        if not collapsed:
            sidebar.select_row(target)
        adjustment = scroll.get_vadjustment()
        deadline = monotonic() + 3
        while adjustment.get_page_size() == 0 or adjustment.get_upper() <= adjustment.get_page_size():
            assert monotonic() < deadline, "offscreen row never received its initial layout"
            gui.pump(20)
        assert adjustment.get_value() == 0
        focus = window.get_focus()

        reveal_window = SimpleNamespace(
            connection_list=sidebar,
            connection_scrolled=scroll,
            _attach_staged_connection_rows=lambda: None,
            _apply_sidebar_row_actions=lambda force: None,
            _select_only_row=sidebar.select_row,
            complete_pending_connection_rows=lambda: None,
            _rows_for_connection=lambda connection: [target],
        )
        MainWindow._finish_rebuild(reveal_window, 0)
        MainWindow._select_opened_connection(reveal_window, object())
        deadline = monotonic() + 3
        while adjustment.get_value() + adjustment.get_page_size() < adjustment.get_upper() - 1:
            if monotonic() >= deadline:
                break
            gui.pump(20)

        assert adjustment.get_value() > 0
        assert adjustment.get_value() + adjustment.get_page_size() >= adjustment.get_upper() - 1
        assert window.get_focus() is focus
    finally:
        window.destroy()
