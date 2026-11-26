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


# -- the files of the configuration, by the names the daemon gave out ---------

def _text_dispatcher(asked):
    from sshpilot.api.models.connections import SshConfigText

    def get(file=""):
        asked.append(file)
        return SshConfigText("Host a\n", "r1", "config", True, file=file)

    return SimpleNamespace(_connections=SimpleNamespace(get_ssh_config_text=get))


def _get_text(dispatcher, params):
    request = RequestEnvelope("1.0", "req-text", "connections.get_ssh_config_text", params)
    return RequestDispatcher._handle_get_ssh_config_text(
        dispatcher, request, SimpleNamespace(client_id="client:a")
    )


def test_config_text_is_asked_for_the_root_or_for_a_named_file():
    asked = []
    dispatcher = _text_dispatcher(asked)
    assert _get_text(dispatcher, {}).operation()["file"] == ""
    named = _get_text(dispatcher, {"file": "/home/u/.ssh/hosts.conf"}).operation()
    assert named["file"] == "/home/u/.ssh/hosts.conf"
    assert asked == ["", "/home/u/.ssh/hosts.conf"]


@pytest.mark.parametrize("params", [{"path": "/etc/passwd"}, {"file": 7}, {"file": "x", "y": 1}])
def test_config_text_takes_a_file_name_and_nothing_else(params):
    with pytest.raises(ValueError):
        _get_text(_text_dispatcher([]), params)


def test_a_draft_request_names_its_file_on_the_wire():
    seen = []
    dispatcher = _dispatcher(seen)
    params = evaluate_ssh_config_draft_request_to_wire(
        EvaluateSshConfigDraftRequest(text="Host a\n", host="a", file="/x/hosts.conf")
    )
    request = RequestEnvelope("1.0", "req-file", "connections.evaluate_ssh_config_draft", params)
    captured = []
    dispatcher._connections.evaluate_ssh_config_draft = (
        lambda draft, cancelled: captured.append(draft.file) or SshConfigDraftEvaluation()
    )
    RequestDispatcher._handle_evaluate_ssh_config_draft(
        dispatcher, request, SimpleNamespace(client_id="client:a")
    ).operation()
    assert captured == ["/x/hosts.conf"]
