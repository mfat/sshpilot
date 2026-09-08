"""Geometry of the resizable sidebar split (``sshpilot.sidebar_paned``).

The sidebar used to live in an ``AdwOverlaySplitView``, which derived its width
from a fraction of the window clamped to a min/max and could not be resized. It
is now a ``Gtk.Paned``: the same levers still bound the width, but the user's
own divider drags win. ``resolve_position`` is the pure function behind that,
kept module level so this runs without building a GTK widget.
"""

import importlib


def _mod():
    return importlib.import_module('sshpilot.sidebar_paned')


def _pos(width, *, min_width=180, max_width=400, fraction=0.25, user_width=None):
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


def test_automatic_width_never_falls_below_the_minimum():
    # 0.25 * 600 = 150, below the 180 floor.
    assert _pos(600) == 180


def test_dragged_width_wins_over_the_fraction():
    assert _pos(1200, user_width=520) == 520


def test_dragged_width_may_exceed_the_automatic_cap():
    """The cap bounds the width the sidebar picks itself, not the user's choice."""
    assert _pos(1600, max_width=400, user_width=700) == 700


def test_dragged_width_stops_before_squeezing_out_the_content():
    # 1000 - 320 of content = 680 is as far right as the divider goes.
    assert _pos(1000, user_width=900) == 680


def test_dragged_width_is_held_above_the_minimum():
    assert _pos(1200, user_width=50) == 180


def test_pinned_levers_give_exactly_that_width():
    """min >= max is how the minimal icon strip freezes the width at 64px."""
    assert _pos(1200, min_width=64, max_width=64, user_width=520) == 64


def test_before_the_first_allocation_the_maximum_stands_in():
    assert _pos(0) == 400
    assert _pos(0, user_width=320) == 320


def test_drag_ceiling_leaves_room_for_the_content():
    mod = _mod()
    assert mod.drag_ceiling(1200, 400) == 1200 - 320
    # Nothing allocated yet: the configured maximum is all there is to go on.
    assert mod.drag_ceiling(0, 400) == 400
