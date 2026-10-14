"""CSI forms pyte 0.8 mis-parses are rewritten or dropped before it sees them."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

from sshpilot.pyte_terminal_model import TerminalModel  # noqa: E402

FINALS = [chr(code) for code in range(0x40, 0x7F)]


def _row(model, row=0):
    return model.row_text(model.history_length + row).rstrip()


@pytest.mark.parametrize("final", FINALS)
@pytest.mark.parametrize("prefix", ["?1", ">1", "=1", "<1", "1 ", "1!", '1"', "1$", "38:2:1:2:3"])
def test_unusual_csi_neither_raises_nor_prints(prefix, final):
    if prefix == "?1" and final in "hl":
        pytest.skip("private modes are passed through")
    model = TerminalModel(20, 3)
    model.feed(b"a" + f"\x1b[{prefix}{final}".encode() + b"b")
    if final not in "bJKXP@":  # these edit the line itself
        assert _row(model) == "ab"


def test_modify_other_keys_does_not_underline():
    model = TerminalModel(20, 3)
    model.feed(b"a\x1b[>4;2mb")
    assert not model.line(model.history_length)[1].underscore


def test_colon_truecolor_is_applied():
    model = TerminalModel(20, 3)
    model.feed(b"\x1b[38:2::10:20:30mx\x1b[48:5:196my")
    line = model.line(model.history_length)
    assert line[0].fg == "0a141e"
    assert line[1].bg != "default"


def test_underline_colour_is_dropped_without_eating_other_attributes():
    model = TerminalModel(20, 3)
    model.feed(b"\x1b[58;2;1;2;3;1mx\x1b[0;58;5;9;4my")
    line = model.line(model.history_length)
    assert line[0].bold and not line[0].underscore
    assert line[1].underscore


def test_sequences_split_across_feeds():
    data = b"a\x1b[>4;2mb\x1b[?4mc\x1b[38:2::1:2:3md"
    for cut in range(len(data) + 1):
        model = TerminalModel(20, 3)
        model.feed(data[:cut])
        model.feed(data[cut:])
        assert _row(model) == "abcd", cut


def test_secondary_da_is_answered():
    replies = []
    model = TerminalModel(20, 3, on_reply=replies.append)
    model.feed(b"\x1b[>c")
    assert replies == [b"\x1b[>0;100;0c"]
    assert _row(model) == ""


def test_private_modes_still_work():
    model = TerminalModel(20, 3)
    model.feed(b"\x1b[?2004h")
    assert model.screen.bracketed_paste
    model.feed(b"\x1b[?2004l")
    assert not model.screen.bracketed_paste
