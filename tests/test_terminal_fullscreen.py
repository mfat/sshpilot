"""Fullscreen behavior that used to live on TerminalWidget.

``FullscreenController`` (``sshpilot/terminal_fullscreen.py``) owned the
toggle, the banner and the saved chrome state, and ``TerminalWidget`` owned the
controller. That ownership is the #1102 defect, so the module is gone: the
window owns ``WindowFullscreenController`` and the terminal only forwards.

The characterization coverage from that module is kept here against the new
owner, minus the banner — that is gone too, replaced by the window-owned
top-edge controls (see tests/test_window_fullscreen.py) — plus the delegation
contract for the terminal-facing wrapper.
"""
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from sshpilot.window_fullscreen import WindowFullscreenController


def _controller(**attrs):
    fc = WindowFullscreenController.__new__(WindowFullscreenController)
    fc.window = SimpleNamespace()
    fc.active = False
    fc._saved = None
    for key, value in attrs.items():
        setattr(fc, key, value)
    return fc


def test_toggle_enters_when_not_fullscreen():
    fc = _controller(active=False)
    calls = []
    fc.enter = lambda: calls.append("enter")
    fc.exit = lambda: calls.append("exit")
    fc.toggle()
    assert calls == ["enter"]


def test_toggle_exits_when_fullscreen():
    fc = _controller(active=True)
    calls = []
    fc.enter = lambda: calls.append("enter")
    fc.exit = lambda: calls.append("exit")
    fc.toggle()
    assert calls == ["exit"]


def test_no_banner_or_bespoke_overlay_machinery_survived():
    """Neither the banner nor the bespoke top-controls revealer came back.

    The exit affordance is the window's own header bar, revealed by the
    ToolbarView along with the tab bar as one surface.
    """
    for name in (
        "_ensure_banner",
        "_attach_banner",
        "_detach_banner",
        "_show_banner",
        "_hide_banner",
        "dismiss_banner",
        "banner_visible",
        "_ensure_top_controls",
        "_destroy_top_controls",
        "top_controls_revealed",
    ):
        assert not hasattr(WindowFullscreenController, name), name


def test_terminal_module_no_longer_defines_a_fullscreen_controller():
    from sshpilot import terminal as terminal_module

    assert not hasattr(terminal_module, "FullscreenController")

    with pytest.raises(ImportError):
        import sshpilot.terminal_fullscreen  # noqa: F401


def test_terminal_toggle_forwards_to_the_window_controller():
    from sshpilot.terminal import TerminalWidget

    toggled = []
    window = SimpleNamespace(
        fullscreen_controller=SimpleNamespace(toggle=lambda: toggled.append(True))
    )
    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.get_root = lambda: window

    terminal.toggle_fullscreen()
    assert toggled == [True]


def test_terminal_toggle_is_a_noop_without_a_window():
    from sshpilot.terminal import TerminalWidget

    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.get_root = lambda: None

    assert terminal.toggle_fullscreen() is None
