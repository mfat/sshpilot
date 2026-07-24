"""SSHConfigDocument: lossless parse/serialize and Host-block addressing."""

import pytest

from sshpilot.ssh_config_document import (
    HostBlock,
    RawSpan,
    SSHConfigDocument,
    split_host_tokens,
)

FIXTURE = (
    "# Global header - do not touch\n"
    "Include fragments/*\n"
    "\n"
    "Host web\n"
    "    # pinned comment\n"
    "    HostName example.com\n"
    "\n"
    "Host db jump\n"
    "\tHostName=db.internal\n"
    "    UnknownCamelCase FooBar\n"
    "\n"
    "Match host *.internal\n"
    "    User matchuser\n"
    "\n"
    "Host \"two words\" plain\n"
    "    HostName spaced.example.com\n"
    "\n"
    "Host\ttail\n"
    "    HostName tail.example.com"  # deliberately no trailing newline
)


@pytest.mark.parametrize("text", [
    FIXTURE,
    "",                                   # empty file
    "# only a comment\n",
    "Host lone\n",                        # header with no body, no newline issues
    "    Host indented\n    HostName x\n",  # indented header
    'Host "unbalanced\n    HostName x\n',   # malformed quoting (fallback split)
])
def test_roundtrip_is_byte_for_byte(text):
    assert SSHConfigDocument.parse_text(text).text() == text


def test_block_boundaries_and_tokens():
    doc = SSHConfigDocument.parse_text(FIXTURE)
    tokens = [b.tokens for b in doc.host_blocks()]
    assert tokens == [
        ["web"],
        ["db", "jump"],
        ["two words", "plain"],
        ["tail"],
    ]
    # The Match block and the Include line live in RawSpans, not HostBlocks.
    raw_text = "".join(n.text() for n in doc.nodes if isinstance(n, RawSpan))
    assert "Match host *.internal" in raw_text
    assert "Include fragments/*" in raw_text
    # In-block comments and blank lines up to the next header belong to the block.
    web = doc.host_blocks("web")[0]
    assert web.text() == (
        "Host web\n"
        "    # pinned comment\n"
        "    HostName example.com\n"
        "\n"
    )


def test_host_blocks_token_membership():
    doc = SSHConfigDocument.parse_text(FIXTURE)
    assert len(doc.host_blocks("jump")) == 1
    assert doc.host_blocks("jump")[0] is doc.host_blocks("db")[0]
    assert doc.host_blocks("two words")  # quoted token addressable
    assert doc.host_blocks("nope") == []


def test_repeated_blocks_are_separate_nodes():
    text = "Host web\n    Port 1\n\nHost web\n    Port 2\n"
    doc = SSHConfigDocument.parse_text(text)
    blocks = doc.host_blocks("web")
    assert len(blocks) == 2
    assert blocks[0].lines != blocks[1].lines


def test_parse_file_matches_parse_text(tmp_path):
    p = tmp_path / "config"
    p.write_text(FIXTURE)
    doc = SSHConfigDocument.parse_file(str(p))
    assert doc.path == str(p)
    assert doc.text() == FIXTURE


def test_split_host_tokens_fallback():
    assert split_host_tokens('a "b c" d') == ["a", "b c", "d"]
    assert split_host_tokens('"unbalanced x') == ['"unbalanced', "x"]
    assert split_host_tokens("") == []


def test_node_types_are_exactly_two():
    doc = SSHConfigDocument.parse_text(FIXTURE)
    assert all(isinstance(n, (HostBlock, RawSpan)) for n in doc.nodes)
