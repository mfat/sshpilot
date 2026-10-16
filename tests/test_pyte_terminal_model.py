"""Headless emulator models behind the built-in terminal backend.

Every model test runs against both the pyte model and the libvterm model
(``make_model`` fixture); each is skipped when its library is missing.
"""

from __future__ import annotations

import re

import pytest

from sshpilot.terminal_model_common import encode_key, encode_mouse


def _pyte_model(*args, **kwargs):
    pytest.importorskip("pyte")
    from sshpilot.pyte_terminal_model import TerminalModel

    return TerminalModel(*args, **kwargs)


def _vterm_model(*args, **kwargs):
    from sshpilot.vterm_terminal_model import VtermTerminalModel, VtermUnavailable

    try:
        return VtermTerminalModel(*args, **kwargs)
    except VtermUnavailable as exc:
        pytest.skip(str(exc))


@pytest.fixture(params=[_pyte_model, _vterm_model], ids=["pyte", "vterm"])
def make_model(request):
    return request.param


def _fill(model, count: int) -> None:
    for index in range(count):
        model.feed(f"line{index}\r\n".encode())


def test_scrolled_lines_enter_scrollback(make_model):
    model = make_model(20, 5)
    _fill(model, 12)
    assert model.history_length == 8
    assert model.row_text(0).rstrip() == "line0"


def test_alternate_screen_keeps_main_screen_and_skips_scrollback(make_model):
    model = make_model(20, 5)
    _fill(model, 3)
    model.feed(b"\x1b[?1049h\x1b[2J\x1b[Hfull screen")
    assert model.screen.alternate_active
    for _ in range(10):
        model.feed(b"\r\n")
    assert model.history_length == 0
    model.feed(b"\x1b[?1049l")
    assert not model.screen.alternate_active
    assert model.row_text(0).rstrip() == "line0"


def test_resize_inside_alternate_screen_restores_main_screen(make_model):
    model = make_model(20, 5)
    _fill(model, 3)
    model.feed(b"\x1b[?1049h\x1b[2J\x1b[Hfull screen")
    model.resize(30, 8)
    model.feed(b"\x1b[2J\x1b[Hredrawn")
    model.resize(20, 4)
    model.feed(b"\x1b[?1049l")
    assert not model.screen.alternate_active
    text = model.all_text()
    assert "full screen" not in text and "redrawn" not in text
    assert [line for line in text.splitlines() if line] == ["line0", "line1", "line2"]


@pytest.mark.parametrize(
    "sizes, history",
    [
        (((20, 3),), 10),
        (((20, 8),), 5),
        (((20, 3), (20, 8)), 5),
        (((30, 2), (15, 9)), 4),
    ],
    ids=["shrink", "grow", "shrink-grow", "both-axes"],
)
def test_prompt_survives_resizes_inside_alternate_screen(make_model, request, sizes, history):
    if "pyte" in request.node.callspec.id:
        request.applymarker(pytest.mark.xfail(reason="known pyte alt-screen resize bug", strict=False))
    model = make_model(20, 5)
    _fill(model, 12)
    model.feed(b"$ less")
    model.feed(b"\x1b[?1049h\x1b[2J\x1b[Hpager\r\nmore")
    for columns, lines in sizes:
        model.resize(columns, lines)
        model.feed(b"\x1b[2J\x1b[Hredrawn")
    model.feed(b"\x1b[?1049lX")
    top = model.history_length
    assert top == history
    assert model.row_text(top - 1).rstrip() == f"line{history - 1}"
    assert model.row_text(top + model.screen.cursor.y).rstrip() == "$ lessX"
    assert "pager" not in model.all_text() and "redrawn" not in model.all_text()


@pytest.mark.parametrize("sizes", [((20, 3),), ((20, 8), (20, 3)), ((20, 3), (20, 9))])
def test_next_prompt_follows_command_after_alternate_screen_resize(make_model, request, sizes):
    # As vim/htop leave it: the command line, then the cursor on a blank row.
    if "pyte" in request.node.callspec.id:
        request.applymarker(pytest.mark.xfail(reason="known pyte alt-screen resize bug", strict=False))
    model = make_model(20, 5)
    _fill(model, 12)
    model.feed(b"$ vim\r\n")
    model.feed(b"\x1b[?1049h\x1b[2J\x1b[Heditor")
    for columns, lines in sizes:
        model.resize(columns, lines)
        model.feed(b"\x1b[2J\x1b[Hredrawn")
    model.feed(b"\x1b[?1049l$ next")
    rows = [model.row_text(row).rstrip() for row in range(model.total_rows)]
    while rows and not rows[-1]:
        rows.pop()
    assert rows[-3:] == ["line11", "$ vim", "$ next"]
    assert rows[:3] == ["line0", "line1", "line2"]


