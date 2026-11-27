"""GUI test: a split-view tab must not force the main window wide (#1423).

Every tab page counts toward the window's minimum width, even in the
background. The labelled bottom bar (~800 px) and the unwrapped hint under the
panes (~620 px) used to set that minimum, so a tiling WM column narrower than
that clipped the whole window.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui

# Two side-by-side placeholder panes need ~380 px; leave room for fonts.
MAX_TAB_MIN_WIDTH = 480


def _min_width(widget):
    from gi.repository import Gtk
    return widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0]


@pytest.mark.parametrize('layout', ['vertical', 'horizontal'])
def test_split_view_tab_minimum_width_stays_small(gui, layout):
    win = gui.window
    svt = win._create_split_view_tab()
    try:
        svt.set_layout_mode(layout)
        gui.pump(1800)  # let the bottom bar reveal
        assert svt._add_pane_strip.get_revealed()
        assert _min_width(svt) <= MAX_TAB_MIN_WIDTH
        assert _min_width(svt._add_strip) <= svt.STRIP_MIN_WIDTH
    finally:
        win.tab_view.close_page(svt._tab_page)
        gui.pump(200)
