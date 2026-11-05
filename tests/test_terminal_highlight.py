"""Network-output highlighter: profiles, chunking, SGR state, pass-through."""

import re

import pytest

from sshpilot.terminal_highlight import PROFILES, Highlighter, apply_config, normalize_profile

SGR = re.compile(rb"\x1b\[(?:36|35|34|33|32|31|39)m")


def colour(text, code):
    return b"\x1b[%dm%s\x1b[39m" % (code, text)


def run(profile, *chunks):
    h = Highlighter(profile)
    return b"".join(h.feed(c) for c in chunks)


def strip(data):
    return SGR.sub(b"", data)


@pytest.mark.parametrize(
    "text, code",
    [
        (b"192.0.2.1", 36),
        (b"192.0.2.0/24", 36),
        (b"255.255.255.0", 36),
        (b"2001:db8::1", 36),
        (b"fe80::1/64", 36),
        (b"2001:db8:0:0:0:0:0:1", 36),
        (b"02:00:5e:10:00:00", 35),
        (b"aa-bb-cc-dd-ee-ff", 35),
        (b"0011.2233.4455", 35),
        (b"001a-2b3c-4d5e", 35),
        (b"vlan 10", 34),
        (b"VLAN 10,20,30-40", 34),
        (b"VLAN0010", 34),
    ],
)
@pytest.mark.parametrize("profile", PROFILES)
def test_generic_tokens_in_every_profile(profile, text, code):
    assert run(profile, b"x " + text + b" y") == b"x " + colour(text, code) + b" y"


@pytest.mark.parametrize(
    "text",
    [b"203.0.113.4.5", b"v203.0.113.4", b"256.1.1.1", b"12:34:56", b"std::string", b"abc192.0.2.1",
     b"aa:bb:cc:dd:ee", b"192.0.2"],
)
def test_false_positives_left_alone(text):
    assert run("generic", text + b"\r\n") == text + b"\r\n"


@pytest.mark.parametrize(
    "profile, text",
    [
        ("cisco-ios", b"GigabitEthernet0/0/1"),
        ("cisco-ios", b"Te1/0/48"),
        ("cisco-ios", b"Po10"),
        ("cisco-ios", b"Vlan100"),
        ("cisco-ios", b"Gi0/1.200"),
        ("cisco-ios", b"Loopback0"),
        ("junos", b"ge-0/0/1"),
        ("junos", b"xe-1/2/3:0.100"),
        ("junos", b"ae0.0"),
        ("junos", b"lo0.0"),
        ("junos", b"irb.100"),
        ("huawei-vrp", b"GigabitEthernet0/0/1"),
        ("huawei-vrp", b"GE0/0/1"),
        ("huawei-vrp", b"Eth-Trunk1"),
        ("huawei-vrp", b"Vlanif10"),
        ("huawei-vrp", b"XGigabitEthernet0/0/1"),
    ],
)
def test_interface_names(profile, text):
    assert run(profile, b"port " + text + b" ok") == b"port " + colour(text, 33) + b" ok"


def test_generic_knows_long_interface_forms_vendors_add_abbreviations():
    # Reworked: the generic profile now carries the shared long forms.
    assert run("generic", b"GigabitEthernet0/1") == colour(b"GigabitEthernet0/1", 33)
    assert run("generic", b"ge-0/0/1") == colour(b"ge-0/0/1", 33)
    assert run("generic", b"Gi0/1") == b"Gi0/1"
    assert run("generic", b"so-0/0/0") == b"so-0/0/0"
    assert run("cisco-ios", b"Gi0/1") == colour(b"Gi0/1", 33)
    assert run("junos", b"so-0/0/0") == colour(b"so-0/0/0", 33)
    assert run("junos", b"Gi0/1") == b"Gi0/1"
    assert run("huawei-vrp", b"GE0/0/1") == colour(b"GE0/0/1", 33)
    assert run("cisco-ios", b"GE0/0/1") == b"GE0/0/1"


@pytest.mark.parametrize(
    "profile, text, code",
    [
        ("cisco-ios", b"up", 32),
        ("cisco-ios", b"down", 31),
        ("cisco-ios", b"err-disabled", 31),
        ("cisco-ios", b"connected", 32),
        ("junos", b"Up", 32),
        ("junos", b"Down", 31),
        ("junos", b"Established", 32),
        ("huawei-vrp", b"UP", 32),
        ("huawei-vrp", b"up(s)", 32),
        ("huawei-vrp", b"*down", 31),
        ("huawei-vrp", b"DOWN", 31),
    ],
)
def test_link_status(profile, text, code):
    assert run(profile, b"is " + text + b",") == b"is " + colour(text, code) + b","


