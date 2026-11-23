"""Advanced Configuration: the rules that need no window."""
from pathlib import Path

import pytest

from sshpilot.api.models.connections import (
    SshConfigDraftError,
    SshConfigDraftLine,
    SshConfigDraftLineState as State,
)
from sshpilot.gtk import advanced_config_messages as messages
from sshpilot.ssh_config_blocks import GLOBAL, HOST, MATCH, Block

ROOT = Path(__file__).resolve().parents[1]


def _can_place(blocks):
    pytest.importorskip("gi")
    from sshpilot.advanced_config_window import can_place

    return can_place(blocks)


def test_global_options_must_precede_every_block_that_is_on():
    host, match = Block(kind=HOST, patterns="a"), Block(kind=MATCH, patterns="all")
    glob = Block(kind=GLOBAL)
    off = Block(kind=HOST, patterns="old", disabled=True)
    assert _can_place([glob, host, match])
    assert _can_place([off, glob, host])       # comments above change nothing
    assert not _can_place([host, glob])        # the options would become host a's
    assert not _can_place([off, host, glob, match])
    assert _can_place([host, match])


def test_target_is_split_like_ssh_does():
    pytest.importorskip("gi")
    from sshpilot.advanced_config_window import split_target

    assert split_target(" web1 ") == (None, "web1")
    assert split_target("deploy@web1") == ("deploy", "web1")
    assert split_target("a@b@web1") == ("a@b", "web1")


def test_every_line_state_is_worded_and_styled():
    for state in State:
        assert messages.state_label(state)
        assert messages.STATE_STYLES[state]
        assert messages.state_tooltip(SshConfigDraftLine(3, state))
    shadowed = SshConfigDraftLine(9, State.SHADOWED, "", 4, "Host web1")
    assert "line 4 (Host web1)" in messages.state_tooltip(shadowed)
    other = SshConfigDraftLine(9, State.SHADOWED, "/etc/ssh/ssh_config", 2, "Host *")
    assert "/etc/ssh/ssh_config, line 2 (Host *)" in messages.state_tooltip(other)


def test_openssh_s_diagnostic_is_shown_as_it_is():
    text = messages.error_text(SshConfigDraftError("", 7, "Bad configuration option: x"))
    assert "7" in text and text.endswith("Bad configuration option: x")
    text = messages.error_text(SshConfigDraftError("/etc/ssh/ssh_config", 2, "Bad port"))
    assert "/etc/ssh/ssh_config" in text


def test_block_status_waits_for_openssh():
    assert messages.block_status(False, None, False)[0] == ""
    assert messages.block_status(True, None, False)[1] == "muted"
    assert messages.block_status(False, True, False)[1] == "success"
    assert messages.block_status(False, False, False)[1] == "muted"


# -- the daemon side stays locale-neutral --------------------------------------

@pytest.mark.parametrize("path", [
    "src/sshpilot/core/ssh_config_draft.py",
    "src/sshpilot/ssh_config_blocks.py",
    "src/sshpilot/api/transport/config_draft_codec.py",
])
def test_backend_modules_do_not_translate(path):
    source = (ROOT / path).read_text()
    assert "gettext" not in source and "_(" not in source.replace("__(", "")


def test_window_strings_are_extracted():
    potfiles = (ROOT / "po" / "POTFILES").read_text().splitlines()
    assert "src/sshpilot/advanced_config_window.py" in potfiles
    assert "src/sshpilot/gtk/advanced_config_messages.py" in potfiles
