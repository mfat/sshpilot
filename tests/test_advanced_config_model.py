"""The draft editor: every edit to the configuration, without a window."""
import pytest

from sshpilot.advanced_config_model import (
    DraftEditor,
    Refusal,
    can_place,
    first_host_name,
    plain_host_names,
    sample_hosts,
)
from sshpilot.ssh_config_blocks import GLOBAL, HOST, MATCH, Block

TEXT = (
    "Compression yes\n\nHost web1 web2\n    User deploy\n    Port 2222\n\n"
    "Host *\n    User fallback\n"
)


@pytest.fixture()
def editor():
    editor = DraftEditor()
    editor.load(TEXT)
    return editor


def test_loading_changes_nothing(editor):
    assert editor.text == TEXT and not editor.dirty
    assert not editor.can_undo and not editor.can_redo


def test_global_options_come_first():
    host, match = Block(kind=HOST, patterns="a"), Block(kind=MATCH, patterns="all")
    glob = Block(kind=GLOBAL)
    off = Block(kind=HOST, patterns="old", disabled=True)
    off_glob = Block(kind=GLOBAL, disabled=True)
    assert can_place([glob, host, match]) and can_place([host, match])
    assert not can_place([host, glob])        # the options would become host a's
    # A switched-off block above active global options would read back as
    # plain comments (an active line follows it): refused.
    assert not can_place([off, glob, host])
    assert can_place([off, off_glob, host])   # both comments: nothing changes


def test_moves_are_refused_when_they_would_change_whose_options_they_are(editor):
    glob, web, star = editor.blocks
    assert editor.move_block(web.uid, 0) is Refusal.GLOBAL_ORDER
    assert editor.move_block(glob.uid, 2) is Refusal.GLOBAL_ORDER
    assert editor.move_block_by(glob.uid, 1) is False      # global options do not move
    assert editor.text == TEXT
    assert editor.move_block(star.uid, 1) is True
    assert [b.patterns for b in editor.blocks] == ["", "*", "web1 web2"]
    assert editor.move_block(star.uid, 1) is False          # already there


def test_a_switched_off_block_cannot_go_above_global_options(editor):
    """Review: such a block read back as comments, and the global options
    as its own once it was switched on again."""
    glob, web, _star = editor.blocks
    assert editor.toggle_block(web.uid) is True
    assert editor.move_block(web.uid, 0) is Refusal.GLOBAL_ORDER
    assert editor.blocks[0] is glob


def test_what_the_editor_writes_reads_back_as_the_same_blocks(editor):
    from sshpilot.ssh_config_blocks import parse

    _glob, web, star = editor.blocks
    editor.toggle_block(web.uid)
    editor.move_block(star.uid, 1)
    editor.add_block(MATCH)
    again = parse(editor.text)
    assert [(b.kind, b.patterns, b.disabled) for b in again.blocks] == [
        (b.kind, b.patterns, b.disabled) for b in editor.blocks
    ][:3] + [("match", "all", False)]


def test_one_global_block(editor):
    assert editor.has_global and editor.add_block(GLOBAL) is None
    fresh = DraftEditor()
    fresh.load("Host a\n    User x\n")
    block = fresh.add_block(GLOBAL)
    assert fresh.blocks[0] is block


def test_structural_edits_are_undone_one_at_a_time(editor):
    web = editor.blocks[1]
    option = editor.add_option(web.uid, "ProxyJump", "bastion")
    editor.delete_option(web.options[0].uid)
    assert "ProxyJump bastion" in editor.text and "User deploy" not in editor.text
    assert editor.undo() and "User deploy" in editor.text
    assert editor.undo() and editor.text == TEXT
    assert not editor.undo()
    assert editor.redo() and editor.redo() and not editor.redo()
    assert editor.find_option(option.uid)[1] is not None     # identities survive


def test_typing_is_one_undo_step_per_pause(editor):
    option = editor.blocks[1].options[0]
    for text in ("r", "ro", "roo", "root"):
        assert editor.set_option(option.uid, "value", text)
    assert editor.can_undo                    # before the pause is over, too
    editor.commit_typing()
    editor.set_option(option.uid, "value", "admin")
    assert editor.undo() and "User root" in editor.text      # back over one edit
    assert editor.undo() and editor.text == TEXT


def test_setting_the_same_text_is_not_an_edit(editor):
    serial = editor.serial
    option = editor.blocks[1].options[0]
    assert not editor.set_option(option.uid, "value", "deploy")
    assert not editor.set_patterns(editor.blocks[1].uid, "web1 web2")
    assert editor.serial == serial and not editor.dirty


def test_options_move_within_and_between_blocks(editor):
    _glob, web, star = editor.blocks
    port = web.options[1]
    assert editor.move_option(port.uid, web.uid, 0)
    assert [o.key for o in web.options] == ["Port", "User"]
    assert not editor.move_option(port.uid, web.uid, 0)
    assert editor.move_option(port.uid, star.uid, None)
    assert [o.key for o in star.options] == ["User", "Port"]


def test_text_replaces_the_draft_and_can_be_undone(editor):
    editor.replace_text("Host only\n    User x\n")
    assert [b.patterns for b in editor.blocks] == ["only"]
    assert editor.undo() and editor.text == TEXT


def test_saved_text_is_the_new_baseline(editor):
    editor.set_option(editor.blocks[1].options[0].uid, "value", "root")
    assert editor.dirty
    editor.mark_saved(editor.text)
    assert not editor.dirty


def test_lines_follow_the_text(editor):
    web = editor.blocks[1]
    assert editor.lines.headers[web.uid] == 3
    assert editor.owner_of_line(4) == web.options[0].uid
    assert editor.owner_of_line(3) == web.uid
    assert editor.owner_of_line(2) is None


def test_the_host_a_block_is_about(editor):
    _glob, web, star = editor.blocks
    assert first_host_name(web) == "web1"
    assert first_host_name(star) == ""                 # a pattern names no host
    assert first_host_name(editor.blocks[0]) == ""
    assert first_host_name(Block(kind=HOST, patterns="*.prod !x edge")) == "edge"
    assert first_host_name(Block(kind=MATCH, patterns="host web1")) == ""
    assert first_host_name(None) == ""


def test_the_hosts_a_pattern_block_is_asked_about(editor):
    """A pattern is no host: OpenSSH is asked about the names the blocks
    spell out, and about a name made up from the pattern."""
    off = Block(kind=HOST, patterns="old", disabled=True)
    assert plain_host_names(editor.blocks + [off]) == ["web1", "web2"]
    assert sample_hosts(Block(kind=HOST, patterns="*.prod !bad.prod web?")) == [
        "x.prod", "webx",
    ]
    assert sample_hosts(Block(kind=MATCH, patterns="host *.prod")) == []   # criteria
    assert sample_hosts(None) == []