def test_status_words_inside_other_words_are_not_coloured():
    assert run("cisco-ios", b"setup update breakdown up-time") == b"setup update breakdown up-time"


def test_generic_status_needs_a_status_context():
    # Reworked: the generic profile colours status, but only in context.
    assert run("generic", b"line is up") == b"line is " + colour(b"up", 32)
    assert run("generic", b"Link: Down") == b"Link: " + colour(b"Down", 31)
    assert run("generic", b"is administratively down") == b"is " + colour(
        b"administratively down", 31
    )
    assert run("generic", b"up/up") == colour(b"up/up", 32)
    assert run("generic", b"up/down") == colour(b"up/down", 31)
    assert run("generic", b"err-disabled") == colour(b"err-disabled", 31)
    assert run("generic", b"pick it up") == b"pick it up"
    assert run("generic", b"it is up to you") == b"it is up to you"
    assert run("generic", b"Gi0/1 up up") == b"Gi0/1 up up"
    assert run("cisco-ios", b"x up up") == b"x " + colour(b"up", 32) + b" " + colour(b"up", 32)


def test_generic_errors():
    for line in (b"% Invalid input", b"Error: bad", b"syntax error, x", b"unknown command"):
        assert run("generic", line) == colour(line, 31)


@pytest.mark.parametrize(
    "profile, line",
    [
        ("cisco-ios", b"% Invalid input detected at '^' marker."),
        ("cisco-ios", b"% Incomplete command."),
        ("junos", b"syntax error, expecting <command>."),
        ("junos", b"error: device busy"),
        ("huawei-vrp", b"Error: Unrecognized command found at '^' position."),
    ],
)
def test_error_lines(profile, line):
    assert run(profile, line + b"\r\n") == colour(line, 31) + b"\r\n"


@pytest.mark.parametrize(
    "profile, text",
    [("cisco-ios", b"Label 16001"), ("junos", b"push 299792"), ("huawei-vrp", b"In-Label: 1024")],
)
def test_mpls_labels(profile, text):
    out = run(profile, b"x " + text + b" y")
    assert strip(out) == b"x " + text + b" y"
    assert b"\x1b[34m" in out


def test_generic_has_mpls_labels_but_push_swap_are_junos_only():
    assert run("generic", b"label 16001") == colour(b"label 16001", 34)
    assert run("generic", b"push 299792") == b"push 299792"
    assert run("junos", b"push 299792") == colour(b"push 299792", 34)


def test_visible_text_unchanged_for_full_session():
    sample = (
        b"R1#show ip int brief\r\nInterface  IP-Address  Status Protocol\r\n"
        b"Gi0/0  198.51.100.1 up up\r\nGi0/1 unassigned administratively down down\r\n"
        b"MAC 0011.2233.4455 vlan 10 fe80::1\r\n% Invalid input detected\r\n"
    )
    for profile in PROFILES:
        assert strip(run(profile, sample)) == sample


def test_multiple_tokens_one_chunk():
    out = run("generic", b"192.0.2.1 -> 02:00:5e:10:00:00")
    assert out == colour(b"192.0.2.1", 36) + b" -> " + colour(b"02:00:5e:10:00:00", 35)


# --- chunking -------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_every_split_point_preserves_text_and_never_holds_text(profile):
    data = (
        b"\x1b[1mhdr\x1b[0m 198.51.100.1/24 02:00:5e:10:00:00 vlan 10 Gi0/1 up\r\n"
        b"\x1b]0;title\x07% Invalid input\r\n\x1b[31mred 192.0.2.2\x1b[0m"
    )
    for cut in range(len(data) + 1):
        h = Highlighter(profile)
        first = h.feed(data[:cut])
        second = h.feed(data[cut:])
        assert strip(first + second) == strip(data)
        # Only an unfinished escape is held back, never text.
        sent = re.sub(rb"\x1b(?:\[[0-?]*[ -/]*)?\Z", b"", data[:cut])
        assert strip(first) == strip(sent)


