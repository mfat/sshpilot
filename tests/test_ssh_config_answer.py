"""Advanced Configuration and the connection editor mark a line alike.

Both read OpenSSH's answer with :class:`sshpilot.ssh_config_answer.ConfigAnswer`
and take each line's mark from its :meth:`verdict`. These tests put one
configuration through both paths, the window's draft evaluation and the
editor's ``evaluate_connection_edit``, and require the same marks.
"""
from __future__ import annotations

import shutil
from dataclasses import replace

import pytest

from sshpilot.api.models.connections import (
    EvaluateConnectionEditRequest,
    EvaluateSshConfigDraftRequest,
    UpdateConnectionRequest,
)
from sshpilot.connection_dialog_answer import EditorAnswer
from sshpilot.ssh_config_answer import (
    NO_EFFECT,
    NOTHING,
    OVERRIDDEN,
    REJECTED,
    USED,
    ConfigAnswer,
)

needs_ssh = pytest.mark.skipif(
    shutil.which("ssh") is None, reason="needs the OpenSSH client"
)

TEXT = (
    "PasswordAuthentication no\n"
    "\n"
    "Host *\n"
    "    User shared\n"
    "\n"
    "Host h\n"
    "    HostName 192.0.2.1\n"
)
OWN = [
    "PasswordAuthentication yes",  # beaten by line 1
    "User mine",                   # beaten by Host *
    "Port 2200",                   # overridden by Settings
    "ServerAliveInterval 30",      # used
]
FORCED = ("Port=2222",)


def _both(tmp_path, own=OWN):
    from tests.core.test_connection_edit_evaluation import _service

    service, repository, root = _service(tmp_path, TEXT)
    args = []
    for option in FORCED:
        args += ["-o", option]
    service._forced_ssh_option_args = lambda: tuple(args)
    editor = service.evaluate_connection_edit(EvaluateConnectionEditRequest(
        connection_id="h",
        update=UpdateConnectionRequest(
            config_patch={"extra_ssh_config": "\n".join(own)},
            expected_generation=repository.get_record("h").generation,
        ),
    ))
    # The window evaluates the same text: what Save would write.
    window = service.evaluate_ssh_config_draft(
        EvaluateSshConfigDraftRequest(text=editor.text, host="h")
    )
    return ConfigAnswer(window), EditorAnswer(editor)


@needs_ssh
def test_the_window_and_the_editor_mark_every_line_the_same(tmp_path):
    window, editor = _both(tmp_path)
    expected = {
        "passwordauthentication": NO_EFFECT,
        "user": NO_EFFECT,
        "port": OVERRIDDEN,
        "serveraliveinterval": USED,
    }
    for keyword, state in expected.items():
        (line, _value), = editor.own[keyword]
        theirs = window.verdict(line, keyword)
        ours = editor.line_verdict(keyword)
        assert ours.state == state, keyword
        # One verdict: same tag, colour, tooltip, notes and winning line
        # (the editor names its file where the window says "this file").
        if theirs.target is not None:
            theirs = replace(theirs, target=(editor.result.file or "", theirs.target[1]))
        assert ours == theirs, keyword


@needs_ssh
def test_no_effect_says_what_wins_and_links_to_it(tmp_path):
    _window, editor = _both(tmp_path)
    verdict = editor.line_verdict("PasswordAuthentication")
    assert verdict.tag == "No effect"
    assert verdict.note == "OpenSSH uses no from line 1 (global options)"
    assert verdict.target == (editor.result.file or "", 1)
    assert "Click to show that line." in verdict.tooltip
    assert verdict.unused


@needs_ssh
def test_a_rejected_line_is_marked_alike_and_does_not_link(tmp_path):
    # OpenSSH then evaluates nothing else: only the rejection is known.
    window, editor = _both(tmp_path, ["Compression maybe"])
    (line, _value), = editor.own["compression"]
    verdict = editor.line_verdict("Compression")
    assert verdict == window.verdict(line, "compression")
    assert verdict.state == REJECTED
    assert (verdict.tag, verdict.style, verdict.target) == ("Rejected", "error", None)
    assert not verdict.unused


def test_without_an_answer_nothing_is_said():
    assert ConfigAnswer(None).verdict(3, "user") == NOTHING
    assert not ConfigAnswer(None).available
