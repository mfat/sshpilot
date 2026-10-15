"""Mode 2026 (synchronized output) tracking for the built-in terminal view."""

from __future__ import annotations

from sshpilot.terminal_model_common import SynchronizedOutput

BEGIN = b"\x1b[?2026h"
END = b"\x1b[?2026l"
QUERY = b"\x1b[?2026$p"


def _tracker():
    now = [0.0]
    return SynchronizedOutput(lambda: now[0]), now


def test_begin_and_end_toggle_active_and_pass_through():
    sync, _ = _tracker()
    assert sync.filter(b"a" + BEGIN + b"b") == (b"a" + BEGIN + b"b", b"")
    assert sync.active
    assert sync.filter(b"c" + END + b"d") == (b"c" + END + b"d", b"")
    assert not sync.active


def test_query_is_answered_and_removed():
    sync, _ = _tracker()
    assert sync.filter(b"x" + QUERY + b"y") == (b"xy", b"\x1b[?2026;2$y")
    sync.filter(BEGIN)
    assert sync.filter(QUERY) == (b"", b"\x1b[?2026;1$y")


def test_sequences_split_across_feeds():
    data = b"a" + BEGIN + b"b" + QUERY + b"c" + END + b"d"
    for cut in range(len(data) + 1):
        sync, _ = _tracker()
        first, reply_one = sync.filter(data[:cut])
        second, reply_two = sync.filter(data[cut:])
        assert first + second == b"a" + BEGIN + b"bc" + END + b"d", cut
        assert reply_one + reply_two == b"\x1b[?2026;1$y", cut
        assert not sync.active, cut


def test_open_update_times_out():
    sync, now = _tracker()
    sync.filter(BEGIN)
    now[0] = SynchronizedOutput.TIMEOUT / 2
    assert sync.active
    assert 0 < sync.remaining() <= SynchronizedOutput.TIMEOUT / 2
    now[0] = SynchronizedOutput.TIMEOUT
    assert not sync.active
    assert sync.remaining() == 0.0


def test_plain_output_is_untouched():
    sync, _ = _tracker()
    data = b"\x1b[?25h\x1b[?2004h hello \x1b[1;31m world"
    assert sync.filter(data) == (data, b"")