def test_escape_split_across_chunks_is_passed_intact():
    h = Highlighter("generic")
    assert h.feed(b"ab\x1b[") == b"ab"
    assert h.feed(b"31mred 192.0.2.1") == b"\x1b[31mred 192.0.2.1"


def test_split_sgr_still_blocks_highlighting():
    assert run("generic", b"\x1b[3", b"1m192.0.2.1") == b"\x1b[31m192.0.2.1"


def test_lone_trailing_esc_is_held_then_released():
    h = Highlighter("generic")
    assert h.feed(b"x\x1b") == b"x"
    assert h.feed(b"[0m") == b"\x1b[0m"


def test_malformed_escape_does_not_stall():
    assert run("generic", b"\x1b[3\nfoo 192.0.2.1")[-12:] == colour(b"192.0.2.1", 36)[-12:]


def test_token_split_across_chunks_colours_the_continuation():
    out = run("generic", b"ip 192.0.2.", b"1 up")
    assert out == b"ip 192.0.2." + colour(b"1", 36) + b" up"


def test_token_split_before_any_match_is_possible():
    out = run("generic", b"02:00:5e:", b"10:00:00")
    assert strip(out) == b"02:00:5e:10:00:00"
    assert out.endswith(colour(b"10:00:00", 35))


def test_prompt_without_newline_is_emitted_immediately():
    h = Highlighter("cisco-ios")
    assert h.feed(b"R1(config-if)#") == b"R1(config-if)#"
    assert h.feed(b"ip address 192.0.2.1") == b"ip address " + colour(b"192.0.2.1", 36)


def test_carry_resets_at_whitespace():
    out = run("generic", b"foo", b" 192.0.2.1")
    assert out == b"foo " + colour(b"192.0.2.1", 36)


def test_osc_string_split_across_chunks_is_untouched():
    h = Highlighter("generic")
    out = h.feed(b"\x1b]52;c;") + h.feed(b"192.0.2.1 02:00:5e:10:00:00") + h.feed(b"\x07 192.0.2.2")
    assert out == b"\x1b]52;c;192.0.2.1 02:00:5e:10:00:00\x07 " + colour(b"192.0.2.2", 36)


def test_osc_terminated_by_st_split_between_esc_and_backslash():
    h = Highlighter("generic")
    out = h.feed(b"\x1b]0;192.0.2.1\x1b") + h.feed(b"\\ 192.0.2.2")
    assert out == b"\x1b]0;192.0.2.1\x1b\\ " + colour(b"192.0.2.2", 36)


# --- remote SGR -----------------------------------------------------------


@pytest.mark.parametrize(
    "sgr", [b"\x1b[31m", b"\x1b[92m", b"\x1b[41m", b"\x1b[38;5;196m", b"\x1b[38;2;1;2;3m",
            b"\x1b[48;5;1m", b"\x1b[38:2::1:2:3m", b"\x1b[7m", b"\x1b[8m", b"\x1b[1;32m"]
)
def test_remote_colour_is_not_clobbered(sgr):
    data = sgr + b"192.0.2.1 up"
    assert run("cisco-ios", data) == data


@pytest.mark.parametrize(
    "reset", [b"\x1b[0m", b"\x1b[m", b"\x1b[39m", b"\x1b[49m", b"\x1b[27m", b"\x1b[28m"]
)
def test_highlighting_resumes_after_remote_resets(reset):
    # Each reset only matters for the attribute it undoes.
    setter = {b"\x1b[39m": b"\x1b[31m", b"\x1b[49m": b"\x1b[41m", b"\x1b[27m": b"\x1b[7m",
              b"\x1b[28m": b"\x1b[8m"}.get(reset, b"\x1b[31m")
    out = run("generic", setter + b"x" + reset + b"192.0.2.1")
    assert out.endswith(colour(b"192.0.2.1", 36))


def test_bold_and_underline_do_not_block_highlighting():
    out = run("generic", b"\x1b[1;4m192.0.2.1")
    assert out == b"\x1b[1;4m" + colour(b"192.0.2.1", 36)


def test_remote_colour_survives_across_chunks_and_lines():
    h = Highlighter("generic")
    h.feed(b"\x1b[33m")
    assert h.feed(b"192.0.2.1\r\n192.0.2.2") == b"192.0.2.1\r\n192.0.2.2"


def test_ris_and_decstr_reset_state():
    assert run("generic", b"\x1b[31m\x1bc192.0.2.1") == b"\x1b[31m\x1bc" + colour(b"192.0.2.1", 36)
    assert run("generic", b"\x1b[31m\x1b[!p192.0.2.1").endswith(colour(b"192.0.2.1", 36))


