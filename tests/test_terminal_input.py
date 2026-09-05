"""Helpers for daemon-backed terminal input encoding and mouse tracking."""

from sshpilot.terminal_input import MouseTrackingState, commit_payload_to_bytes


def test_ascii_commit_round_trips_as_utf8():
    assert commit_payload_to_bytes("whoami\r", 7) == b"whoami\r"
    assert commit_payload_to_bytes("\x1bOA", 3) == b"\x1bOA"


def test_utf8_character_uses_utf8_when_size_matches_bytes():
    text = "é"
    raw = text.encode("utf-8")
    assert commit_payload_to_bytes(text, len(raw)) == raw


def test_x10_mouse_high_bytes_are_preserved_via_latin1():
    # X10 report: CSI M + (32+button) + (32+x) + (32+y). Column 200 encodes
    # as byte 232 (0xE8), which is not valid UTF-8 on its own.
    text = "\x1b[M" + chr(32) + chr(232) + chr(40)
    assert len(text) == 6
    utf8 = text.encode("utf-8")
    assert len(utf8) != 6
    assert commit_payload_to_bytes(text, 6) == text.encode("latin-1")
    assert commit_payload_to_bytes(text, 6)[4] == 232


def test_ctrl_space_nul_survives_gtype_string_truncation():
    # VTE's "commit" signal is declared with G_TYPE_STRING, so PyGObject
    # stops at the NUL and hands us "" while size still says 1. Captured
    # from real key presses on a PTY-less VTE 0.84 (GH #1240); the same
    # widget with a PTY writes b"\x00" straight to the pty.
    assert commit_payload_to_bytes("", 1) == b"\x00"  # Ctrl+Space, Ctrl+@, Ctrl+2


def test_ctrl_alt_space_keeps_its_trailing_nul():
    # Ctrl+Alt+Space is ESC NUL; the string stops after ESC, size says 2.
    assert commit_payload_to_bytes("\x1b", 2) == b"\x1b\x00"


def test_lone_escape_is_not_padded():
    # Ctrl+[ and Ctrl+3 commit the same text as Ctrl+Alt+Space. Only size
    # tells them apart, so a matching size must never grow the payload.
    assert commit_payload_to_bytes("\x1b", 1) == b"\x1b"


def test_empty_payload_without_length_stays_empty():
    assert commit_payload_to_bytes("", 0) == b""
    assert commit_payload_to_bytes("") == b""


def test_multibyte_text_truncated_at_nul_pads_after_utf8():
    # Padding has to extend the encoding that matches the reported length,
    # not the shorter latin-1 one: "é\0" is three bytes on the wire.
    assert commit_payload_to_bytes("é", 3) == "é".encode("utf-8") + b"\x00"


def test_bytes_payload_is_returned_unchanged():
    payload = b"\x1b[M\xe8("
    assert commit_payload_to_bytes(payload, len(payload)) == payload
    assert commit_payload_to_bytes(bytearray(payload)) == payload


def test_sgr_mouse_stays_ascii():
    report = "\x1b[<0;12;8M"
    assert commit_payload_to_bytes(report, len(report)) == report.encode("ascii")


def test_mouse_tracking_detects_x10_and_sgr():
    state = MouseTrackingState()
    assert state.active is False
    assert state.feed(b"\x1b[?1000h") is True
    assert state.feed(b"\x1b[?1006h") is True
    assert state.feed(b"\x1b[?1006l") is True  # 1000 still on
    assert state.feed(b"\x1b[?1000l") is False


def test_mouse_tracking_combined_csi_and_split_chunks():
    state = MouseTrackingState()
    assert state.feed(b"\x1b[?1000;1006h") is True
    state.reset()
    assert state.active is False
    assert state.feed(b"\x1b[?10") is False
    assert state.feed(b"00h") is True


def test_mouse_tracking_reset_clears_modes():
    state = MouseTrackingState()
    state.feed(b"\x1b[?1000h")
    assert state.active is True
    assert state.modes == (1000,)
    state.feed(b"\x1bc")
    assert state.active is False
    assert state.modes == ()
    assert state.feed(b"\x1b[?1000h\x1bc") is False
    assert state.feed(b"\x1bc\x1b[?1000h") is True
    assert state.modes == (1000,)


def test_mouse_tracking_modes_are_sorted_and_stable():
    state = MouseTrackingState()
    state.feed(b"\x1b[?1006h")
    state.feed(b"\x1b[?1000h")
    assert state.modes == (1000, 1006)
    state.feed(b"\x1b[?1000l")
    assert state.modes == (1006,)
