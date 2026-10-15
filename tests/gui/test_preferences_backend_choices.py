"""Real-GTK check of the Terminal Backend picker's built-in emulator entries."""

import pytest

from tests._gui_harness import requires_gui

requires_gui()

pytestmark = pytest.mark.gui


def test_backend_picker_lists_pyte_and_libvterm(gui):
    from sshpilot.preferences import PreferencesWindow

    prefs = PreferencesWindow(gui.window, gui.window.config)
    prefs._ensure_page_built(prefs._page_id("Terminal"))
    try:
        model = prefs.backend_row.get_model()
        labels = [model.get_string(i) for i in range(model.get_n_items())]
        assert any(label.startswith('pyte') for label in labels), labels
        assert any(label.startswith('libvterm') for label in labels), labels
        print('Terminal Backend entries:', labels, 'selected:', labels[prefs.backend_row.get_selected()])
    finally:
        prefs.close()