def test_injected_colour_restores_default_foreground_only():
    out = run("generic", b"\x1b[1m192.0.2.1")
    assert b"\x1b[39m" in out and b"\x1b[0m" not in out


# --- alt screen -----------------------------------------------------------


@pytest.mark.parametrize("mode", [b"1049", b"47", b"1047", b"25;1049"])
def test_alt_screen_is_skipped_then_resumes(mode):
    h = Highlighter("generic")
    assert h.feed(b"\x1b[?" + mode + b"h192.0.2.1") == b"\x1b[?" + mode + b"h192.0.2.1"
    assert h.feed(b"\x1b[?" + mode + b"l192.0.2.1") == b"\x1b[?" + mode + b"l" + colour(
        b"192.0.2.1", 36
    )


def test_non_alt_private_modes_do_not_disable():
    assert run("generic", b"\x1b[?25l192.0.2.1").endswith(colour(b"192.0.2.1", 36))


def test_alt_screen_mode_split_across_chunks():
    h = Highlighter("generic")
    h.feed(b"\x1b[?10")
    h.feed(b"49h")
    assert h.feed(b"192.0.2.1") == b"192.0.2.1"


# --- passthrough / settings ----------------------------------------------


def test_bytes_without_esc_or_tokens_pass_through_exactly():
    data = bytes(b for b in range(256) if b != 0x1B) * 4
    assert run("cisco-ios", data) == data


def test_utf8_split_mid_character():
    text = "café 192.0.2.1".encode()
    out = run("generic", text[:4], text[4:])
    assert strip(out) == text


def test_empty_feed():
    assert Highlighter().feed(b"") == b""


def test_profile_fallback():
    assert normalize_profile("junos") == "junos"
    assert normalize_profile("bogus") == "generic"
    assert normalize_profile(None) == "generic"


