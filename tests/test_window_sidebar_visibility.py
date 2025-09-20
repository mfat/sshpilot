import pytest

from sshpilot import window


class DummyControl:
    def __init__(self):
        self.visible = True

    def set_visible(self, value):
        self.visible = value

    def get_visible(self):
        return self.visible


class DummyOverlaySplitView:
    def __init__(self):
        self.calls = []

    def set_show_sidebar(self, visible):
        self.calls.append(visible)


class DummyNavigationSplitView:
    def __init__(self):
        self.calls = []

    def set_show_sidebar(self, visible):
        self.calls.append(visible)


def test_toggle_sidebar_visibility_overlay_keeps_header_controls(monkeypatch):
    win = window.MainWindow.__new__(window.MainWindow)
    win.split_view = DummyOverlaySplitView()
    win._split_variant = 'overlay'
    win.header_bar = DummyControl()
    win.sidebar_toggle_button = DummyControl()
    win.header_bar.visible = True
    win.sidebar_toggle_button.visible = True

    monkeypatch.setattr(window, 'HAS_OVERLAY_SPLIT', True)
    monkeypatch.setattr(window, 'HAS_NAV_SPLIT', False)

    win._toggle_sidebar_visibility(True)
    assert win.header_bar.visible is True
    assert win.sidebar_toggle_button.visible is True
    assert win.split_view.calls[-1] is True

    win._toggle_sidebar_visibility(False)
    assert win.header_bar.visible is True
    assert win.sidebar_toggle_button.visible is True
    assert win.split_view.calls[-1] is False


def test_toggle_sidebar_visibility_navigation_keeps_header_controls(monkeypatch):
    win = window.MainWindow.__new__(window.MainWindow)
    win.split_view = DummyNavigationSplitView()
    win._split_variant = 'navigation'
    win.header_bar = DummyControl()
    win.sidebar_toggle_button = DummyControl()

    monkeypatch.setattr(window, 'HAS_OVERLAY_SPLIT', False)
    monkeypatch.setattr(window, 'HAS_NAV_SPLIT', True)

    win._toggle_sidebar_visibility(True)
    assert win.header_bar.visible is True
    assert win.sidebar_toggle_button.visible is True
    assert win.split_view.calls == []

    win._toggle_sidebar_visibility(False)
    assert win.header_bar.visible is True
    assert win.sidebar_toggle_button.visible is True
    assert win.split_view.calls == []
