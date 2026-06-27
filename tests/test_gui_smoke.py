"""Smoke test: the real app boots under the GUI harness and a window comes up."""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def test_app_boots_and_window_present(gui):
    assert gui.window is not None
    # The pinned Start tab means at least one page exists on a fresh window.
    assert gui.window.tab_view.get_n_pages() >= 1
    # No stray confirmation dialogs on a clean boot.
    assert gui.message_dialogs() == []


def test_open_local_tabs(gui):
    gui.open_local_tabs(2)
    assert len(gui.user_pages()) == 2
