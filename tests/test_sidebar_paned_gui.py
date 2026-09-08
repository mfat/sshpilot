"""Widget-level coverage for the resizable sidebar split.

Builds a real ``SidebarPaned`` (no application, no daemon) and drives the split
view API the window uses, so the paned's answers to those calls — pinning for
the minimal icon strip, hide/show, remembering a dragged width — are checked
against real GTK rather than the geometry helper alone.

Runs only under the on-demand GUI harness (skipped headless / in CI).
"""
import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, _GLib = requires_gui()

pytestmark = pytest.mark.gui


def _paned(**kwargs):
    from sshpilot.sidebar_paned import SidebarPaned

    kwargs.setdefault('min_width', 180)
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


def test_pinning_the_width_freezes_it_and_releasing_restores_it():
    paned = _paned(user_width=360)
    # The minimal icon strip pins the width; the sidebar's own minimum must not
    # hold the divider back at a pinned width.
    paned.pin_width(64)
    assert paned.get_position() == 64
    # Released: back to the width the user chose.
    paned.release_width()
    assert paned.get_position() == 360


def test_resting_width_survives_being_pinned():
    """The strip animation needs the released width while the width is pinned."""
    paned = _paned(user_width=360)
    paned.pin_width(64)
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
        paned.pin_width(64)
        paned.release_width()
        assert paned.get_position() == 520
    finally:
        window.destroy()


def test_a_squeezed_sidebar_keeps_its_leading_edge():
    """The 64px icon strip is narrower than the sidebar's own minimum, so the
    sidebar gets clipped — what must survive is its start (the icons), not the
    empty tail Gtk.Paned would leave behind on its own."""
    paned = _paned()
    wide = Gtk.Box()
    wide.set_size_request(200, -1)
    paned.set_sidebar(wide)
    window = _shown(paned)
    try:
        paned.pin_width(64)
        _pump()
        assert paned.get_position() == 64
        assert wide.get_width() == 200          # laid out at its own minimum
        found, bounds = wide.compute_bounds(paned)
        assert found and bounds.get_x() == 0    # anchored at the leading edge
    finally:
        window.destroy()


def test_overlay_presentation_is_not_offered_by_the_paned():
    paned = _paned()
    paned.set_collapsed(True)
    assert paned.get_collapsed() is False
