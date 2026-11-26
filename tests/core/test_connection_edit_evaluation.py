"""connections.evaluate_connection_edit: OpenSSH's answer for an edit, before
it is saved, about exactly the text the save would write.

Real OpenSSH (``ssh -G``) on a temporary configuration; nothing is written.
"""
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.connections import (
    ConnectionEditEvaluation,
    ConnectionEditRefusal,
    CreateConnectionRequest,
    EvaluateConnectionEditRequest,
    SshConfigDraftLineState as State,
    UpdateConnectionRequest,
)
from sshpilot.api.transport.config_draft_codec import (
    connection_edit_evaluation_from_wire,
    connection_edit_evaluation_to_wire,
    evaluate_connection_edit_request_from_wire,
    evaluate_connection_edit_request_to_wire,
)
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore


def _service(tmp_path, text):
    root = tmp_path / "ssh_config"
    root.write_text(text, encoding="utf-8")
    repository = ConnectionRepository(
        ssh_store=SshConfigStore(root, isolated=True),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=True,
    )
    return ConnectionApplicationService(repository, client_name="test"), repository, root


def _values(evaluation):
    return {item.key: item.value for item in reversed(evaluation.values)}


def _own(result):
    return {item.key: item.value for item in result.own_lines}


def _state(result, key):
    line = next(item.line for item in result.own_lines if item.key == key)
    return next(item for item in result.evaluation.lines if item.line == line)


def test_an_option_inherited_from_a_later_host_star_says_where_it_comes_from(tmp_path):
    """#1406: Host * at the bottom of the file decides Compression."""
    service, _repo, root = _service(
        tmp_path,
        "Host h\n    HostName 192.0.2.1\n\nHost *\n    Compression yes\n",
    )
    saved = root.read_text()
    result = service.evaluate_connection_edit(
        EvaluateConnectionEditRequest(connection_id="h")
    )
    assert result.refusal is None and result.host == "h" and result.file == ""
    assert "compression" not in _own(result)
    assert _values(result.evaluation)["compression"] == "yes"
    source = next(s for s in result.evaluation.sources if s.key == "compression")
    assert (source.file, source.line, source.block) == ("", 5, "Host *")
    assert root.read_text() == saved                          # nothing written


def test_an_own_line_an_earlier_block_beats_is_shadowed(tmp_path):
    service, repo, root = _service(
        tmp_path,
        "Host *\n    Compression yes\n\nHost h\n    HostName 192.0.2.1\n",
    )
    record = repo.get_record("h")
    result = service.evaluate_connection_edit(EvaluateConnectionEditRequest(
        connection_id="h",
        update=UpdateConnectionRequest(
            config_patch={"extra_ssh_config": "Compression no"},
            expected_generation=record.generation,
        ),
    ))
    assert _own(result)["compression"] == "no"
    state = _state(result, "compression")
    assert state.state is State.SHADOWED
    assert (state.decided_line, state.decided_block) == (2, "Host *")
    assert _values(result.evaluation)["compression"] == "yes"


def test_the_rendered_block_is_the_block_the_save_writes(tmp_path):
    text = "Host h\n    HostName 192.0.2.1\n    # kept\n    ServerAliveInterval 5\n"
    patch = {"extra_ssh_config": "ServerAliveInterval 5\nCompression yes",
             "proxy_jump": ["bastion"]}
    service, repo, root = _service(tmp_path, text)
    record = repo.get_record("h")
    request = UpdateConnectionRequest(
        username="deploy", config_patch=patch, expected_generation=record.generation,
    )
    result = service.evaluate_connection_edit(
        EvaluateConnectionEditRequest(connection_id="h", update=request)
    )
    service.update_connection(ConnectionId("h"), request)
    saved = root.read_text().splitlines()
    assert [(item.line, item.value) for item in result.own_lines] == [
        (item.line, saved[item.line - 1].split(None, 1)[1].strip())
        for item in result.own_lines
    ]
    assert _own(result)["user"] == "deploy" and _own(result)["proxyjump"] == "bastion"


def test_a_new_connection_is_appended_where_a_create_appends_it(tmp_path):
    service, _repo, root = _service(
        tmp_path, "Host *\n    User shared\n    Port 2200\n",
    )
    result = service.evaluate_connection_edit(EvaluateConnectionEditRequest(
        create=CreateConnectionRequest(
            nickname="new", hostname="192.0.2.9", username="",
            config_patch={"extra_ssh_config": "Port 2222"},
        ),
    ))
    assert result.host == "new" and _own(result)["hostname"] == "192.0.2.9"
    values = _values(result.evaluation)
    assert values["user"] == "shared"                       # inherited
    assert values["port"] == "2200"                         # Host * comes first
    assert _state(result, "port").state is State.SHADOWED
    assert "new" not in root.read_text()


