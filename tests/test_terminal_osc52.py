"""OSC 52 clipboard-write detection (GH #1297)."""

import base64

import pytest

from sshpilot.terminal_osc52 import (
    OSC52_DEFAULT_MAX_KIB,
    OSC52_DEFAULT_POLICY,
    OSC52_MAX_MAX_KIB,
    OSC52_MIN_MAX_KIB,
    TARGET_CLIPBOARD,
    TARGET_PRIMARY,
    ClipboardWrite,
    Osc52Scanner,
    normalize_osc52_max_kib,
    normalize_osc52_policy,
    selection_targets,
)


def b64(text):
    return base64.b64encode(text.encode()).decode()


def osc52(text, selection="c", terminator="\a"):
    return f"\x1b]52;{selection};{b64(text)}{terminator}".encode()


def texts(writes):
    return [w.text for w in writes]


def test_bel_terminated_write_is_reported():
    writes = Osc52Scanner().feed(osc52("OSC52-TEST"))
    assert writes == [
        ClipboardWrite(targets=frozenset({TARGET_CLIPBOARD}), text="OSC52-TEST")
    ]


def test_st_terminated_write_is_reported():
    writes = Osc52Scanner().feed(osc52("OSC52-ST-TEST", terminator="\x1b\\"))
    assert texts(writes) == ["OSC52-ST-TEST"]


def test_write_surrounded_by_other_output():
    data = b"prompt$ " + osc52("hello") + b"\r\nprompt$ "
    assert texts(Osc52Scanner().feed(data)) == ["hello"]


def test_utf8_payload_is_decoded():
    assert texts(Osc52Scanner().feed(osc52("héllo ✓"))) == ["héllo ✓"]


def test_str_input_from_pty_bridge_is_accepted():
    assert texts(Osc52Scanner().feed(osc52("from str").decode())) == ["from str"]


@pytest.mark.parametrize("split", range(1, 20))
def test_sequence_split_across_chunks(split):
    data = b"ab" + osc52("split-me", terminator="\x1b\\") + b"cd"
    scanner = Osc52Scanner()
    writes = scanner.feed(data[:split]) + scanner.feed(data[split:])
    assert texts(writes) == ["split-me"]


def test_sequence_fed_one_byte_at_a_time():
    scanner = Osc52Scanner()
    writes = []
    for byte in osc52("slow", terminator="\x1b\\"):
        writes += scanner.feed(bytes([byte]))
    assert texts(writes) == ["slow"]


def test_clipboard_read_is_ignored():
    assert Osc52Scanner().feed(b"\x1b]52;c;?\a") == []


def test_empty_payload_does_not_clear_clipboard():
    assert Osc52Scanner().feed(b"\x1b]52;c;\a") == []


def test_invalid_base64_is_ignored():
    assert Osc52Scanner().feed(b"\x1b]52;c;not base64!!\a") == []
    assert Osc52Scanner().feed(b"\x1b]52;c;abc\a") == []  # bad padding


def test_wrapped_base64_is_accepted():
    payload = base64.encodebytes(("x" * 100).encode())  # newline every 76
    assert b"\n" in payload
    data = b"\x1b]52;c;" + payload + b"\a"
    assert texts(Osc52Scanner().feed(data)) == ["x" * 100]


def test_other_osc_sequences_are_ignored():
    assert Osc52Scanner().feed(b"\x1b]0;title\a\x1b]521;c;aGk=\a") == []


def test_can_cancels_sequence():
    scanner = Osc52Scanner()
    assert scanner.feed(b"\x1b]52;c;aGk=\x18more") == []
    assert texts(scanner.feed(osc52("after"))) == ["after"]


def test_esc_inside_payload_aborts_and_rescans():
    data = b"\x1b]52;c;aGk=" + osc52("next")
    assert texts(Osc52Scanner().feed(data)) == ["next"]


def test_c1_bytes_in_utf8_text_do_not_trigger():
    # U+275D is E2 9D 9D: two 0x9D bytes, which are 8-bit OSC in C1.
    data = "❝52;c;aGk=❧".encode() + b"\x07"
    assert Osc52Scanner().feed(data) == []


