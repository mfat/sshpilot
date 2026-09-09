"""Geometry of the resizable sidebar split (``sshpilot.sidebar_paned``).

The sidebar used to live in an ``AdwOverlaySplitView``, which derived its width
from a fraction of the window clamped to a min/max and could not be resized. It
is now a ``Gtk.Paned``: the user's own divider drags win, and the only floor is
what the sidebar's content measures (``min_width`` here — there is no configured
minimum). ``resolve_position`` is the pure function behind that, kept module
level so this runs without building a GTK widget.
"""

import importlib


def _mod():
    return importlib.import_module('sshpilot.sidebar_paned')


# Stands in for the width the sidebar's own content measures at; the widget
# passes the measurement here, never a constant.
_CONTENT_MIN = 180


def _pos(width, *, min_width=_CONTENT_MIN, max_width=400, fraction=0.25,
         user_width=None):
    return _mod().resolve_position(
        width,
        min_width=min_width,
        max_width=max_width,
        fraction=fraction,
        user_width=user_width,
    )


def test_automatic_width_is_the_fraction_of_the_window():
    assert _pos(1200) == 300


def test_automatic_width_stops_at_the_default_cap():
    """The sidebar only sizes itself this wide; nothing caps a dragged width."""
    # 0.25 * 2400 = 600, above the 400 cap.
    assert _pos(2400) == 400


def test_automatic_width_never_falls_below_the_content_minimum():
    # 0.25 * 600 = 150, narrower than the content can be laid out in.
    assert _pos(600) == 180


def test_dragged_width_wins_over_the_fraction():
    assert _pos(1200, user_width=520) == 520


def test_dragged_width_may_exceed_the_automatic_cap():
    """The cap bounds the width the sidebar picks itself, not the user's choice."""
    assert _pos(1600, max_width=400, user_width=700) == 700


def test_dragged_width_stops_before_squeezing_out_the_content():
    # 1000 - 320 of content = 680 is as far right as the divider goes.
    assert _pos(1000, user_width=900) == 680


def test_dragged_width_is_held_above_the_content_minimum():
    assert _pos(1200, user_width=50) == 180


def test_a_narrow_sidebar_may_be_dragged_down_to_what_it_measures():
    """Nothing but the content holds the floor, so content that fits in 90px
    lets the divider come to 90px."""
    assert _pos(1200, min_width=90, user_width=50) == 90


def test_content_wider_than_the_automatic_cap_gets_the_width_it_needs():
    """The cap bounds the width the sidebar picks for itself; it cannot push the
    sidebar below what its content measures."""
    assert _pos(1200, min_width=460, max_width=400) == 460


def test_before_the_first_allocation_the_maximum_stands_in():
    assert _pos(0) == 400
    assert _pos(0, user_width=320) == 320


def test_drag_ceiling_leaves_room_for_the_content():
    mod = _mod()
    assert mod.drag_ceiling(1200, 400) == 1200 - 320
    # Nothing allocated yet: the configured maximum is all there is to go on.
    assert mod.drag_ceiling(0, 400) == 400
