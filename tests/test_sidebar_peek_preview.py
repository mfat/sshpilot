"""The hover-reveal sketch plays the reveal in the right order."""

from sshpilot.sidebar_peek_preview import LOOP_S, frame_at


def test_sidebar_appears_only_after_the_pointer_rests_on_the_button():
    pointer, _alpha, panel, on_button, _row = frame_at(0.0)
    assert panel == 0 and not on_button
    _pointer, _alpha, panel, on_button, _row = frame_at(1.0)
    assert on_button and panel == 0
    _pointer, _alpha, panel, _on, row = frame_at(2.2)
    assert panel == 1 and row is not None
    _pointer, _alpha, panel, _on, _row = frame_at(3.7)
    assert panel == 0


def test_the_loop_repeats():
    assert frame_at(2.2) == frame_at(2.2 + LOOP_S)
