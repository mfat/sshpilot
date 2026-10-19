"""GUI test: "Add Terminal" scrolls a new off-screen pane into view.

Once the split grid fills the viewport, a new pane lands below the visible
area; without the reveal the click looked like it did nothing.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _pane_visible(svt, pane):
    ok, bounds = pane.compute_bounds(svt._content_area)
    assert ok
    adj = svt._pane_scroll.get_vadjustment()
    top = bounds.get_y()
    bottom = top + bounds.get_height()
    value = adj.get_value()
    return value - 1 <= top and bottom <= value + adj.get_page_size() + 1


@pytest.mark.parametrize('layout', ['vertical', 'horizontal'])
def test_add_terminal_reveals_new_pane(gui, layout):
    win = gui.window
    svt = win._create_split_view_tab()
    try:
        svt.set_layout_mode(layout)
        # The action bar slides in ~1 s after the tab maps and shrinks the
        # viewport; a user cannot click "Add Terminal" before that.
        gui.pump(1800)
        assert svt._add_pane_strip.get_revealed()
        adj = svt._pane_scroll.get_vadjustment()
        assert adj.get_value() == 0

        # Keep adding until a pane would have been created off-screen.
        for _ in range(4):
            svt._add_pane_btn.emit('clicked')
            gui.pump(400)
            assert _pane_visible(svt, svt._panes[-1])
        assert adj.get_value() > 0, 'grid never overflowed the viewport'
    finally:
        win.tab_view.close_page(svt._tab_page)
        gui.pump(200)