@pytest.mark.parametrize(
    "selection, targets",
    [
        ("c", {TARGET_CLIPBOARD}),
        ("", {TARGET_CLIPBOARD}),
        ("s", {TARGET_CLIPBOARD}),
        ("p", {TARGET_PRIMARY}),
        ("pc", {TARGET_CLIPBOARD, TARGET_PRIMARY}),
    ],
)
def test_selection_parameter(selection, targets):
    writes = Osc52Scanner().feed(osc52("x", selection=selection))
    assert [w.targets for w in writes] == [frozenset(targets)]


def test_cut_buffer_only_selection_is_ignored():
    assert selection_targets("0") == frozenset()
    assert Osc52Scanner().feed(osc52("x", selection="0")) == []


def test_invalid_selection_is_not_a_clipboard_write():
    assert Osc52Scanner().feed(b"\x1b]52;x;aGk=\a") == []


def test_oversized_payload_is_dropped():
    scanner = Osc52Scanner(max_bytes=64)
    assert scanner.feed(osc52("y" * 100)) == []
    # The scanner recovers for the next copy.
    assert texts(scanner.feed(b"$ " + osc52("small"))) == ["small"]


def test_oversized_payload_split_across_chunks_is_dropped():
    scanner = Osc52Scanner(max_bytes=64)
    data = osc52("y" * 100)
    assert scanner.feed(data[:40]) + scanner.feed(data[40:]) == []


def test_back_to_back_chunks_join_into_one_copy():
    # The osc52 PyPI tool splits one base64 string into 32 KiB slices.
    text = "chunked copy " * 5000
    encoded = base64.b64encode(text.encode())
    size = 32 * 1024
    data = b"".join(
        b"\x1b]52;c;" + encoded[i:i + size] + b"\x07"
        for i in range(0, len(encoded), size)
    )
    writes = Osc52Scanner().feed(data)
    assert len(writes) == 3
    assert writes[-1].text == text
    assert writes[-1].parts == 3


def test_back_to_back_chunks_join_across_feed_boundaries():
    scanner = Osc52Scanner()
    first = b"\x1b]52;c;" + base64.b64encode(b"abc") + b"\x07"
    second = b"\x1b]52;c;" + base64.b64encode(b"def") + b"\x07"
    writes = scanner.feed(first + second[:3])
    writes += scanner.feed(second[3:])
    assert texts(writes) == ["abc", "abcdef"]


def test_output_between_copies_starts_a_new_copy():
    data = osc52("first") + b"\r\n$ " + osc52("second")
    assert texts(Osc52Scanner().feed(data)) == ["first", "second"]


def test_different_selection_starts_a_new_copy():
    data = osc52("clip", selection="c") + osc52("prim", selection="p")
    writes = Osc52Scanner().feed(data)
    assert texts(writes) == ["clip", "prim"]
    assert writes[1].targets == frozenset({TARGET_PRIMARY})


def test_joined_run_over_limit_is_dropped_entirely():
    scanner = Osc52Scanner(max_bytes=40)
    chunk = b"\x1b]52;c;" + base64.b64encode(b"x" * 24) + b"\x07"  # 32 b64
    writes = scanner.feed(chunk + chunk + chunk)
    # The first chunk fits; the run then overflows and its tail is not
    # copied on its own as if it were the whole text.
    assert texts(writes) == ["x" * 24]
    assert texts(scanner.feed(b"$ " + osc52("ok"))) == ["ok"]


def test_policy_and_limit_normalization():
    assert OSC52_DEFAULT_POLICY == "ask"
    assert normalize_osc52_policy("Always") == "always"
    assert normalize_osc52_policy("never") == "never"
    assert normalize_osc52_policy(None) == "ask"
    assert normalize_osc52_policy("bogus") == "ask"
    assert normalize_osc52_max_kib(None) == OSC52_DEFAULT_MAX_KIB
    assert normalize_osc52_max_kib(1) == OSC52_MIN_MAX_KIB
    assert normalize_osc52_max_kib(10**9) == OSC52_MAX_MAX_KIB
    assert normalize_osc52_max_kib("256") == 256
