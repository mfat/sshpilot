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


def _settle(done, timeout=2.0):
    """Run the loop until ``done()`` or ``timeout``: a resize is laid out on
    the next frame-clock tick, which an idle-only pump returns before."""
    import time

    context = _GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        context.iteration(False)


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


def test_a_sidebar_at_its_floor_follows_content_that_grows():
    """A row that gains a status lock raises the content floor with no window
    resize; the divider must follow it rather than clip the lock, and go back
    to the dragged width once the lock is gone."""
    paned = _paned()
    sidebar = Gtk.Box()
    sidebar.set_size_request(200, -1)
    paned.set_sidebar(sidebar)
    window = _shown(paned)
    try:
        paned.set_position(120)          # drag into the wall
        assert paned.get_position() == 200
        sidebar.set_size_request(240, -1)  # the lock appears
        _settle(lambda: paned.get_position() == 240)
        assert paned.get_position() == 240
        sidebar.set_size_request(200, -1)  # and goes away
        _settle(lambda: paned.get_position() == 200)
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


def test_the_clip_holder_keeps_the_sidebar_leading_edge():
    """Squeezed below the sidebar's minimum (for a frame, before the collapse
    lands), the holder keeps the child's start edge (x=0), not the empty tail
    Gtk.Paned would leave behind on its own."""
    from sshpilot.sidebar_paned import _ClipStart

    wide = Gtk.Box()
    wide.set_size_request(200, -1)
    holder = _ClipStart(wide)
    holder.set_size_request(120, -1)
    holder.set_halign(Gtk.Align.START)
    window = Gtk.Window()
    window.set_default_size(300, 200)
    window.set_child(holder)
    window.present()
    try:
        _settle(lambda: holder.get_width() > 0)
        _pump()
        found, bounds = wide.compute_bounds(holder)
        assert found and bounds.get_x() == 0
    finally:
        window.destroy()


def test_a_window_too_narrow_for_both_sides_collapses_the_sidebar():
    """When the sidebar could only stay by being clipped, it leaves the layout
    (GNOME's collapsed split view, #1423); the content keeps its width, and
    the user's own show/hide choice and dragged width survive it."""
    collapsed_calls = []
    paned = _paned(user_width=300, on_collapsed_changed=collapsed_calls.append)
    sidebar = Gtk.Box()
    sidebar.set_size_request(250, -1)
    paned.set_sidebar(sidebar)
    content = Gtk.Box()
    content.set_size_request(460, -1)
    paned.set_content(content)
    window = _shown(paned, width=600)
    try:
        _settle(lambda: paned.get_collapsed() and collapsed_calls)
        assert paned.get_collapsed()
        assert paned.get_start_child().get_visible() is False
        assert paned.get_show_sidebar() is True
        found, bounds = content.compute_bounds(paned)
        assert found
        assert bounds.get_x() == 0
        assert bounds.get_x() + bounds.get_width() <= paned.get_width()
        assert paned.user_width == 300
        assert collapsed_calls == [True]
    finally:
        window.destroy()


def test_a_collapsed_sidebar_docks_again_once_it_fits():
    collapsed_calls = []
    paned = _paned(on_collapsed_changed=collapsed_calls.append)
    sidebar = Gtk.Box()
    sidebar.set_size_request(250, -1)
    paned.set_sidebar(sidebar)
    window = _shown(paned, width=500)
    try:
        _settle(lambda: paned.get_collapsed())
        window.set_default_size(1000, 600)
        _settle(lambda: not paned.get_collapsed() and collapsed_calls[-1:] == [False])
        assert not paned.get_collapsed()
        assert paned.get_start_child().get_visible() is True
        assert collapsed_calls == [True, False]
    finally:
        window.destroy()


def test_a_sidebar_hidden_by_the_user_stays_hidden_across_a_collapse():
    paned = _paned()
    sidebar = Gtk.Box()
    sidebar.set_size_request(250, -1)
    paned.set_sidebar(sidebar)
    paned.set_show_sidebar(False)
    window = _shown(paned, width=500)
    try:
        _settle(lambda: paned.get_collapsed())
        window.set_default_size(1000, 600)
        _settle(lambda: not paned.get_collapsed())
        assert paned.get_show_sidebar() is False
        assert paned.get_start_child().get_visible() is False
    finally:
        window.destroy()


def test_collapse_cannot_be_forced():
    """Only the width collapses the paned; the overlay is the window's."""
    paned = _paned()
    paned.set_collapsed(True)
    assert paned.get_collapsed() is False
