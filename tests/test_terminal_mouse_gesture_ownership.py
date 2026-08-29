"""Capture gestures must release every pointer sequence SSH Pilot does not own."""

from unittest.mock import patch

import pytest

import sshpilot.terminal as terminal_mod


class _Gesture:
    def __init__(self):
        self.states = []

    def set_state(self, state):
        self.states.append(state)


def _resolved_state(handled):
    gesture = _Gesture()
    terminal_mod._finish_capture_gesture(gesture, handled)
    return gesture.states


@pytest.mark.parametrize(
    ("handled", "expected"),
    ((False, "denied"), (True, "claimed")),
)
def test_capture_gesture_resolves_ownership(handled, expected):
    with patch.object(
        terminal_mod.Gtk.EventSequenceState, "DENIED", "denied"
    ), patch.object(
        terminal_mod.Gtk.EventSequenceState, "CLAIMED", "claimed"
    ):
        assert _resolved_state(handled) == [expected]


def test_irrelevant_primary_context_click_is_denied():
    with patch.object(terminal_mod.Gdk, "BUTTON_SECONDARY", 3), patch.object(
        terminal_mod.Gtk.EventSequenceState, "DENIED", "denied"
    ), patch.object(terminal_mod.Gtk.EventSequenceState, "CLAIMED", "claimed"):
        handled = terminal_mod._context_click_is_handled(
            1,
            paste_on_right_click=False,
            shift_held=False,
            native_vte_menu=True,
        )
        assert _resolved_state(handled) == ["denied"]


def test_context_gesture_avoids_vte_primary_sequences_but_preserves_pyxterm_dismissal():
    with patch.object(terminal_mod.Gdk, "BUTTON_SECONDARY", 3):
        assert terminal_mod._context_gesture_button(manual_dismiss=False) == 3
        assert terminal_mod._context_gesture_button(manual_dismiss=True) == 0


def test_shift_primary_link_interaction_is_denied():
    with patch.object(terminal_mod.Gtk.EventSequenceState, "DENIED", "denied"), patch.object(
        terminal_mod.Gtk.EventSequenceState, "CLAIMED", "claimed"
    ):
        handled = terminal_mod._link_click_is_handled(
            1, active=True, modifier_held=False, uri="https://example.com"
        )
        assert _resolved_state(handled) == ["denied"]


@pytest.mark.parametrize(
    ("uri", "expected"),
    ((None, "denied"), ("https://example.com", "claimed")),
)
def test_ctrl_click_ownership_depends_on_uri(uri, expected):
    with patch.object(terminal_mod.Gtk.EventSequenceState, "DENIED", "denied"), patch.object(
        terminal_mod.Gtk.EventSequenceState, "CLAIMED", "claimed"
    ):
        handled = terminal_mod._link_click_is_handled(
            1, active=True, modifier_held=True, uri=uri
        )
        assert _resolved_state(handled) == [expected]


def test_application_owned_right_click_is_claimed():
    with patch.object(terminal_mod.Gdk, "BUTTON_SECONDARY", 3), patch.object(
        terminal_mod.Gtk.EventSequenceState, "DENIED", "denied"
    ), patch.object(terminal_mod.Gtk.EventSequenceState, "CLAIMED", "claimed"):
        handled = terminal_mod._context_click_is_handled(
            3,
            paste_on_right_click=False,
            shift_held=False,
            native_vte_menu=False,
        )
        assert _resolved_state(handled) == ["claimed"]


def test_native_vte_right_click_remains_vte_owned_unless_pasting():
    with patch.object(terminal_mod.Gdk, "BUTTON_SECONDARY", 3):
        assert not terminal_mod._context_click_is_handled(
            3,
            paste_on_right_click=False,
            shift_held=True,
            native_vte_menu=True,
        )
        assert terminal_mod._context_click_is_handled(
            3,
            paste_on_right_click=True,
            shift_held=False,
            native_vte_menu=True,
        )


def test_mouse_tracking_releases_right_click_even_when_paste_is_on():
    with patch.object(terminal_mod.Gdk, "BUTTON_SECONDARY", 3), patch.object(
        terminal_mod.Gtk.EventSequenceState, "DENIED", "denied"
    ), patch.object(
        terminal_mod.Gtk.EventSequenceState, "CLAIMED", "claimed"
    ):
        handled = terminal_mod._context_click_is_handled(
            3,
            paste_on_right_click=True,
            shift_held=False,
            native_vte_menu=True,
            mouse_tracking=True,
        )
        assert _resolved_state(handled) == ["denied"]
        # Shift+right-click still belongs to SSH Pilot (menu / paste override).
        handled = terminal_mod._context_click_is_handled(
            3,
            paste_on_right_click=True,
            shift_held=True,
            native_vte_menu=False,
            mouse_tracking=True,
        )
        assert _resolved_state(handled) == ["claimed"]
