"""The block view of an SSH configuration: lossless, and line-accurate."""
import pytest

from sshpilot.ssh_config_blocks import (
    GLOBAL, HOST, MATCH, Block, Option, parse, scan_blocks, serialize,
)

SAMPLES = [
    "",
    "Host a\n    User x\n",
    "# top\nCompression yes\n\n# web\nHost web *.prod !bad   # note\n\tUser deploy # who\n"
    "    Port=2222\n\n\nMatch host x exec \"test -f /a b\"\n  ProxyJump j\n# tail\n",
    "Host a\r\n  User x\r\n\r\nHost b\r\n  Port 1",
    "# [disabled] Host old\n# [disabled]     User o\n# plain comment\nHost new\n    User n\n",
    "# [disabled global section]\n# [disabled] Compression yes\nHost a\n  User b\n",
    "Include conf.d/*\nHost *\n  SendEnv LANG\n",
    "﻿Host a\n  User b\n"[1:],
]


@pytest.mark.parametrize("text", SAMPLES)
def test_unedited_text_round_trips_exactly(text):
    out, _lines = serialize(parse(text))
    assert out == text


def test_blocks_options_and_comments_are_read():
    draft = parse(SAMPLES[2])
    assert [(b.kind, b.patterns) for b in draft.blocks] == [
        (GLOBAL, ""), (HOST, "web *.prod !bad"), (MATCH, 'host x exec "test -f /a b"'),
    ]
    web = draft.blocks[1]
    assert web.comment == "# note" and web.note == "web"
    assert [(o.key, o.value, o.comment) for o in web.options] == [
        ("User", "deploy", "# who"), ("Port", "2222", ""),
    ]
    assert draft.trailing == ["# tail"]


def test_only_an_edited_line_is_rewritten():
    draft = parse(SAMPLES[2])
    web = draft.blocks[1]
    web.options[0].value = "root"
    web.note = "production web"
    out, _lines = serialize(draft)
    assert "# production web\nHost web *.prod !bad   # note\n" in out
    assert "    User root # who\n    Port=2222\n" in out
    web.patterns = "web2"
    assert "\nHost web2 # note\n" in serialize(draft)[0]


def test_line_map_agrees_with_the_daemon_s_scan():
    draft = parse(SAMPLES[2])
    text, lines = serialize(draft)
    scanned = scan_blocks(text)
    assert [lines.headers[b.uid] for b in draft.blocks] == [b.line for b in scanned]
    assert [
        [lines.options[o.uid] for o in b.options] for b in draft.blocks
    ] == [[item.line for item in b.lines] for b in scanned]
    assert [b.kind for b in scanned] == [GLOBAL, HOST, MATCH]


def test_a_switched_off_block_is_comments_and_comes_back():
    draft = parse("Host a\n    User x\n\nHost b\n    Port 1\n")
    draft.blocks[0].disabled = True
    text, _lines = serialize(draft)
    assert text == "# [disabled] Host a\n# [disabled]     User x\n\nHost b\n    Port 1\n"
    assert [b.line for b in scan_blocks(text)] == [4]
    again = parse(text)
    assert [(b.patterns, b.disabled) for b in again.blocks] == [("a", True), ("b", False)]
    again.blocks[0].disabled = False
    assert serialize(again)[0] == "Host a\n    User x\n\nHost b\n    Port 1\n"


def test_new_blocks_and_options_are_written_plainly():
    draft = parse("Host a\n    User x\n")
    draft.blocks.append(Block(kind=MATCH, patterns="", options=[Option("Port", "22")]))
    draft.blocks[0].options.append(Option("ProxyJump", "bastion"))
    draft.blocks[0].options.append(Option("", ""))      # a row still being typed
    assert serialize(draft)[0] == (
        "Host a\n    User x\n    ProxyJump bastion\n\nMatch all\n    Port 22\n"
    )


def test_clone_keeps_identities_for_undo():
    draft = parse("Host a\n    User x\n")
    copy = draft.clone()
    copy.blocks[0].options[0].value = "y"
    assert draft.blocks[0].options[0].value == "x"
    assert copy.blocks[0].uid == draft.blocks[0].uid
    assert copy.find_option(draft.blocks[0].options[0].uid)[1].value == "y"