def test_a_create_whose_name_is_taken_is_refused_as_the_save_would_be(tmp_path):
    service, _repo, _root = _service(tmp_path, "Host h\n    HostName 192.0.2.1\n")
    result = service.evaluate_connection_edit(EvaluateConnectionEditRequest(
        create=CreateConnectionRequest(nickname="h", hostname="192.0.2.2"),
    ))
    assert result.refusal is ConnectionEditRefusal.NAME_TAKEN
    assert result.evaluation is None


def test_an_unknown_connection_is_not_found(tmp_path):
    service, _repo, _root = _service(tmp_path, "Host h\n    HostName 192.0.2.1\n")
    result = service.evaluate_connection_edit(
        EvaluateConnectionEditRequest(connection_id="nope")
    )
    assert result.refusal is ConnectionEditRefusal.NOT_FOUND


def test_without_own_answers_what_the_host_inherits_for_those_keywords(tmp_path):
    service, _repo, _root = _service(
        tmp_path,
        "Host h\n    HostName 192.0.2.1\n    PreferredAuthentications password\n"
        "    Compression no\n\nHost *\n    PreferredAuthentications publickey\n",
    )
    result = service.evaluate_connection_edit(EvaluateConnectionEditRequest(
        connection_id="h", without_own=("preferredauthentications",),
    ))
    assert _values(result.evaluation)["preferredauthentications"] == "password"
    inherited = _values(result.without_own)
    assert inherited["preferredauthentications"] == "publickey"
    assert inherited["compression"] == "no"           # other own lines stay


def test_without_own_is_what_the_configuration_gives_not_settings(tmp_path):
    """The editor's checkboxes show what the configuration gives a host
    that sets no login line; Settings' forced value is reported apart."""
    service, _repo, _root = _service(
        tmp_path,
        "Host h\n    HostName 192.0.2.1\n\nHost *\n    PreferredAuthentications password,publickey\n",
    )
    service._forced_ssh_option_args = lambda: ("-o", "PreferredAuthentications=publickey")
    result = service.evaluate_connection_edit(EvaluateConnectionEditRequest(
        connection_id="h", without_own=("preferredauthentications",),
    ))
    assert _values(result.evaluation)["preferredauthentications"] == "publickey"
    assert _values(result.without_own)["preferredauthentications"] == "password,publickey"


def test_a_shared_host_line_is_evaluated_without_the_save_s_permission(tmp_path):
    service, _repo, _root = _service(
        tmp_path, "Host h other\n    HostName 192.0.2.1\n",
    )
    result = service.evaluate_connection_edit(
        EvaluateConnectionEditRequest(connection_id="h")
    )
    assert result.refusal is None and _own(result)["hostname"] == "192.0.2.1"


def test_the_wire_form_round_trips(tmp_path):
    service, repo, _root = _service(
        tmp_path, "Host *\n    Compression yes\n\nHost h\n    HostName 192.0.2.1\n",
    )
    request = EvaluateConnectionEditRequest(
        connection_id="h",
        update=UpdateConnectionRequest(username="u", expected_generation=1),
        without_own=("user",),
    )
    assert evaluate_connection_edit_request_from_wire(
        evaluate_connection_edit_request_to_wire(request)
    ) == request
    result = service.evaluate_connection_edit(request)
    assert connection_edit_evaluation_from_wire(
        connection_edit_evaluation_to_wire(result)
    ) == result
    refused = ConnectionEditEvaluation(refusal=ConnectionEditRefusal.INVALID)
    assert connection_edit_evaluation_from_wire(
        connection_edit_evaluation_to_wire(refused)
    ) == refused


@pytest.mark.parametrize("kwargs", [
    {},
    {"connection_id": "h", "create": CreateConnectionRequest(nickname="n", hostname="")},
    {"update": UpdateConnectionRequest(username="u")},
    {"connection_id": "h", "without_own": ("not a keyword",)},
])
def test_malformed_requests_are_refused(kwargs):
    with pytest.raises((TypeError, ValueError)):
        EvaluateConnectionEditRequest(**kwargs)


def test_a_newer_edit_supersedes_the_older_one_of_the_same_client():
    from sshpilot.api.transport.envelopes import RequestEnvelope
    from sshpilot.daemon.dispatch import RequestDispatcher

    seen = []

    def evaluate(edit, cancelled):
        seen.append(edit.connection_id)
        return ConnectionEditEvaluation()

    dispatcher = SimpleNamespace(
        _connections=SimpleNamespace(evaluate_connection_edit=evaluate)
    )

    def dispatch(connection_id):
        params = evaluate_connection_edit_request_to_wire(
            EvaluateConnectionEditRequest(connection_id=connection_id)
        )
        envelope = RequestEnvelope(
            "1.0", f"req-{connection_id}", "connections.evaluate_connection_edit", params
        )
        return RequestDispatcher._handle_evaluate_connection_edit(
            dispatcher, envelope, SimpleNamespace(client_id="client:a")
        )

    older, newer = dispatch("one"), dispatch("two")
    with pytest.raises(SshPilotError) as error:
        older.operation()
    assert error.value.code is ErrorCode.OPERATION_CANCELLED
    newer.operation()
    assert seen == ["two"]