def test_cell_attributes(make_model):
    model = make_model(20, 5)
    model.feed(b"\x1b[1;3;4;7;31;42mA\x1b[0m\x1b[38;5;200mB\x1b[0m\xe4\xb8\xad.")
    line = model.line(model.history_length)
    a, b = line[0], line[1]
    assert (a.data, a.fg, a.bg) == ("A", "red", "green")
    assert a.bold and a.italics and a.underscore and a.reverse
    assert (b.data, b.fg, b.bg, b.bold) == ("B", "ff00d7", "default", False)
    assert (line[2].data, line[3].data, line[4].data) == ("中", "", ".")


def test_focus_and_any_motion_modes(make_model):
    from sshpilot.terminal_model_common import MODE_FOCUS_EVENTS

    model = make_model(20, 5)
    model.feed(b"\x1b[?1004h\x1b[?1003h")
    assert MODE_FOCUS_EVENTS in model.screen.mode
    assert model.screen.mouse_tracking == "any"
    assert not model.screen.mouse_sgr
    model.feed(b"\x1b[?1004l\x1b[?1003l")
    assert MODE_FOCUS_EVENTS not in model.screen.mode
    assert model.screen.mouse_tracking is None


def test_device_queries_are_answered(make_model):
    replies = []
    model = make_model(20, 5, on_reply=replies.append)
    model.feed(b"\x1b[c\x1b[6n")
    # Primary DA wording differs (pyte: VT102 "?6c", libvterm: VT100+AVO "?1;2c").
    assert len(replies) == 2
    assert re.fullmatch(rb"\x1b\[\?[0-9;]+c", replies[0])
    assert replies[1] == b"\x1b[1;1R"


def test_shrinking_keeps_cursor_row_and_growing_restores_history(make_model):
    model = make_model(20, 5)
    _fill(model, 12)
    model.resize(20, 3)
    assert model.screen.cursor.y == 2
    assert model.history_length == 10
    model.resize(20, 8)
    assert model.history_length == 5
    assert model.screen.cursor.y == 7


def _conhost_repaint(model, rows) -> None:
    # What conhost sends after a resize: its viewport, from the top row down.
    model.feed(b"\x1b[H" + b"\x1b[K\r\n".join(rows) + b"\x1b[K\x1b[J")


def _transcript(model):
    rows = [model.row_text(row).rstrip() for row in range(model.total_rows)]
    while rows and not rows[-1]:
        rows.pop()
    return rows


def test_growing_under_conpty_keeps_history_in_place(make_model):
    model = make_model(20, 6)
    model.pull_history_on_grow = False
    for number in range(1000, 1013):
        model.feed(b"%d\r\n" % number)
    model.feed(b"$ ")
    expected = [str(number) for number in range(1000, 1013)] + ["$"]
    model.resize(20, 3)
    _conhost_repaint(model, [b"1011", b"1012", b"$ "])
    assert model.history_length == 11
    model.resize(20, 9)
    assert model.history_length == 11
    assert model.screen.cursor.y == 2
    _conhost_repaint(model, [b"1011", b"1012", b"$ "])
    assert _transcript(model) == expected


def test_growing_with_pull_back_loses_rows_under_conpty(make_model):
    # Why ConPTY sessions turn the pull off: conhost paints over pulled rows.
    model = make_model(20, 6)
    for number in range(1000, 1013):
        model.feed(b"%d\r\n" % number)
    model.feed(b"$ ")
    model.resize(20, 3)
    model.resize(20, 9)
    _conhost_repaint(model, [b"1011", b"1012", b"$ "])
    assert "1007" not in _transcript(model)


def test_ed3_clears_scrollback_only(make_model):
    model = make_model(20, 5)
    _fill(model, 12)
    model.feed(b"\x1b[3J")
    assert model.history_length == 0
    assert model.row_text(model.history_length + 3).rstrip() == "line11"


def test_modes_and_title(make_model):
    model = make_model(20, 5)
    model.feed(b"\x1b[?1h\x1b[?2004h\x1b[?1002h\x1b[?1006h\x1b]0;remote\x07")
    screen = model.screen
    assert screen.app_cursor_keys
    assert screen.bracketed_paste
    assert screen.mouse_tracking == "drag"
    assert screen.mouse_sgr
    assert screen.title == "remote" and screen.title_changed


def test_selection_text_and_search(make_model):
    model = make_model(20, 5)
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


def test_libvterm_row_reads_match_per_cell_reads():
    model = _vterm_model(12, 3)
    if model._get_row is None:
        pytest.skip("libvterm build without sshpilot_vterm_screen_get_row")
    model.feed("\x1b[1;31mred\x1b[0m 中😀 e\u0301\x1b[7mrev\r\n\x1b[48;5;236mbg".encode("utf-8"))
    for y in range(3):
        assert model._screen_row_raw(y) == model._screen_row_raw_per_cell(y)


def test_libvterm_lookup_prefers_override_then_frozen_bundle(monkeypatch, tmp_path):
    import sys

    from sshpilot import vterm_terminal_model as vterm

    monkeypatch.setenv("SSHPILOT_LIBVTERM", str(tmp_path / "custom.dll"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    paths = vterm._candidate_paths()
    assert paths[0] == str(tmp_path / "custom.dll")
    assert paths[1].startswith(str(tmp_path / "bundle"))
