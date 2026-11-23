"""Draft evaluation crosses the wire strictly and unchanged."""
import pytest

from sshpilot.api.models.connections import (
    EvaluateSshConfigDraftRequest,
    SshConfigDraftError,
    SshConfigDraftEvaluation,
    SshConfigDraftInclude,
    SshConfigDraftLine,
    SshConfigDraftLineState,
    SshConfigDraftSource,
    SshConfigDraftValue,
)
from sshpilot.api.transport.config_draft_codec import (
    evaluate_ssh_config_draft_request_from_wire,
    evaluate_ssh_config_draft_request_to_wire,
    ssh_config_draft_evaluation_from_wire,
    ssh_config_draft_evaluation_to_wire,
)

EVALUATION = SshConfigDraftEvaluation(
    values=(SshConfigDraftValue("user", "deploy"), SshConfigDraftValue("identityfile", "~/.ssh/a")),
    defaults=("addressfamily",),
    matched_blocks=(3, 9),
    lines=(
        SshConfigDraftLine(4, SshConfigDraftLineState.APPLIED),
        SshConfigDraftLine(10, SshConfigDraftLineState.SHADOWED, "/etc/ssh/ssh_config", 2, "Host *"),
    ),
    sources=(SshConfigDraftSource("user", "", 4, "Host web1"),),
    includes=(SshConfigDraftInclude(1, ("/home/u/.ssh/conf.d/a.conf",)),),
    forced=(SshConfigDraftValue("connecttimeout", "7"),),
    system_config="/etc/ssh/ssh_config",
    isolated=False,
)


@pytest.mark.parametrize("request_", [
    EvaluateSshConfigDraftRequest(text="Host a\n", host="a"),
    EvaluateSshConfigDraftRequest(text="", host="a", user="u", port=2200, run_new_exec=True),
])
def test_request_round_trips(request_):
    wire = evaluate_ssh_config_draft_request_to_wire(request_)
    assert evaluate_ssh_config_draft_request_from_wire(wire) == request_


@pytest.mark.parametrize("evaluation", [
    EVALUATION,
    SshConfigDraftEvaluation(errors=(SshConfigDraftError("", 2, "Bad configuration option: x"),)),
    SshConfigDraftEvaluation(pending_exec=("touch /tmp/x",), isolated=True),
])
def test_evaluation_round_trips(evaluation):
    wire = ssh_config_draft_evaluation_to_wire(evaluation)
    assert ssh_config_draft_evaluation_from_wire(wire) == evaluation


def test_unknown_and_invalid_fields_are_rejected():
    wire = ssh_config_draft_evaluation_to_wire(EVALUATION)
    with pytest.raises((TypeError, ValueError)):
        ssh_config_draft_evaluation_from_wire({**wire, "extra": 1})
    bad = dict(wire, lines=[dict(wire["lines"][0], state="nonsense")])
    with pytest.raises((TypeError, ValueError)):
        ssh_config_draft_evaluation_from_wire(bad)
    with pytest.raises((TypeError, ValueError)):
        evaluate_ssh_config_draft_request_from_wire({"text": "", "host": ""})
    with pytest.raises((TypeError, ValueError)):
        EvaluateSshConfigDraftRequest(text="", host="a", port=0)


def test_evaluated_means_openssh_answered():
    assert EVALUATION.evaluated
    assert not SshConfigDraftEvaluation(pending_exec=("x",)).evaluated
    assert not SshConfigDraftEvaluation(
        errors=(SshConfigDraftError("", 0, "bad"),)
    ).evaluated
