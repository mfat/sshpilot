"""Which lines of a block the Structured view shows as fields, and what a
key file says about itself (GTK-free)."""
from __future__ import annotations

import os

from sshpilot import advanced_config_fields as fields
from sshpilot.ssh_config_blocks import parse


def _block(text):
    return parse("Host x\n" + text).blocks[0]


def _keys(block, uids):
    by_uid = {option.uid: option for option in block.options}
    return [by_uid[uid].key for uid in uids]


def test_each_field_shows_the_first_line_of_its_keyword():
    block = _block(
        "    HostName a\n    User one\n    User two\n    Port 22\n"
        "    ProxyJump j1\n    Compression yes\n"
    )
    claims = fields.claim(block.options)
    assert sorted(claims.single) == ["hostname", "port", "proxyjump", "user"]
    first_user = block.options[1].uid
    assert claims.single["user"] == first_user
    # The second User line is a line of its own (OpenSSH does not use it):
    # it is shown in the table, never dropped or merged.
    assert _keys(block, claims.other) == ["User", "Compression"]


def test_a_switch_takes_a_line_only_when_it_is_yes_or_no():
    block = _block(
        "    ForwardAgent $SSH_AUTH_SOCK\n    IdentitiesOnly Yes\n    ForwardAgent no\n"
    )
    claims = fields.claim(block.options)
    assert "forwardagent" not in claims.switches          # the first one is a socket
    assert claims.switches["identitiesonly"] == block.options[1].uid
    # OpenSSH keeps the first ForwardAgent: the second is not the field's to show.
    assert _keys(block, claims.other) == ["ForwardAgent", "ForwardAgent"]


def test_lists_keep_every_line_in_order():
    block = _block(
        "    IdentityFile ~/.ssh/a\n    LocalForward 1 h:1\n    IdentityFile ~/.ssh/b\n"
        "    DynamicForward 1080\n    RemoteForward 2 h:2\n"
    )
    claims = fields.claim(block.options)
    assert _keys(block, claims.identities) == ["IdentityFile", "IdentityFile"]
    assert _keys(block, claims.forwards) == ["LocalForward", "DynamicForward", "RemoteForward"]
    assert claims.other == []


def test_a_line_added_from_the_table_stays_there():
    block = _block("    Port 22\n    Port 23\n")
    kept = block.options[0].uid
    claims = fields.claim(block.options, keep={kept})
    assert claims.other == [kept, block.options[1].uid]
    # The field shows the first Port line or none: never the second, which
    # OpenSSH does not use, as if it were the one in effect.
    assert "port" not in claims.single


def test_an_empty_keyword_is_a_line_of_the_table():
    block = _block("    User a\n")
    block.options.append(type(block.options[0])())
    claims = fields.claim(block.options)
    assert claims.other == [block.options[1].uid]


def test_key_file_status(tmp_path):
    home = str(tmp_path)
    key = tmp_path / ".ssh" / "id"
    key.parent.mkdir()
    key.write_text("k")
    os.chmod(key, 0o644)
    status = fields.key_file_status("~/.ssh/id", home)
    assert (status.state, status.path, status.mode) == (fields.KEY_OPEN, str(key), 0o644)
    assert fields.fix_key_permissions(status.path)
    assert fields.key_file_status('"~/.ssh/id"', home).state == fields.KEY_OK
    assert fields.key_file_status("~/.ssh/none", home).state == fields.KEY_MISSING
    for value in ("~/.ssh/id_%h", "${HOME}/.ssh/id", "~other/.ssh/id", ""):
        assert fields.key_file_status(value, home).state == fields.KEY_UNCHECKED


def test_paths_are_written_as_a_configuration_writes_them():
    assert fields.home_relative("/home/me/.ssh/id", "/home/me") == "~/.ssh/id"
    assert fields.home_relative("/home/me/my keys/id", "/home/me") == '"~/my keys/id"'
    assert fields.home_relative("/etc/ssh/key", "/home/me") == "/etc/ssh/key"


def test_jump_hops():
    assert fields.jump_hops("none") == [] and fields.jump_hops("") == []
    assert fields.jump_hops("a, admin@b:2222") == ["a", "admin@b:2222"]
    assert [fields.hop_host(h) for h in (
        "admin@bastion:2222", "ssh://u@h:22", "[::1]:22", "plain",
    )] == ["bastion", "h", "::1", "plain"]


def test_forwards_read_as_listen_and_target():
    assert fields.forward_parts("localforward", "5432 127.0.0.1:5432") == (":5432", "127.0.0.1:5432")
    assert fields.forward_parts("localforward", "5432 [127.0.0.1]:5432") == (":5432", "127.0.0.1:5432")
    assert fields.forward_parts("dynamicforward", "localhost:1080") == ("localhost:1080", "")
    assert fields.same_forward("remoteforward", "9000 localhost:9000", "9000  [localhost]:9000")


def test_lines_keep_the_block_order_each_with_its_control():
    block = _block(
        "    LocalForward 1 h:1\n    User a\n    Compression yes\n    IdentityFile ~/.ssh/a\n"
        "    ForwardAgent no\n    User b\n"
    )
    kinds = fields.line_kinds(block.options)
    assert [uid for uid, _kind in kinds] == [option.uid for option in block.options]
    assert [kind for _uid, kind in kinds] == [
        fields.FORWARD_KIND, fields.SINGLE_KIND, fields.OTHER_KIND,
        fields.IDENTITY_KIND, fields.SWITCH_KIND, fields.OTHER_KIND,
    ]
    added = block.options[1].uid
    assert dict(fields.line_kinds(block.options, keep={added}))[added] == fields.OTHER_KIND
