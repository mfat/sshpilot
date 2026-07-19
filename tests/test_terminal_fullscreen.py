"""Characterization tests for the terminal fullscreen feature.

Pins the cleanly-testable behavior before extraction into FullscreenController:
the toggle dispatch and the banner show/hide/dismiss visibility. The heavy
enter/exit window juggling reaches into the toplevel window and is covered by
the real-GTK smoke + manual test instead; the focus-restoration collapse is
tested as a method once extracted (see test_restore_focus_* below).

Standard headless pattern: build a bare TerminalWidget via ``__new__`` and stub
only the attributes each method touches.
"""
import pytest

pytest.importorskip("gi")

from sshpilot.terminal import TerminalWidget


class _Container:
    def __init__(self, visible=False):
        self.visible = visible
        self._child = object()  # not an Adw.Banner → set_revealed path skipped

    def get_child(self):
        return self._child

    def set_visible(self, v):
        self.visible = v


def _tw(**attrs):
    tw = TerminalWidget.__new__(TerminalWidget)
    tw._is_fullscreen = False
    tw._fullscreen_banner_container = None
    for k, v in attrs.items():
        setattr(tw, k, v)
    return tw


def test_toggle_enters_when_not_fullscreen():
    tw = _tw(_is_fullscreen=False)
    calls = []
    tw._enter_fullscreen = lambda: calls.append("enter")
    tw._exit_fullscreen = lambda: calls.append("exit")
    tw.toggle_fullscreen()
    assert calls == ["enter"]


def test_toggle_exits_when_fullscreen():
    tw = _tw(_is_fullscreen=True)
    calls = []
    tw._enter_fullscreen = lambda: calls.append("enter")
    tw._exit_fullscreen = lambda: calls.append("exit")
    tw.toggle_fullscreen()
    assert calls == ["exit"]


def test_show_fullscreen_banner_makes_it_visible():
    tw = _tw(_fullscreen_banner_container=_Container(visible=False))
    tw._show_fullscreen_banner()
    assert tw._fullscreen_banner_container.visible is True


def test_hide_fullscreen_banner_makes_it_invisible():
    tw = _tw(_fullscreen_banner_container=_Container(visible=True))
    tw._hide_fullscreen_banner()
    assert tw._fullscreen_banner_container.visible is False


def test_dismiss_hides_banner():
    tw = _tw()
    hidden = []
    tw._hide_fullscreen_banner = lambda: hidden.append(True)
    tw._on_fullscreen_banner_dismiss(None)
    assert hidden == [True]
