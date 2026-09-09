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


def test_clip_reveal_shows_all_packable_items_and_hides_overflow():
    """During width animation the toolbar reveals by clipping, not overflow."""
    toolbar = OverflowToolbar.__new__(OverflowToolbar)
    toolbar._spacing = 6
    toolbar._primary_count = 1
    toolbar._applying = False
    toolbar._clip_reveal = True
    toolbar._last_visible = 1
    toolbar._last_overflow = True
    toolbar._last_available = 90
    toolbar._last_overflowed_ids = (1, 2)
    toolbar._overflow_btn = MagicMock()
    toolbar._items = []
    toolbar._preferred_width = MagicMock(return_value=200)

    buttons = []
    for i in range(4):
        btn = MagicMock()
        btn._overflow_force_hidden = (i == 3)  # last one force-hidden
        buttons.append(btn)
        toolbar._items.append(btn)

    OverflowToolbar._apply_clip_reveal(toolbar)

    assert buttons[0].set_visible.call_args.args == (True,)
    assert buttons[1].set_visible.call_args.args == (True,)
    assert buttons[2].set_visible.call_args.args == (True,)
    assert buttons[3].set_visible.call_args.args == (False,)
    toolbar._overflow_btn.set_visible.assert_called_with(False)
    assert toolbar._last_visible == -1
    assert toolbar._last_available == -1


def test_clip_reveal_with_a_target_freezes_the_destination_split(monkeypatch):
    """Given the width the animation ends at, the frozen row is the split that
    width settles on — so the reveal does not show buttons that hop into the
    "…" menu on the last frame."""
    toolbar = OverflowToolbar.__new__(OverflowToolbar)
    toolbar._applying = False
    toolbar._clip_reveal = True
    toolbar._clip_reveal_target = 261
    toolbar._items = [MagicMock()]
    toolbar._overflow_btn = MagicMock()
    applied = []
    monkeypatch.setattr(toolbar, '_apply_overflow', applied.append)

    OverflowToolbar._apply_clip_reveal(toolbar)

    assert applied == [261]
    # No blanket "show everything": the destination decides what is visible.
    toolbar._items[0].set_visible.assert_not_called()
    toolbar._overflow_btn.set_visible.assert_not_called()


def test_set_clip_reveal_target_change_relayouts(monkeypatch):
    toolbar = OverflowToolbar.__new__(OverflowToolbar)
    toolbar._clip_reveal = False
    toolbar._clip_reveal_target = None
    calls = []
    monkeypatch.setattr(
        toolbar, 'force_relayout', lambda: calls.append('relayout'))

    OverflowToolbar.set_clip_reveal(toolbar, True, target_width=300)
    assert toolbar._clip_reveal_target == 300
    OverflowToolbar.set_clip_reveal(toolbar, True, target_width=300)
    assert calls == ['relayout']          # same target: no second relayout
    OverflowToolbar.set_clip_reveal(toolbar, False)
    assert toolbar._clip_reveal_target is None
    assert calls == ['relayout', 'relayout']


def test_set_clip_reveal_is_idempotent(monkeypatch):
    toolbar = OverflowToolbar.__new__(OverflowToolbar)
    toolbar._clip_reveal = False
    calls = []
    monkeypatch.setattr(
        toolbar, 'force_relayout', lambda: calls.append('relayout'))

    OverflowToolbar.set_clip_reveal(toolbar, True)
    assert toolbar._clip_reveal is True
    assert calls == ['relayout']

    OverflowToolbar.set_clip_reveal(toolbar, True)
    assert calls == ['relayout']

    OverflowToolbar.set_clip_reveal(toolbar, False)
    assert toolbar._clip_reveal is False
    assert calls == ['relayout', 'relayout']
