"""A client's newer draft evaluation supersedes its earlier ones."""
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.connections import (
    EvaluateSshConfigDraftRequest,
    SshConfigDraftEvaluation,
)
from sshpilot.api.transport.config_draft_codec import (
    evaluate_ssh_config_draft_request_to_wire,
)
from sshpilot.api.transport.envelopes import RequestEnvelope
from sshpilot.daemon.dispatch import (
    EFFECTIVE_CONFIG_COMMAND_KEY,
    SSH_CONFIG_DRAFT_COMMAND_KEY,
    RequestDispatcher,
)


def _dispatch(dispatcher, client, text):
    params = evaluate_ssh_config_draft_request_to_wire(
        EvaluateSshConfigDraftRequest(text=text, host="web1")
    )
    request = RequestEnvelope("1.0", f"req-{text}", "connections.evaluate_ssh_config_draft", params)
    return RequestDispatcher._handle_evaluate_ssh_config_draft(
        dispatcher, request, SimpleNamespace(client_id=client)
    )


def _dispatcher(seen):
    def evaluate(draft, cancelled):
        seen.append((draft.text, cancelled))
        return SshConfigDraftEvaluation()

    return SimpleNamespace(_connections=SimpleNamespace(evaluate_ssh_config_draft=evaluate))


def test_an_older_question_is_dropped_before_it_starts():
    seen = []
    dispatcher = _dispatcher(seen)
    older = _dispatch(dispatcher, "client:a", "one")
    newer = _dispatch(dispatcher, "client:a", "two")
    with pytest.raises(SshPilotError) as error:
        older.operation()
    assert error.value.code is ErrorCode.OPERATION_CANCELLED
    newer.operation()
    assert [text for text, _cancelled in seen] == ["two"]


def test_a_running_evaluation_is_told_when_it_is_superseded():
    seen = []
    dispatcher = _dispatcher(seen)
    first = _dispatch(dispatcher, "client:a", "one")
    first.operation()
    _text, cancelled = seen[0]
    assert cancelled() is False
    _dispatch(dispatcher, "client:a", "two")
    assert cancelled() is True                # polled while ssh runs


def test_clients_do_not_supersede_each_other_and_have_their_own_lane():
    seen = []
    dispatcher = _dispatcher(seen)
    a = _dispatch(dispatcher, "client:a", "one")
    b = _dispatch(dispatcher, "client:b", "two")
    a.operation()
    b.operation()
    assert len(seen) == 2
    assert a.command_key != b.command_key
    assert a.command_key[0] == SSH_CONFIG_DRAFT_COMMAND_KEY != EFFECTIVE_CONFIG_COMMAND_KEY
