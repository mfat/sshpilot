"""Headless emulator model behind the pyte terminal backend."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("pyte")

from sshpilot.pyte_terminal_model import (  # noqa: E402
    TerminalModel,
    encode_key,
    encode_mouse,
)


def _fill(model: TerminalModel, count: int) -> None:
    for index in range(count):
        model.feed(f"line{index}\r\n".encode())


def test_scrolled_lines_enter_scrollback():
    model = TerminalModel(20, 5)
    _fill(model, 12)
    assert model.history_length == 8
    assert model.row_text(0).rstrip() == "line0"


def test_alternate_screen_keeps_main_screen_and_skips_scrollback():
    model = TerminalModel(20, 5)
    _fill(model, 3)
    model.feed(b"\x1b[?1049h\x1b[2J\x1b[Hfull screen")
    assert model.screen.alternate_active
    for _ in range(10):
        model.feed(b"\r\n")
    assert model.history_length == 0
    model.feed(b"\x1b[?1049l")
    assert not model.screen.alternate_active
    assert model.row_text(0).rstrip() == "line0"


def test_device_queries_are_answered():
    replies = []
    model = TerminalModel(20, 5, on_reply=replies.append)
    model.feed(b"\x1b[c\x1b[6n")
    assert replies == [b"\x1b[?6c", b"\x1b[1;1R"]


def test_shrinking_keeps_cursor_row_and_growing_restores_history():
    model = TerminalModel(20, 5)
    _fill(model, 12)
    model.resize(20, 3)
    assert model.screen.cursor.y == 2
    assert model.history_length == 10
    model.resize(20, 8)
    assert model.history_length == 5
    assert model.screen.cursor.y == 7


def test_ed3_clears_scrollback_only():
    model = TerminalModel(20, 5)
    _fill(model, 12)
    model.feed(b"\x1b[3J")
    assert model.history_length == 0
    assert model.row_text(model.history_length + 3).rstrip() == "line11"


def test_modes_and_title():
    model = TerminalModel(20, 5)
    model.feed(b"\x1b[?1h\x1b[?2004h\x1b[?1002h\x1b[?1006h\x1b]0;remote\x07")
    screen = model.screen
    assert screen.app_cursor_keys
    assert screen.bracketed_paste
    assert screen.mouse_tracking == "drag"
    assert screen.mouse_sgr
    assert screen.title == "remote" and screen.title_changed


def test_selection_text_and_search():
    model = TerminalModel(20, 5)
    _fill(model, 12)
    assert model.text_between((5, 0), (6, 4)) == "line5\nline6"
    pattern = re.compile("line1")

    def matcher(text, start):
        match = pattern.search(text, start)
        return (match.start(), match.end()) if match else None

    assert model.find(matcher) == (1, 0, 5)
    assert model.find(matcher, after=(1, 0)) == (10, 0, 5)
    assert model.find(matcher, backwards=True) == (11, 0, 5)


@pytest.mark.parametrize(
    "args, kwargs, expected",
    [
        (("Up",), {}, b"\x1b[A"),
        (("Up",), {"app_cursor": True}, b"\x1bOA"),
        (("Up",), {"ctrl": True}, b"\x1b[1;5A"),
        (("c", "c"), {"ctrl": True}, b"\x03"),
        (("x", "x"), {"alt": True}, b"\x1bx"),
        (("F1",), {}, b"\x1bOP"),
        (("F5",), {}, b"\x1b[15~"),
        (("Delete",), {"shift": True}, b"\x1b[3;2~"),
        (("BackSpace",), {}, b"\x7f"),
        (("Return",), {}, b"\r"),
        (("ISO_Left_Tab",), {"shift": True}, b"\x1b[Z"),
        (("space", " "), {"ctrl": True}, b"\x00"),
        (("Shift_L",), {}, None),
    ],
)
def test_encode_key(args, kwargs, expected):
    assert encode_key(*args, **kwargs) == expected


def test_encode_mouse_sgr_and_legacy():
    assert encode_mouse(0, 4, 2, action="press", sgr=True) == b"\x1b[<0;5;3M"
    assert encode_mouse(0, 4, 2, action="release", sgr=True) == b"\x1b[<0;5;3m"
    assert encode_mouse(0, 4, 2, action="press", sgr=False) == b"\x1b[M %#"
    assert encode_mouse(0, 4, 2, action="release", sgr=False) == b"\x1b[M#%#"
    assert encode_mouse(0, 300, 2, action="press", sgr=False) is None
