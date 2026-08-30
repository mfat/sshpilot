"""Copying while a remote app owns the mouse (issue #1178).

Reported as "all clipboard copy methods stop working in tmux". The 5.9.9
debug logs pin the trigger precisely: copy works until a remote application
turns on mouse reporting, then every attempt logs
``copied=False reason=empty-selection`` with *no* ``selection-changed`` event
in between, and recovers the moment tracking drops. The reporter's own bisect
found the application to be a full-screen CLI running inside tmux -- tmux
itself had ``mouse off``.

That much is protocol, not a defect: once DECSET 1000/1002/1003/1006 is set,
press/drag/release belong to the remote app, and Shift is the documented
escape hatch (VTE checks ``GDK_SHIFT_MASK``; xterm.js checks
``shouldForceSelection`` -> ``e.shiftKey``). What sshPilot adds on top are the
two defects pinned here:

* the Shift-drag escape hatch is destroyed by the double-Shift Omnisearch
  gesture, which no pointer activity can cancel, so a second Shift-drag within
  the double-tap window steals focus instead of selecting; and
* a copy that finds no selection reports nothing at all, which is what turned
  a discoverable protocol limit into "it silently stopped working".
"""

from types import SimpleNamespace

import pytest

from gi.repository import Gdk

from sshpilot.shortcut_utils import DOUBLE_SHIFT_SHORTCUT, DoubleShiftDetector
from sshpilot.terminal_input import MouseTrackingState

try:
    import sshpilot.window as window_module
    from sshpilot.window import MainWindow
except Exception:  # pragma: no cover - depends on GTK test stub state
    window_module = None
    MainWindow = None


# Emitted by the CLI the reporter bisected to, and echoed verbatim by the
# 5.9.9 log line ``mouse tracking changed active=True
# modes=[1000, 1002, 1003, 1006]``.
MOUSE_ON = b"\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h"
MOUSE_OFF = b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"


# ---------------------------------------------------------------------------
# The premise: what the reporter's logs actually recorded.
# ---------------------------------------------------------------------------


def test_remote_app_claims_the_pointer_and_gives_it_back():
    """The tracker reproduces both transitions seen in the 5.9.9 log."""
    tracker = MouseTrackingState()

    assert tracker.feed(b"$ claude\r\n") is False
    assert tracker.feed(MOUSE_ON) is True
    assert tracker.modes == (1000, 1002, 1003, 1006)

    # Copy failed for every attempt in this window; tracking outlives the
    # app's own redraws.
    assert tracker.feed(b"\x1b[2J\x1b[Hredraw") is True

    assert tracker.feed(MOUSE_OFF) is False


# ---------------------------------------------------------------------------
# Defect 1: the double-Shift Omnisearch gesture eats Shift-drag selection.
# ---------------------------------------------------------------------------


def _shift_drag(detector, press_at, release_at):
    """One Shift-drag selection: hold Shift, drag the mouse, let go."""
    detector.key_pressed(True, press_at)
    detector.pointer_activity()  # button press + motion during the drag
    return detector.key_released(True, release_at)


def test_shift_drag_selection_does_not_arm_the_double_shift_gesture():
    """Shift held for a drag is a selection, never half of a double-tap.

    Without this, two Shift-drags inside the 0.5 s window open Omnisearch,
    which takes focus from the terminal -- the "works once or twice, then all
    copy methods stop" report. Shift-drag is the *only* way to select while a
    remote app owns the pointer, so this collision removes the last escape
    hatch.
    """
    detector = DoubleShiftDetector(interval_seconds=0.5)

    assert _shift_drag(detector, 1.0, 1.4) is False
    # Second selection, well inside the double-tap window.
    assert _shift_drag(detector, 1.6, 2.0) is False
    # And a third: the gesture must not accumulate across drags either.
    assert _shift_drag(detector, 2.1, 2.5) is False


def test_pointer_activity_between_taps_cancels_a_pending_double_shift():
    """A click between two Shift taps means the user is selecting, not searching."""
    detector = DoubleShiftDetector(interval_seconds=0.5)

    detector.key_pressed(True, 1.0)
    assert detector.key_released(True, 1.05) is False

    detector.pointer_activity()

    detector.key_pressed(True, 1.2)
    assert detector.key_released(True, 1.25) is False


def test_plain_double_shift_still_opens_omnisearch():
    """The fix must not cost the shortcut its ordinary keyboard-only path."""
    detector = DoubleShiftDetector(interval_seconds=0.5)

    detector.key_pressed(True, 1.0)
    assert detector.key_released(True, 1.05) is False
    detector.key_pressed(True, 1.2)
    assert detector.key_released(True, 1.25) is True


class _RecordingController:
    """Stand-in for the GTK controllers ``_setup_omnisearch_shortcut`` installs."""

    def __init__(self, kind):
        self.kind = kind
        self.button = None
        self.phase = None
        self.exclusive = None
        self.handlers = {}

    def set_propagation_phase(self, phase):
        self.phase = phase

    def set_button(self, button):
        self.button = button

    def set_exclusive(self, exclusive):
        self.exclusive = exclusive

    def connect(self, signal, handler):
        self.handlers[signal] = handler


class _FakeGtk:
    """Only the surface ``_setup_omnisearch_shortcut`` touches."""

    PropagationPhase = SimpleNamespace(CAPTURE="capture", BUBBLE="bubble")
    EventSequenceState = SimpleNamespace(DENIED="denied")

    @staticmethod
    def EventControllerKey():
        return _RecordingController("key")

    @staticmethod
    def GestureClick():
        return _RecordingController("click")

    @staticmethod
    def EventControllerMotion():
        return _RecordingController("motion")


class _DummyGesture:
    def __init__(self):
        self.state = None

    def set_state(self, state):
        self.state = state


