"""libghostty-vt model specifics (the shared contract is in test_pyte_terminal_model)."""

from __future__ import annotations

import sys
from collections import deque

import pytest


@pytest.fixture
def make_model():
    from sshpilot.ghostty_terminal_model import GhosttyTerminalModel, GhosttyUnavailable

    def make(*args, **kwargs):
        try:
            return GhosttyTerminalModel(*args, **kwargs)
        except GhosttyUnavailable as exc:
            pytest.skip(str(exc))

    return make


def _fill(model, count: int, start: int = 0) -> None:
    model.feed(b"".join(b"line%d\r\n" % index for index in range(start, start + count)))


def test_scrollback_pushes_count_rows_moved_into_history(make_model):
    model = make_model(20, 5)
    _fill(model, 3)
    assert model.screen.scrollback_pushes == 0
    _fill(model, 10, start=3)
    assert model.screen.scrollback_pushes == 9
    model.feed(b"\x1b[?1049h")
    _fill(model, 10)
    assert model.screen.scrollback_pushes == 9  # the alternate screen has no history
    model.feed(b"\x1b[?1049l")
    _fill(model, 1, start=13)
    assert model.screen.scrollback_pushes == 10


def test_scrollback_pushes_stay_exact_while_history_is_pruned(make_model):
    model = make_model(20, 5, scrollback_limit=50)
    pruned = False
    for index in range(4000):
        before = model.history_length
        model.feed(b"x%d\r\n" % index)
        pruned = pruned or model.history_length < before
    assert pruned  # libghostty dropped whole pages of history on the way
    assert model.screen.scrollback_pushes == 4000 - 4


def test_reads_deep_scrollback_and_screen_in_any_order(make_model):
    model = make_model(30, 4)
    _fill(model, 500)
    history = model.history_length
    assert history == 497
    assert model.row_text(0).rstrip() == "line0"
    assert model.row_text(history + 2).rstrip() == "line499"
    assert model.row_text(250).rstrip() == "line250"
    assert model.row_text(history - 1).rstrip() == "line496"
    assert model.all_text().splitlines()[-1] == "line499"
    assert model.line(history + 4) == {}


def test_row_edited_then_scrolled_away_in_one_write_is_read_fresh(make_model):
    model = make_model(20, 3)
    model.feed(b"old\r\nb\r\nc")
    assert model.row_text(0).rstrip() == "old"
    # Rewrite the top row, then scroll it into the history, in one write.
    model.feed(b"\x1b[1;1Hnew\x1b[3;1H\r\n\r\n")
    assert model.history_length == 2
    assert model.row_text(0).rstrip() == "new"


def test_scrollback_limit_follows_the_view_and_zero_disables_history(make_model):
    model = make_model(20, 3)
    screen = model.screen
    assert screen.scrollback.maxlen == 10000
    screen.scrollback = deque(screen.scrollback, maxlen=0)
    _fill(model, 50)
    assert model.history_length == 0
    screen.scrollback = deque(screen.scrollback, maxlen=200)
    assert screen.scrollback.maxlen == 200
    _fill(model, 50)
    assert 0 < model.history_length < 400


def test_reset_keeps_or_clears_history(make_model):
    model = make_model(20, 3)
    _fill(model, 10)
    model.feed(b"\x1b[?1h\x1b[?2004hprompt")
    model.reset(clear_scrollback=False)
    assert model.history_length > 0
    assert not model.screen.app_cursor_keys and not model.screen.bracketed_paste
    assert model.all_text().splitlines()[0] == "line0"
    assert "prompt" not in model.all_text()
    model.reset(clear_scrollback=True)
    assert model.history_length == 0
    assert model.all_text() == ""


def test_graphemes_wide_cells_and_background_only_cells(make_model):
    model = make_model(20, 3)
    model.feed("é 🧪 中\x1b[48;5;196m\x1b[K".encode())
    line = model.line(model.history_length)
    assert line[0].data == "é"
    assert line[2].data == "🧪" and line[3].data == ""
    assert line[5].data == "中" and line[6].data == ""
    # Erased with a background colour: no text, the colour kept.
    assert (line[10].data, line[10].bg) == (" ", "ff0000")
    model.feed(b"\r\n\x1b[48;2;1;2;3m\x1b[K")
    assert model.line(model.history_length + 1)[0].bg == "010203"


def test_truecolor_and_palette_styles(make_model):
    model = make_model(20, 3)
    model.feed(b"\x1b[38;2;10;20;30;48;5;21;9;5mX\x1b[0m\x1b[94mY")
    x, y = model.line(model.history_length)[0], model.line(model.history_length)[1]
    assert (x.fg, x.bg, x.strikethrough, x.blink) == ("0a141e", "0000ff", True, True)
    assert (y.fg, y.bg, y.bold) == ("brightblue", "default", False)


def test_title_bell_and_replies(make_model):
    replies = []
    model = make_model(20, 3, on_reply=replies.append)
    model.feed(b"\x1b]2;caf\xc3\xa9\x07\x07\x1b[5n")
    assert model.screen.title == "café" and model.screen.title_changed
    assert model.screen.bell_rung
    assert replies == [b"\x1b[0n"]


def test_lookup_prefers_override_then_frozen_bundle(monkeypatch, tmp_path):
    from sshpilot import ghostty_terminal_model as ghostty

    monkeypatch.setenv("SSHPILOT_LIBGHOSTTY_VT", str(tmp_path / "custom.dll"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    paths = ghostty._candidate_paths()
    assert paths[0] == str(tmp_path / "custom.dll")
    assert paths[1].startswith(str(tmp_path / "bundle"))


def test_unknown_cell_layout_is_refused():
    from sshpilot.ghostty_terminal_model import _parse_layout

    with pytest.raises((KeyError, ValueError)):
        _parse_layout({"schema": 2, "types": {}})
    with pytest.raises((KeyError, ValueError)):
        _parse_layout({"schema": 1, "types": {"GhosttyCell": {"kind": "struct", "size": 16}}})


def test_osc8_link_targets_on_screen_and_in_history(make_model):
    model = make_model(30, 4)
    model.feed(b"see \x1b]8;;https://example.org/osc8\x1b\\docs\x1b]8;;\x1b\\ here\r\n")
    row = model.history_length
    assert [model.hyperlink_uri(row, column) for column in (3, 4, 7, 8)] == [
        None, "https://example.org/osc8", "https://example.org/osc8", None,
    ]
    _fill(model, 10)
    assert model.history_length > row  # now in the scrollback
    assert model.hyperlink_uri(row, 5) == "https://example.org/osc8"
    assert model.hyperlink_uri(-1, 0) is None and model.hyperlink_uri(row, 99) is None
