"""Characterization tests for the terminal fullscreen feature.

Pins the cleanly-testable behavior before extraction into FullscreenController:
the toggle dispatch and the banner show/hide/dismiss visibility. The heavy
enter/exit window juggling reaches into the toplevel window and is covered by
the real-GTK smoke + manual test instead; the focus-restoration collapse is
tested as a method once extracted (see test_restore_focus_* below).

Standard headless pattern: build a bare TerminalWidget via ``__new__`` and stub
only the attributes each method touches.
"""
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from sshpilot.terminal_fullscreen import FullscreenController


class _Container:
    def __init__(self, visible=False):
        self.visible = visible
        self._child = object()  # not an Adw.Banner → set_revealed path skipped

    def get_child(self):
        return self._child

    def set_visible(self, v):
        self.visible = v


def _fc(**attrs):
    fc = FullscreenController.__new__(FullscreenController)
    fc.t = SimpleNamespace()
    fc._is_fullscreen = False
    fc._fullscreen_banner_container = None
    for k, v in attrs.items():
        setattr(fc, k, v)
    return fc


def test_toggle_enters_when_not_fullscreen():
    fc = _fc(_is_fullscreen=False)
    calls = []
    fc._enter_fullscreen = lambda: calls.append("enter")
    fc._exit_fullscreen = lambda: calls.append("exit")
    fc.toggle_fullscreen()
    assert calls == ["enter"]


def test_toggle_exits_when_fullscreen():
    fc = _fc(_is_fullscreen=True)
    calls = []
    fc._enter_fullscreen = lambda: calls.append("enter")
    fc._exit_fullscreen = lambda: calls.append("exit")
    fc.toggle_fullscreen()
    assert calls == ["exit"]


def test_show_fullscreen_banner_makes_it_visible():
    fc = _fc(_fullscreen_banner_container=_Container(visible=False))
    fc._show_fullscreen_banner()
    assert fc._fullscreen_banner_container.visible is True


def test_hide_fullscreen_banner_makes_it_invisible():
    fc = _fc(_fullscreen_banner_container=_Container(visible=True))
    fc._hide_fullscreen_banner()
    assert fc._fullscreen_banner_container.visible is False


def test_dismiss_hides_banner():
    fc = _fc()
    hidden = []
    fc._hide_fullscreen_banner = lambda: hidden.append(True)
    fc._on_fullscreen_banner_dismiss(None)
    assert hidden == [True]


def test_restore_focus_prefers_backend_then_vte_then_self():
    # backend present → backend.grab_focus
    grabbed = []
    fc = _fc()
    fc.t = SimpleNamespace(backend=SimpleNamespace(grab_focus=lambda: grabbed.append("backend")),
                           vte=SimpleNamespace(grab_focus=lambda: grabbed.append("vte")),
                           grab_focus=lambda: grabbed.append("self"))
    assert fc._restore_focus() is False
    assert grabbed == ["backend"]

    # no backend → vte.grab_focus
    grabbed.clear()
    fc.t = SimpleNamespace(backend=None,
                           vte=SimpleNamespace(grab_focus=lambda: grabbed.append("vte")),
                           grab_focus=lambda: grabbed.append("self"))
    fc._restore_focus()
    assert grabbed == ["vte"]