class _OmnisearchWindow:
    """Minimal stand-in bound to the real MainWindow gesture handlers."""

    _setup_omnisearch_shortcut = MainWindow._setup_omnisearch_shortcut
    _omnisearch_uses_double_shift = MainWindow._omnisearch_uses_double_shift
    _is_shift_key = staticmethod(MainWindow._is_shift_key)
    _on_omnisearch_key_pressed = MainWindow._on_omnisearch_key_pressed
    _on_omnisearch_key_released = MainWindow._on_omnisearch_key_released
    _on_omnisearch_pointer_pressed = MainWindow._on_omnisearch_pointer_pressed
    _on_omnisearch_pointer_activity = MainWindow._on_omnisearch_pointer_activity

    def __init__(self, monkeypatch):
        self.opened = []
        self.controllers = []
        self.now = 0.0
        monkeypatch.setattr(window_module, "Gtk", _FakeGtk)
        self._setup_omnisearch_shortcut()

    # Real MainWindow inherits these from Gtk/Adw; the stubs do not.
    def add_controller(self, controller):
        self.controllers.append(controller)

    def get_application(self):
        return SimpleNamespace(
            accelerators_enabled=True,
            get_effective_shortcuts=lambda _action: [DOUBLE_SHIFT_SHORTCUT],
        )

    def activate_omni_search(self):
        self.opened.append(True)

    def _shortcut_event_time(self):
        return self.now

    def _pointer_controller(self):
        for controller in self.controllers:
            if controller.kind == "click":
                return controller
        raise AssertionError("pointer GestureClick was not installed")

    # -- event helpers -----------------------------------------------------
    def press_shift(self, at):
        self.now = at
        self._on_omnisearch_key_pressed(None, Gdk.KEY_Shift_L, 0, 0)

    def release_shift(self, at):
        self.now = at
        self._on_omnisearch_key_released(None, Gdk.KEY_Shift_L, 0, 0)

    def click(self):
        """Deliver a button press through the installed pointer observer."""
        gesture = _DummyGesture()
        pointer = self._pointer_controller()
        pointer.handlers["pressed"](gesture, 1, 0.0, 0.0)
        return gesture


@pytest.mark.skipif(MainWindow is None, reason="GTK stubs unavailable")
def test_window_installs_a_pointer_observer_for_the_double_shift_gesture(monkeypatch):
    """A correct detector with no pointer wiring would still ship the bug."""
    window = _OmnisearchWindow(monkeypatch)

    kinds = [controller.kind for controller in window.controllers]
    assert "key" in kinds
    assert "click" in kinds
    assert "motion" in kinds

    pointer = window._pointer_controller()
    assert pointer.button == 0, "must observe every button, not just primary"
    assert pointer.phase == _FakeGtk.PropagationPhase.BUBBLE
    assert pointer.exclusive is False
    assert "pressed" in pointer.handlers

    # Observing only: the sequence is denied so terminal selection is untouched.
    gesture = window.click()
    assert gesture.state == _FakeGtk.EventSequenceState.DENIED


@pytest.mark.skipif(MainWindow is None, reason="GTK stubs unavailable")
def test_window_shift_drag_selections_never_open_omnisearch(monkeypatch):
    """The reporter's actual sequence, driven through the real handlers."""
    window = _OmnisearchWindow(monkeypatch)

    window.press_shift(1.0)
    window.click()          # begin the Shift+drag selection
    window.release_shift(1.4)

    window.press_shift(1.6)  # second selection, inside the double-tap window
    window.click()
    window.release_shift(2.0)

    assert window.opened == []


@pytest.mark.skipif(MainWindow is None, reason="GTK stubs unavailable")
def test_window_keyboard_only_double_shift_still_opens_omnisearch(monkeypatch):
    window = _OmnisearchWindow(monkeypatch)

    window.press_shift(1.0)
    window.release_shift(1.05)
    window.press_shift(1.2)
    window.release_shift(1.25)

    assert window.opened == [True]


# ---------------------------------------------------------------------------
# Defect 2: a copy that finds nothing selected says nothing at all.
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Backend stub with the copy contract both real backends implement."""

    def __init__(self, selection):
        self._selection = selection

    def get_has_selection(self):
        return bool(self._selection)

    def copy_clipboard(self, format="text", on_complete=None):
        copied = bool(self._selection)
        if on_complete is not None:
            on_complete(copied)


def _terminal(selection="", *, mouse_tracking=False):
    from sshpilot.terminal import TerminalWidget

    tracker = MouseTrackingState()
    if mouse_tracking:
        tracker.feed(MOUSE_ON)

    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.backend = _FakeBackend(selection)
    terminal._mouse_tracking = tracker
    terminal.toasts = []
    terminal._show_toast = lambda message, timeout=3: terminal.toasts.append(message)
    return terminal


def test_successful_copy_still_confirms():
    terminal = _terminal("selected text")
    terminal.copy_text()
    assert terminal.toasts == ["Copied to clipboard"]


def test_empty_copy_is_reported_instead_of_failing_silently():
    """No selection must produce feedback, not nothing.

    Every failing attempt in the reporter's log ended at
    ``copied=False reason=empty-selection`` with no UI trace whatsoever.
    """
    terminal = _terminal("")
    terminal.copy_text()

    assert terminal.toasts, "copy with no selection produced no user feedback"
    assert "Copied to clipboard" not in terminal.toasts


def test_empty_copy_under_mouse_tracking_names_the_shift_workaround():
    """When a remote app owns the pointer, say how to select anyway."""
    terminal = _terminal("", mouse_tracking=True)
    terminal.copy_text()

    assert terminal.toasts
    assert "Shift" in terminal.toasts[0], terminal.toasts
