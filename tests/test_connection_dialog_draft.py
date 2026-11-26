"""The connection editor agrees with OpenSSH about lines not saved yet.

Each case builds the draft the dialog would send, has the daemon's draft
evaluator ask real OpenSSH about it (isolated mode: the root is read with
``-F``, nothing of the developer's configuration is involved), feeds the
answer to the authentication model, and compares with ``ssh -G`` on the
file as it would be saved.
"""
from __future__ import annotations

import getpass
import shutil
import subprocess

import pytest

from sshpilot.api.models.connections import EvaluateSshConfigDraftRequest
from sshpilot.connection_dialog_auth import AuthPolicy, is_policy_line
from sshpilot.connection_dialog_draft import (
    build_drafts,
    draft_for_host,
    has_own_block,
    read_answer,
)
from sshpilot.core.ssh_config_draft import DraftContext, evaluate_draft

pytestmark = pytest.mark.skipif(
    shutil.which("ssh") is None, reason="needs the OpenSSH client"
)

USER = getpass.getuser()


def _ssh_g(path, host="demo"):
    out = subprocess.run(
        ["ssh", "-F", str(path), "-G", host], capture_output=True, text=True,
        check=True, stdin=subprocess.DEVNULL,
    ).stdout
    values = {}
    for line in out.splitlines():
        key, _sep, value = line.strip().partition(" ")
        values.setdefault(key.lower(), []).append(value)
    return values


def _ask(tmp_path, text, host="demo"):
    root = tmp_path / "isolated" / "config"
    root.parent.mkdir(exist_ok=True)
    if not root.exists():
        root.write_text(text)
    context = DraftContext(root_path=str(root), isolated=True, overrides=())
    evaluation = evaluate_draft(
        EvaluateSshConfigDraftRequest(text=text, host=host), context, root.read_text()
    )
    return read_answer(evaluation, text, host)


def _policy_for(tmp_path, saved_text, host="demo"):
    """The model as the dialog has it after opening *host*."""
    own = []
    in_own = False
    for line in saved_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("host ", "match ")):
            in_own = stripped.split()[1:] == [host] and stripped.lower().startswith("host ")
            continue
        if in_own and is_policy_line(stripped):
            own.append(stripped)
    policy = AuthPolicy.from_lines(own)
    _resolve(tmp_path, policy, saved_text, host)
    return policy


def _resolve(tmp_path, policy, saved_text, host="demo", identity=None):
    lines = policy.to_lines()
    drafts = build_drafts(saved_text, host, lines, identity)
    effective = _ask(tmp_path, drafts.effective, host)
    inherited = effective if drafts.inherited == drafts.effective else _ask(
        tmp_path, drafts.inherited, host
    )
    policy.set_draft(effective.first(), inherited.first(), lines)
    return drafts, effective


# -- building the draft --------------------------------------------------------

def test_draft_swaps_only_the_named_lines_of_the_hosts_own_block():
    text = (
        "Host other\n    PasswordAuthentication no\n"
        "Host demo extra\n    User me\n    PasswordAuthentication yes\n"
        "    ChallengeResponseAuthentication no\n"
        "Host *\n    PasswordAuthentication no\n"
    )
    draft = draft_for_host(
        text, "demo", {"passwordauthentication", "kbdinteractiveauthentication"},
        ["PubkeyAuthentication no"],
    )
    assert draft == (
        "Host other\n    PasswordAuthentication no\n"
        "Host demo extra\n    PubkeyAuthentication no\n    User me\n"
        "Host *\n    PasswordAuthentication no\n"
    )


def test_a_host_kept_in_an_included_file_has_no_draft():
    text = "Include conf.d/*\nHost *\n    User me\n"
    assert not has_own_block(text, "demo")
    assert draft_for_host(text, "demo", {"user"}, []) is None
    assert build_drafts(text, "demo", ["PasswordAuthentication no"]) is None


def test_a_new_host_is_appended_after_everything_else():
    draft = draft_for_host(
        "Host *\n    User me", "fresh", set(), ["Port 2200"], new=True
    )
    assert draft == "Host *\n    User me\nHost fresh\n    Port 2200\n"


# -- the reported case: a later Match block decides once the line is gone ---------

def test_removing_the_hosts_yes_shows_the_match_blocks_no(tmp_path):
    saved = (
        "Host demo\n    PasswordAuthentication yes\n"
        f"Match localuser {USER}\n    PasswordAuthentication no\n"
    )
    policy = _policy_for(tmp_path, saved)
    assert policy.shown_checked("password") is True
    assert "password" in policy.will_try()

    assert policy.set_switch("password", None)
    drafts, _answer = _resolve(tmp_path, policy, saved)

    after = tmp_path / "after"
    after.write_text(drafts.effective)
    assert _ssh_g(after)["passwordauthentication"] == ["no"]
    assert policy.effective_switch("password") is False
    assert policy.shown_checked("password") is False
    assert "password" not in policy.will_try()