def test_full_size_chunk_keeps_visible_text():
    line = b"Gi0/1 is up, 192.0.2.1 02:00:5e:10:00:00 vlan 10\r\n"
    data = line * (32768 // len(line))
    out = run("cisco-ios", data)
    assert strip(out) == data


# --- TerminalWidget hook ---------------------------------------------------


def _widget(highlighter):
    import types

    from sshpilot.terminal import TerminalWidget

    fed = []
    widget = types.SimpleNamespace(
        backend=types.SimpleNamespace(feed=fed.append),
        _highlighter=highlighter,
        _mouse_tracking=None,
    )
    return TerminalWidget._paint_display.__get__(widget), fed


def test_paint_display_feeds_highlighted_bytes():
    paint, fed = _widget(Highlighter("generic"))
    paint(b"host 192.0.2.1")
    assert fed == [b"host " + colour(b"192.0.2.1", 36)]


def test_paint_display_disabled_is_byte_identical():
    paint, fed = _widget(Highlighter("generic", enabled=False))
    paint(b"host 192.0.2.1")
    assert fed == [b"host 192.0.2.1"]


# --- regression: control strings, C1, state, validation ---------------------

PLAIN_IP = colour(b"192.0.2.2", 36)


@pytest.mark.parametrize("intro", [b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_"])
def test_bel_does_not_end_dcs_sos_pm_apc(intro):
    data = intro + b"q 192.0.2.1\x07 192.0.2.1 02:00:5e:10:00:00"
    h = Highlighter("generic")
    assert h.feed(data) == data
    assert h.feed(b"\x1b\\ 192.0.2.2") == b"\x1b\\ " + PLAIN_IP


def test_bel_ends_osc_only():
    assert run("generic", b"\x1b]0;192.0.2.1\x07 192.0.2.2") == b"\x1b]0;192.0.2.1\x07 " + PLAIN_IP


@pytest.mark.parametrize(
    "chunks",
    [
        [b"\xc2\x9d0;192.0.2.1\xc2\x9c 192.0.2.2"],
        [b"\xc2\x9d0;192.0.2.1\xc2", b"\x9c 192.0.2.2"],
        [b"\xc2", b"\x9d0;192.0.2.1\xc2\x9c 192.0.2.2"],
        [b"\xc2\x90q192.0.2.1\x07\xc2\x9c 192.0.2.2"],
    ],
)
def test_c1_strings_are_passed_through_untouched(chunks):
    out = run("generic", *chunks)
    assert out.startswith(b"".join(chunks)[:-len(b" 192.0.2.2")])
    assert out.endswith(b" " + PLAIN_IP)
    assert out.count(b"\x1b[36m") == 1


def test_c1_csi_sgr_is_tracked_and_not_clobbered():
    assert run("generic", b"\xc2\x9b31m192.0.2.1") == b"\xc2\x9b31m192.0.2.1"
    assert run("generic", b"\xc2\x9b31m", b"192.0.2.1") == b"\xc2\x9b31m192.0.2.1"
    assert run("generic", b"\xc2", b"\x9b31m192.0.2.1") == b"\xc2\x9b31m192.0.2.1"


def test_c1_alt_screen_and_raw_8bit_bytes():
    assert run("generic", b"\xc2\x9b?1049h192.0.2.1") == b"\xc2\x9b?1049h192.0.2.1"
    # A raw 0x9b is a UTF-8 continuation byte (here "ě"), not CSI.
    assert run("generic", "ě 192.0.2.1".encode()) == "ě ".encode() + colour(b"192.0.2.1", 36)


def test_disable_flushes_held_partial_escape_exactly():
    h = Highlighter("generic")
    assert h.feed(b"a\x1b[3") == b"a"
    assert h.configure(False, "generic") == b"\x1b[3"
    # The rest of that sequence now arrives while disabled: byte-identical.
    assert h.feed(b"1m192.0.2.1") == b"1m192.0.2.1"
    assert h.configure(True, "generic") == b""
    # State survived the round trip: the remote's red is still in force.
    assert h.feed(b"192.0.2.1") == b"192.0.2.1"


def test_disabled_feed_is_byte_identical_for_every_split():
    data = b"\x1b[1mhdr\x1b[0m 192.0.2.1 \x1b]0;t\x07 02:00:5e:10:00:00\r\n\x1b[?1049h"
    for cut in range(len(data) + 1):
        h = Highlighter("cisco-ios", enabled=False)
        assert h.feed(data[:cut]) + h.feed(data[cut:]) == data


def test_state_survives_toggle_and_profile_change():
    h = Highlighter("generic")
    h.feed(b"\x1b[?1049h")
    assert h.configure(False, "generic") == b""
    assert h.configure(True, "cisco-ios") == b""
    assert h.profile == "cisco-ios"
    assert h.feed(b"192.0.2.1") == b"192.0.2.1"  # still on the alt screen
    h.feed(b"\x1b[?1049l")
    assert h.feed(b"192.0.2.1") == colour(b"192.0.2.1", 36)


def test_state_is_tracked_while_disabled():
    h = Highlighter("generic", enabled=False)
    h.feed(b"\x1b[?1049h\x1b]0;unterminated")
    h.configure(True, "generic")
    assert h.feed(b"192.0.2.1 ") == b"192.0.2.1 "  # inside the OSC string
    assert h.feed(b"\x07\x1b[?1049l192.0.2.1") == b"\x07\x1b[?1049l" + colour(b"192.0.2.1", 36)


def test_reset_clears_hold_string_and_alt_screen():
    h = Highlighter("generic")
    h.feed(b"\x1b[?1049h\x1b]0;x\x1b")
    h.reset()
    assert h.feed(b"192.0.2.1") == colour(b"192.0.2.1", 36)
    h.feed(b"x\x1b[3")
    h.reset()
    assert h.feed(b"1m 192.0.2.1") == b"1m " + colour(b"192.0.2.1", 36)


def test_reset_keeps_settings():
    h = Highlighter("junos")
    h.reset()
    assert (h.enabled, h.profile) == (True, "junos")


class _Config:
    def __init__(self, **values):
        self.values = values

    def get_setting(self, key, default=None):
        return self.values.get(key, default)


def test_apply_config():
    h = Highlighter(enabled=False)
    apply_config(h, _Config(**{"terminal.highlight_enabled": True,
                               "terminal.highlight_profile": "huawei-vrp"}))
    assert (h.enabled, h.profile) == (True, "huawei-vrp")
    apply_config(h, _Config(**{"terminal.highlight_enabled": "yes",
                               "terminal.highlight_profile": "nope"}))
    assert (h.enabled, h.profile) == (False, "generic")


@pytest.mark.parametrize(
    "text",
    [b"::ffff:203.0.113.4", b"1::", b"2001:db8::1/128", b"fe80::1/64", b"dead:beef::1"],
)
def test_ipv6_valid_forms(text):
    assert run("generic", b"a " + text + b" b") == b"a " + colour(text, 36) + b" b"


@pytest.mark.parametrize(
    "text",
    [b"1:2:3:4:5:6:7:8:9", b"2001:db8::1/129", b"1:2:3", b"12:34:56", b"::g", b":::",
     b"1::2::3", b"2001:db8:0:0:0:0:0:0:1", b"::ffff:1.2.3.999"],
)
def test_ipv6_invalid_or_overlong_forms_are_plain(text):
    assert run("generic", b"a " + text + b" b") == b"a " + text + b" b"


@pytest.mark.parametrize(
    "line",
    [b"version 203.0.113.4", b"Version: 203.0.113.4", b"ver 192.0.2.1", b"Release 198.51.100.9"],
)
def test_version_like_ipv4_is_plain(line):
    assert run("cisco-ios", line) == line


def test_ipv4_prefix_length_is_range_checked():
    assert run("generic", b"192.0.2.1/32") == colour(b"192.0.2.1/32", 36)
    assert run("generic", b"192.0.2.1/33") == colour(b"192.0.2.1", 36) + b"/33"


@pytest.mark.parametrize(
    "text, ok",
    [(b"vlan 1", True), (b"vlan 4094", True), (b"vlan 4095", False), (b"vlan 0", False),
     (b"vlan 10,20,4094", True), (b"vlan 10,5000", False), (b"VLAN4094", True),
     (b"VLAN4095", False), (b"vlan-id 99", True)],
)
def test_vlan_range(text, ok):
    assert run("generic", text) == (colour(text, 34) if ok else text)


@pytest.mark.parametrize(
    "text, ok",
    [(b"label 1048575", True), (b"label 1048576", False), (b"Label: 16", True),
     (b"label 9999999", False)],
)
def test_mpls_label_range(text, ok):
    assert run("generic", text) == (colour(text, 34) if ok else text)


@pytest.mark.parametrize(
    "text",
    [b"aa:bb-cc:dd-ee:ff", b"aa-bb:cc-dd:ee-ff", b"aabb.ccdd-eeff", b"aabb-ccdd.eeff"],
)
def test_mixed_mac_separators_are_plain(text):
    assert run("generic", text) == text


def test_mac_is_not_taken_from_inside_an_eight_group_ipv6():
    text = b"02:00:5e:10:00:00:00:11"
    assert run("generic", text) == colour(text, 36)


@pytest.mark.parametrize(
    "setup, still_off",
    [
        ([b"\x1b[?1049h", b"\x1b[?47h"], b"\x1b[?1049l"),
        ([b"\x1b[?47h", b"\x1b[?1047h"], b"\x1b[?47l"),
        ([b"\x1b[?1047;1049h"], b"\x1b[?1047l"),
    ],
)
def test_alt_screen_modes_are_a_set(setup, still_off):
    h = Highlighter("generic")
    for chunk in setup:
        h.feed(chunk)
    h.feed(still_off)
    assert h.feed(b"192.0.2.1") == b"192.0.2.1"
    for off in (b"\x1b[?47l", b"\x1b[?1047l", b"\x1b[?1049l"):
        h.feed(off)
    assert h.feed(b"192.0.2.1") == colour(b"192.0.2.1", 36)


# --- widget lifecycle ---------------------------------------------------------


def _lifecycle_widget(config=None):
    import types

    fed = []
    return types.SimpleNamespace(
        backend=types.SimpleNamespace(feed=fed.append, reset=lambda *a: None),
        _highlighter=Highlighter("generic"),
        _mouse_tracking=None,
        config=config,
    ), fed


def test_widget_continuity_loss_resets_highlighter_before_marker():
    from sshpilot.terminal import TerminalWidget

    widget, fed = _lifecycle_widget()
    widget._feed_display = lambda data: TerminalWidget._paint_display(widget, data)
    widget._highlighter.feed(b"\x1b[?1049h\x1b[3")
    TerminalWidget._on_daemon_continuity_lost(widget)
    marker = b"\r\n[Earlier terminal output is no longer available]\r\n"
    assert fed == [marker]
    assert widget._highlighter.feed(b"192.0.2.1") == colour(b"192.0.2.1", 36)


def test_widget_uninstall_daemon_io_resets_highlighter():
    from sshpilot.terminal import TerminalWidget

    widget, _ = _lifecycle_widget()
    widget._uninstall_selection_feed_pause = lambda: None
    widget._highlighter.feed(b"\x1b[?1049h")
    TerminalWidget._uninstall_daemon_backend_io(widget)
    assert widget._highlighter.feed(b"192.0.2.1") == colour(b"192.0.2.1", 36)


def test_widget_disable_flushes_held_bytes_to_backend():
    from sshpilot.terminal import TerminalWidget

    config = _Config(**{"terminal.highlight_enabled": True})
    widget, fed = _lifecycle_widget(config)
    widget._highlighter.feed(b"x\x1b[3")
    config.values["terminal.highlight_enabled"] = False
    TerminalWidget._apply_highlight_settings(widget)
    assert fed == [b"\x1b[3"]


# --- regression: colon sub-parameters, string aborts, partials -----------------


def test_colon_truecolour_sets_fg_and_does_not_leak_subparameters():
    # 38:2::R:G:B has an empty colorspace slot; none of R/G/B=0 may reset fg.
    data = b"\x1b[38:2::255:128:0m192.0.2.1"
    assert run("generic", data) == data
    data = b"\x1b[38:2::0:0:0m192.0.2.1"
    assert run("generic", data) == data


def test_colon_background_and_underline_colour():
    assert run("generic", b"\x1b[48:2::1:2:3m192.0.2.1") == b"\x1b[48:2::1:2:3m192.0.2.1"
    # 58 (underline colour) never blocks highlighting, in either form.
    for sgr in (b"\x1b[58:2::1:2:3m", b"\x1b[58;2;1;2;3m", b"\x1b[58;5;1m"):
        assert run("generic", sgr + b"192.0.2.1") == sgr + colour(b"192.0.2.1", 36)


def test_colon_subparameter_of_another_code_does_not_reset():
    data = b"\x1b[31;4:0m192.0.2.1"
    assert run("generic", data) == data
    assert run("generic", b"\x1b[4:3m192.0.2.1") == b"\x1b[4:3m" + colour(b"192.0.2.1", 36)


def test_semicolon_extended_colours_still_consume_their_arguments():
    # 38;5;0 must not let the trailing 0 reset; a following 1 is bold, not colour.
    assert run("generic", b"\x1b[38;5;0m192.0.2.1") == b"\x1b[38;5;0m192.0.2.1"
    assert run("generic", b"\x1b[38;2;0;0;0m192.0.2.1") == b"\x1b[38;2;0;0;0m192.0.2.1"
    out = run("generic", b"\x1b[48;5;0;1m192.0.2.1")
    assert out == b"\x1b[48;5;0;1m192.0.2.1"


@pytest.mark.parametrize("cancel", [b"\x18", b"\x1a"])
@pytest.mark.parametrize("intro", [b"\x1b]0;", b"\x1bP", b"\x1b_"])
def test_can_and_sub_end_control_strings(intro, cancel):
    assert run("generic", intro + b"x" + cancel + b"192.0.2.1") == (
        intro + b"x" + cancel + colour(b"192.0.2.1", 36)
    )


def test_new_esc_ends_control_string_without_being_consumed():
    out = run("generic", b"\x1b]0;x\x1b[31m192.0.2.1")
    assert out == b"\x1b]0;x\x1b[31m192.0.2.1"  # the CSI was parsed: red is in force
    out = run("generic", b"\x1b]0;x\x1b[0m192.0.2.1")
    assert out == b"\x1b]0;x\x1b[0m" + colour(b"192.0.2.1", 36)


def test_esc_split_inside_string_terminator_is_still_st():
    assert run("generic", b"\x1bPq\x1b", b"\\192.0.2.1") == b"\x1bPq\x1b\\" + colour(
        b"192.0.2.1", 36
    )


def test_esc_with_intermediate_is_held_until_final():
    h = Highlighter("generic")
    assert h.feed(b"a\x1b(") == b"a"
    assert h.feed(b"B192.0.2.1") == b"\x1b(B" + colour(b"192.0.2.1", 36)


@pytest.mark.parametrize("text, ok", [
    (b"implicit-null", True), (b"explicit-null", True),
    (b"nonimplicit-null", False), (b"implicit-nullable", False), (b"x-implicit-null", False),
])
def test_null_labels_need_word_boundaries(text, ok):
    assert run("generic", text) == (colour(text, 34) if ok else text)


def test_overlong_trailing_token_is_not_carried():
    h = Highlighter("generic")
    h.feed(b"x" * 100 + b"192.0.2.")
    assert h.feed(b"1") == b"1"  # carry dropped: no half-token continuation
    h.feed(b"ip 192.0.2.")
    assert h.feed(b"1") == colour(b"1", 36)


def test_disabled_esc_free_chunk_returns_same_object_without_regex(monkeypatch):
    import sshpilot.terminal_highlight as th

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("regex used for an ESC-free chunk while disabled")

    h = Highlighter(enabled=False)
    for name in ("_NEXT_RE", "_CSI_RE", "_ESC_RE", "_PARTIAL_ESC_RE", "_TAIL_RE"):
        monkeypatch.setattr(th, name, Boom())
    data = b"Gi0/1 192.0.2.1 is up\r\n"
    assert h.feed(data) is data


def test_disabled_still_tracks_alt_screen_across_esc_chunks():
    h = Highlighter(enabled=False)
    assert h.feed(b"\x1b[?1049h") == b"\x1b[?1049h"
    assert h.feed(b"plain") == b"plain"
    h.configure(True, "generic")
    assert h.feed(b"192.0.2.1") == b"192.0.2.1"  # still on the alternate screen


def test_widget_highlighter_is_constructed_once_per_widget(monkeypatch):
    import sshpilot.terminal as terminal_mod

    built = []
    real = terminal_mod.Highlighter
    monkeypatch.setattr(
        terminal_mod, "Highlighter", lambda *a, **k: built.append(1) or real(*a, **k)
    )
    w = terminal_mod.TerminalWidget.__new__(terminal_mod.TerminalWidget)
    for _ in range(5):
        w._highlighter
    assert len(built) == 1


def test_highlight_module_is_preloaded():
    from sshpilot import startup_preload

    assert "sshpilot.terminal_highlight" in startup_preload.MODULES


def test_enabling_mid_escape_keeps_the_pending_sequence():
    h = Highlighter(enabled=False)
    assert h.feed(b"\x1b") == b"\x1b"
    assert h.configure(True, "generic") == b""
    # The ESC is already on screen: only the rest is emitted, red stays in force.
    assert h.feed(b"[31m 192.0.2.1") == b"[31m 192.0.2.1"


def test_enabling_mid_string_keeps_the_pending_string():
    h = Highlighter(enabled=False)
    assert h.feed(b"\x1b]0;ti") == b"\x1b]0;ti"
    h.configure(True, "generic")
    assert h.feed(b"192.0.2.1\x07 192.0.2.2") == b"192.0.2.1\x07 " + colour(b"192.0.2.2", 36)


def test_enabling_mid_escape_still_unfinished_never_replays():
    h = Highlighter(enabled=False)
    h.feed(b"\x1b")
    h.configure(True, "generic")
    assert h.feed(b"[") == b""
    assert h.feed(b"31m 192.0.2.1") == b"[31m 192.0.2.1"


def test_disabling_after_that_flushes_only_unshown_bytes():
    h = Highlighter(enabled=False)
    h.feed(b"\x1b")
    h.configure(True, "generic")
    h.feed(b"[")
    assert h.configure(False, "generic") == b"["


def test_disabled_chunk_clears_the_carry():
    h = Highlighter()
    assert h.feed(b"192.0.2.") == b"192.0.2."
    h.configure(False, "generic")
    assert h.feed(b"break ") == b"break "
    h.configure(True, "generic")
    assert h.feed(b"1") == b"1"


def test_version_guard_works_across_chunks():
    h = Highlighter()
    assert h.feed(b"version ") == b"version "
    assert h.feed(b"203.0.113.4") == b"203.0.113.4"
    h.reset()
    assert h.feed(b"host ") == b"host "
    assert h.feed(b"203.0.113.4") == colour(b"203.0.113.4", 36)


def test_apply_highlight_settings_logs_state_at_debug(caplog):
    import logging

    from sshpilot.terminal import TerminalWidget

    w = TerminalWidget.__new__(TerminalWidget)
    w.config = _Config(**{"terminal.highlight_enabled": True, "terminal.highlight_profile": "junos"})
    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal"):
        TerminalWidget._apply_highlight_settings(w)
    assert "Output highlighting enabled=True profile=junos" in caplog.text
