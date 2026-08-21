"""macOS temporarily adds a dedicated header bar in fullscreen."""

import types


def test_macos_fullscreen_header_is_inserted_above_tab_row_and_removed():
    from sshpilot.window import MainWindow

    class Toolbar:
        def __init__(self):
            self.top_bars = []

        def add_top_bar(self, widget):
            self.top_bars.append(widget)

        def remove(self, widget):
            self.top_bars.remove(widget)

    class Controls:
        def __init__(self):
            self.visible = True

        def set_visible(self, visible):
            self.visible = visible

    macos_header = object()
    tab_row = object()
    window = types.SimpleNamespace()
    window._macos_header_bar = macos_header
    window._macos_fullscreen_header_attached = False
    window.header_bar = tab_row
    toolbar = Toolbar()
    toolbar.top_bars = [tab_row]
    window._content_toolbar_view = toolbar
    window._window_controls_start = Controls()
    window._window_controls_end = Controls()

    MainWindow._set_macos_fullscreen_headerbar(window, True)

    assert toolbar.top_bars == [macos_header, tab_row]
    assert window._window_controls_start.visible is False
    assert window._window_controls_end.visible is False

    MainWindow._set_macos_fullscreen_headerbar(window, False)

    assert toolbar.top_bars == [tab_row]
    assert window._window_controls_start.visible is True
    assert window._window_controls_end.visible is True
