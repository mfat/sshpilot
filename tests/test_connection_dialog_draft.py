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
from sshpilot.api.models.connections import (
    EvaluateConnectionEditRequest,
    UpdateConnectionRequest,
)
from sshpilot.connection_dialog_answer import EditorAnswer
from sshpilot.connection_dialog_draft import _AUTH_KEYWORDS, read_answer
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


def _edit(tmp_path, saved_text, host="demo", extra=None, identity_files=None):
    """The editor's question, through the daemon's own service: the saved
    configuration, and an update carrying the form's pending lines."""
    from tests.core.test_connection_edit_evaluation import _service

    folder = tmp_path / "service"
    folder.mkdir(exist_ok=True)
    service, repository, _root = _service(folder, saved_text)
    patch = {}
    if extra is not None:
        patch["extra_ssh_config"] = "\n".join(extra)
    if identity_files is not None:
        patch["identity_files"] = list(identity_files)
    update = None
    if patch:
        update = UpdateConnectionRequest(
            config_patch=patch,
            expected_generation=repository.get_record(host).generation,
        )
    return service.evaluate_connection_edit(EvaluateConnectionEditRequest(
        connection_id=host, update=update, without_own=tuple(sorted(_AUTH_KEYWORDS)),
    ))


def _resolve(tmp_path, policy, saved_text, host="demo"):
    lines = policy.to_lines()
    result = _edit(tmp_path, saved_text, host, extra=lines)
    answer = EditorAnswer(result)
    policy.set_draft(answer.first(), answer.inherited_first(), lines)
    return result, answer


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
    result, _answer = _resolve(tmp_path, policy, saved)

    after = tmp_path / "after"
    after.write_text(result.text)
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
    result, _answer = _resolve(tmp_path, policy, saved)

    after = tmp_path / "after"
    after.write_text(result.text)
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


def _identities(result):
    return read_answer(result.evaluation, result.text, result.host).identities


def test_effective_identities_list_inherited_files_in_openssh_order(tmp_path):
    result = _edit(tmp_path, IDENTITY_CONFIG)
    identities = _identities(result)
    saved = tmp_path / "saved"
    saved.write_text(IDENTITY_CONFIG)
    assert [i.path for i in identities] == _ssh_g(saved)["identityfile"]
    assert [(i.path, i.origin, i.block) for i in identities] == [
        ("~/.ssh/demo_key", "host", "Host demo"),
        ("~/.ssh/star_key", "block", "Host *"),
    ]


def test_pending_identity_lines_are_counted(tmp_path):
    result = _edit(tmp_path, IDENTITY_CONFIG, identity_files=["~/.ssh/new_key"])
    assert [(i.path, i.origin) for i in _identities(result)] == [
        ("~/.ssh/new_key", "host"), ("~/.ssh/star_key", "block"),
    ]


def test_builtin_identity_files_are_named_as_defaults(tmp_path):
    answer = _ask(tmp_path, "Host demo\n    User me\n")
    assert answer.identities
    assert {i.origin for i in answer.identities} == {"default"}
