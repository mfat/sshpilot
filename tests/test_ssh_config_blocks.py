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
    # Review: these opened already marked as changed.
    "\ufeffHost a\n  User b\n",                                  # 7: a byte-order mark
    "Host a\r\n  User x\n\nHost b\n  Port 1\r\n",               # 8: mixed line endings
    "Host a\n  User x\n# [disabled] Host b\n# [disabled]   Port 1\n"
    "  Compression yes\nHost c\n  Port 2\n",                     # 9: an active line after
    "# [disabled] Host a\n# [disabled]     Port 1\n"
    "# [disabled global section]\n# [disabled] User g\n",        # 10: off block above off global
    '"Match" exec "true"\n  "User" x\nhost lower\n\tuser=y\n',  # 11: quoted, lower-case keywords
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
    assert draft.trailing == ["# tail\n"]


def test_only_an_edited_line_is_rewritten():
    draft = parse(SAMPLES[2])
    web = draft.blocks[1]
    web.options[0].value = "root"
    web.set_note("production web")
    out, _lines = serialize(draft)
    assert "# production web\nHost web *.prod !bad   # note\n" in out
    # The edited line keeps its tab; the untouched one its "=".
    assert "\tUser root # who\n    Port=2222\n" in out
    web.patterns = "web2"
    assert "\nHost web2 # note\n" in serialize(draft)[0]


def test_an_edited_line_keeps_how_it_was_written():
    """Review: editing one value re-indented the line and re-spelled the
    keyword. Indentation, spelling, separator and line ending stay."""
    draft = parse("host a\r\n\tuser=x\r\nHost b\n  Port 1")
    draft.blocks[0].patterns = "z"
    draft.blocks[0].options[0].value = "y"
    draft.blocks[1].options[0].value = "2"
    draft.blocks[1].options.append(Option("User", "u"))
    assert serialize(draft)[0] == "host z\r\n\tuser=y\r\nHost b\n  Port 2\r\n  User u\r\n"


def test_an_active_line_after_switched_off_lines_stays_where_it_is():
    """Review: it was moved up into the block above. The comments are then
    not read as a block at all, so nothing moves."""
    text = SAMPLES[9]
    draft = parse(text)
    assert [(b.patterns, b.disabled) for b in draft.blocks] == [("a", False), ("c", False)]
    assert [o.key for o in draft.blocks[0].options] == ["User", "Compression"]
    assert serialize(draft)[0] == text


def test_switched_off_global_options_are_read_below_a_switched_off_block():
    """Review: the marker was only recognised above every block, so the
    global options became that block's on reload."""
    draft = parse(SAMPLES[10])
    assert [(b.kind, b.disabled, [o.key for o in b.options]) for b in draft.blocks] == [
        ("host", True, ["Port"]), ("global", True, ["User"]),
    ]


def test_quoted_keywords_are_read_as_openssh_reads_them():
    draft = parse(SAMPLES[11])
    assert [(b.kind, b.patterns) for b in draft.blocks] == [
        ("match", 'exec "true"'), ("host", "lower"),
    ]
    assert [b.kind for b in scan_blocks(SAMPLES[11])] == ["match", "host"]
    assert scan_blocks(SAMPLES[11])[0].lines[0].keyword == "user"


def test_global_options_have_no_note():
    """Review: there is no header to put it above, so it attached itself
    to whatever came next."""
    text = "# about everything\nCompression yes\nHost a\n  User x\n"
    draft = parse(text)
    glob = draft.blocks[0]
    assert glob.note == ""
    glob.set_note("ignored")
    assert serialize(draft)[0] == text


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
