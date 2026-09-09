"""Unit tests for sidebar toolbar overflow packing."""

from unittest.mock import MagicMock

from sshpilot.overflow_toolbar import OverflowToolbar, choose_overflow


def test_everything_fits_without_overflow():
    assert choose_overflow([36, 36, 36], 120, spacing=6, overflow_width=36) == (
        3,
        False,
    )


def test_trailing_items_overflow_when_narrow():
    # Three 36px buttons + 2*6 spacing = 120; with overflow button need more.
    visible, show = choose_overflow(
        [36, 36, 36, 36], 90, spacing=6, overflow_width=36
    )
    assert show is True
    assert visible == 1  # 36 + 6 + 36 = 78 <= 90; two + overflow = 114 > 90


def test_zero_visible_still_shows_overflow_button():
    visible, show = choose_overflow(
        [40, 40, 40], 30, spacing=6, overflow_width=36
    )
    assert visible == 0
    assert show is True


def test_primary_pair_fits_with_overflow():
    # edit + delete (36 each) + overflow (36) + spacing = 36*3 + 12 = 120
    visible, show = choose_overflow(
        [36, 36, 36, 36, 36], 120, spacing=6, overflow_width=36
    )
    assert show is True
    assert visible == 2


def test_empty_toolbar():
    assert choose_overflow([], 100) == (0, False)


def test_item_width_reuses_cache_when_hidden_measure_collapses():
    """Hidden GTK widgets often measure as 0; packing must keep the cached size."""
    toolbar = OverflowToolbar.__new__(OverflowToolbar)
    widget = MagicMock()
    widget.measure.return_value = (0, 0, -1, -1)
    widget._overflow_nat_width = 36
    assert OverflowToolbar._item_width(toolbar, widget) == 36


def test_apply_overflow_keeps_icons_hidden_after_remeasure():
    """A re-allocate must not un-hide overflowed icons when measures collapse."""
    toolbar = OverflowToolbar.__new__(OverflowToolbar)
    toolbar._spacing = 6
    toolbar._primary_count = 1
    toolbar._applying = False
    toolbar._last_visible = -1
    toolbar._last_overflow = False
    toolbar._last_available = -1
    toolbar._last_overflowed_ids = ()
    toolbar._overflow_btn = MagicMock()
    toolbar._overflow_btn.get_visible.return_value = True
    toolbar._overflow_btn.measure.return_value = (36, 36, -1, -1)
    toolbar._items = []
    toolbar._rebuild_overflow_menu = MagicMock()

    buttons = []
    for _ in range(4):
        btn = MagicMock()
        btn.get_visible.return_value = True
        btn._overflow_force_hidden = False
        btn._overflow_nat_width = 36

        def _measure(_orientation, _for_size, b=btn):
            if b.get_visible():
                return (36, 36, -1, -1)
            return (0, 0, -1, -1)

        btn.measure.side_effect = _measure

        def _set_visible(show, b=btn):
            b.get_visible.return_value = bool(show)

        btn.set_visible.side_effect = _set_visible
        buttons.append(btn)
        toolbar._items.append(btn)

    # 90px: one 36px button + overflow (36) + spacing = 78.
    OverflowToolbar._apply_overflow(toolbar, 90)
    assert toolbar._last_visible == 1
    assert toolbar._last_overflow is True
    assert buttons[0].get_visible() is True
    assert all(not b.get_visible() for b in buttons[1:])

    # Remeasure as if the MenuButton click triggered allocate; hidden buttons
    # now measure 0 — packing must still keep them in the overflow menu.
    toolbar._last_available = -1  # force a fresh choose_overflow pass
    OverflowToolbar._apply_overflow(toolbar, 90)
    assert toolbar._last_visible == 1
    assert toolbar._last_overflow is True
    assert buttons[0].get_visible() is True
    assert all(not b.get_visible() for b in buttons[1:])
    toolbar._overflow_btn.set_visible.assert_called_with(True)
