"""Widget-level coverage for the resizable sidebar split.

Builds a real ``SidebarPaned`` (no application, no daemon) and drives the split
view API the window uses — hide/show, remembering a dragged width, resting
width — against real GTK rather than the geometry helper alone.

Runs only under the on-demand GUI harness (skipped headless / in CI).
"""
import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, _GLib = requires_gui()

pytestmark = pytest.mark.gui


def _paned(**kwargs):
    from sshpilot.sidebar_paned import SidebarPaned

    kwargs.setdefault('max_width', 400)
    paned = SidebarPaned(**kwargs)
    paned.set_sidebar(Gtk.Box())
    paned.set_content(Gtk.Box())
    return paned


def test_saved_width_is_restored_before_any_allocation():
    paned = _paned(user_width=330)
    assert paned.get_position() == 330
    assert paned.get_sidebar_width() == 330


def _pump(rounds=50):
    context = _GLib.MainContext.default()
    for _ in range(rounds):
        if not context.iteration(False):
            break


def _shown(paned, width=1000, height=600):
    """Put the paned on screen and pump the loop until it has an allocation."""
    window = Gtk.Window()
    window.set_default_size(width, height)
    window.set_child(paned)
    window.present()
    context = _GLib.MainContext.default()
    for _ in range(200):
        if paned.get_width() > 0:
            break
        context.iteration(False)
    assert paned.get_width() > 0, 'paned never got an allocation'
    return window


def test_divider_move_is_remembered_and_reported():
    seen = []
    paned = _paned(on_user_resize=seen.append)
    window = _shown(paned)
    try:
        paned.set_position(360)      # what a drag on the handle produces
        assert paned.user_width == 360
        paned._persist()             # the debounce timeout's payload
        assert seen == [360]
    finally:
        window.destroy()


def test_resting_width_matches_user_width():
    paned = _paned(user_width=360)
    assert paned.get_resting_sidebar_width() == 360


def test_show_sidebar_tracks_the_sidebar_child():
    paned = _paned()
    paned.set_show_sidebar(False)
    assert paned.get_show_sidebar() is False
    assert paned.get_start_child().get_visible() is False
    paned.set_show_sidebar(True)
    assert paned.get_show_sidebar() is True
    assert paned.get_start_child().get_visible() is True


def test_visibility_decided_before_the_sidebar_exists_still_applies():
    from sshpilot.sidebar_paned import SidebarPaned

    paned = SidebarPaned()           # hide-on-startup runs before setup_sidebar()
    paned.set_show_sidebar(False)
    sidebar = Gtk.Box()
    paned.set_sidebar(sidebar)
    assert paned.get_sidebar() is sidebar
    # The sidebar is carried by a clipping holder, which is what gets hidden.
    assert paned.get_show_sidebar() is False
    assert paned.get_start_child().get_visible() is False


def test_a_dragged_width_outlives_the_automatic_maximum():
    """Nothing caps the user's own width — there is no maximum-width setting."""
    paned = _paned(user_width=520)          # wider than the 400 automatic cap
    window = _shown(paned, width=1400)
    try:
        assert paned.get_position() == 520
    finally:
        window.destroy()


def test_a_drag_into_the_wall_stops_at_the_content_floor():
    paned = _paned()
    sidebar = Gtk.Box()
    sidebar.set_size_request(200, -1)
    paned.set_sidebar(sidebar)
    paned.set_content(Gtk.Box())
    window = _shown(paned)
    try:
        paned.set_position(120)          # under the 200px content minimum
        assert paned.get_position() == 200
    finally:
        window.destroy()


def test_live_drag_callback_fires_while_resizing():
    widths = []
    paned = _paned(on_drag=widths.append)
    window = _shown(paned)
    try:
        paned.set_position(300)
        assert widths and widths[-1] == 300
    finally:
        window.destroy()


def test_a_squeezed_sidebar_keeps_its_leading_edge():
    """When the split is too narrow for the sidebar's content minimum, the
    clip holder must keep the child's start edge (x=0), not the empty tail
    Gtk.Paned would leave behind on its own."""
    paned = _paned()
    wide = Gtk.Box()
    wide.set_size_request(200, -1)
    paned.set_sidebar(wide)
    # Window too narrow to give sidebar 200 + content 320.
    window = _shown(paned, width=400)
    try:
        _pump()
        found, bounds = wide.compute_bounds(paned)
        assert found and bounds.get_x() == 0
    finally:
        window.destroy()


def test_overlay_presentation_is_not_offered_by_the_paned():
    paned = _paned()
    paned.set_collapsed(True)
    assert paned.get_collapsed() is False