def test_checking_keeps_the_hosts_no_when_a_match_block_also_says_no(tmp_path):
    saved = (
        "Host demo\n    PasswordAuthentication no\n"
        f"Match localuser {USER}\n    PasswordAuthentication no\n"
    )
    policy = _policy_for(tmp_path, saved)
    assert policy.shown_checked("password") is False

    assert policy.set_tried("password", True)
    # Dropping the "no" would leave the Match block's: a "yes" is written.
    assert "PasswordAuthentication yes" in policy.to_lines()
    drafts, _answer = _resolve(tmp_path, policy, saved)

    after = tmp_path / "after"
    after.write_text(drafts.effective)
    assert _ssh_g(after)["passwordauthentication"] == ["yes"]
    assert policy.shown_checked("password") is True
    assert "password" in policy.will_try()


def test_an_edit_makes_the_answer_stale_until_the_next_one(tmp_path):
    saved = "Host demo\n    PasswordAuthentication yes\n"
    policy = _policy_for(tmp_path, saved)
    assert policy._draft_current()
    policy.set_switch("password", False)
    assert not policy._draft_current()
    # The model answers from the block's own line meanwhile.
    assert policy.effective_switch("password") is False


# -- a failed lookup is unknown, not a default -----------------------------------

def test_a_failed_lookup_is_never_shown_as_a_default():
    policy = AuthPolicy.from_lines(["PasswordAuthentication yes"])
    policy.set_inherited({"passwordauthentication": "yes"})
    policy.set_switch("password", None)
    policy.set_lookup_failed()
    assert policy.lookup_failed
    assert policy.will_try() is None
    assert policy.effective_switch("password") is None
    assert policy.shown_checked("password") is None
    # A later answer clears it.
    policy.set_draft({"passwordauthentication": "no"}, {"passwordauthentication": "no"},
                     policy.to_lines())
    assert not policy.lookup_failed
    assert policy.shown_checked("password") is False


def test_a_rejected_draft_gives_no_answer(tmp_path):
    assert _ask(tmp_path, "Host demo\n    NoSuchKeyword 1\n") is None


# -- the summary and an unknown switch -------------------------------------------

def test_summary_marks_a_listed_method_whose_switch_is_unknown():
    policy = AuthPolicy.from_lines([
        "PreferredAuthentications gssapi-with-mic,publickey,password",
        "PasswordAuthentication no",
        "PubkeyAuthentication yes",
    ])
    # Nothing resolved yet: Kerberos is listed, its switch has no answer.
    assert policy.shown_checked("gssapi-with-mic") is None
    assert policy.will_try() == ["gssapi-with-mic", "publickey"]
    assert policy.unconfirmed_tried() == ["gssapi-with-mic"]
    # The disabled method is left out; the enabled one is not in doubt.
    assert "password" not in policy.will_try()
    assert "publickey" not in policy.unconfirmed_tried()

    policy.set_draft(
        {"preferredauthentications": "gssapi-with-mic,publickey,password",
         "gssapiauthentication": "no", "pubkeyauthentication": "yes",
         "passwordauthentication": "no"},
        {"gssapiauthentication": "no"},
        policy.to_lines(),
    )
    assert policy.unconfirmed_tried() == []
    assert policy.will_try() == ["publickey"]
    assert policy.shown_checked("gssapi-with-mic") is False


# -- identity files --------------------------------------------------------------

IDENTITY_CONFIG = (
    "Host demo\n    IdentitiesOnly yes\n    IdentityFile ~/.ssh/demo_key\n"
    "Host *\n    IdentityFile ~/.ssh/star_key\n"
)


def test_effective_identities_list_inherited_files_in_openssh_order(tmp_path):
    drafts = build_drafts(IDENTITY_CONFIG, "demo", [])
    answer = _ask(tmp_path, drafts.effective)
    saved = tmp_path / "saved"
    saved.write_text(IDENTITY_CONFIG)
    assert [i.path for i in answer.identities] == _ssh_g(saved)["identityfile"]
    assert [(i.path, i.origin, i.block) for i in answer.identities] == [
        ("~/.ssh/demo_key", "host", "Host demo"),
        ("~/.ssh/star_key", "block", "Host *"),
    ]
    # Nothing was edited: the draft is the saved text, so a save of an
    # unrelated field has no inherited key to copy into the host's block.
    assert drafts.effective == IDENTITY_CONFIG


def test_pending_identity_lines_are_counted(tmp_path):
    drafts = build_drafts(
        IDENTITY_CONFIG, "demo", [],
        ["IdentityFile ~/.ssh/new_key", "IdentitiesOnly yes"],
    )
    answer = _ask(tmp_path, drafts.effective)
    assert [(i.path, i.origin) for i in answer.identities] == [
        ("~/.ssh/new_key", "host"), ("~/.ssh/star_key", "block"),
    ]


def test_builtin_identity_files_are_named_as_defaults(tmp_path):
    answer = _ask(tmp_path, "Host demo\n    User me\n")
    assert answer.identities
    assert {i.origin for i in answer.identities} == {"default"}
