from dataclasses import fields

import pytest

from sshpilot.api import ErrorCode, SshPilotError
from sshpilot.api.models import (
    AuthenticationMethod,
    ConnectionDetails,
    ConnectionEditorDetails,
    ConnectionSummary,
)
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.transport.codec import (
    connection_editor_details_from_wire,
    connection_editor_details_to_wire,
)


def test_list_and_get_connections_preserve_order_and_identity(
    fake_repo,
    client_factory,
):
    fake_repo.create_connection(
        {
            "nickname": "second",
            "hostname": "second.example",
            "username": "bob",
            "port": 2202,
        }
    )
    fake_repo.create_group("Production")
    fake_repo.assign_connection_to_group("demo", "production")
    client = client_factory(fake_repo)

    summaries = client.list_connections()
    details = client.get_connection(summaries[0].id)

    assert [item.nickname for item in summaries] == ["demo", "second"]
    assert all(isinstance(item, ConnectionSummary) for item in summaries)
    assert isinstance(details, ConnectionDetails)
    assert details.id == summaries[0].id
    assert details.groups[0].name == "Production"


def test_unknown_connection_has_structured_not_found_error(fake_repo, client_factory):
    client = client_factory(fake_repo)
    missing_id = ConnectionId("nonexistent")

    with pytest.raises(SshPilotError) as caught:
        client.get_connection(missing_id)

    assert caught.value.code is ErrorCode.CONNECTION_NOT_FOUND
    assert caught.value.connection_id == missing_id


def test_connection_dtos_never_return_internal_models_or_secrets(
    fake_repo,
    client_factory,
):
    client = client_factory(fake_repo)
    record = fake_repo.get_record("demo")
    summary = client.list_connections()[0]
    details = client.get_connection(summary.id)
    public_fields = {item.name for item in fields(details)}

    assert summary is not record
    assert details is not record
    assert "password" not in public_fields
    assert "key_passphrase" not in public_fields
    assert "do-not-expose" not in repr(details)
    assert "do-not-expose-either" not in repr(details)


def test_existing_connection_id_is_stable_across_equivalent_reload(
    fake_repo,
    client_factory,
):
    client = client_factory(fake_repo)
    first_id = client.list_connections()[0].id
    fake_repo.update_connection(
        "demo",
        {"hostname": "changed.example", "username": "different"},
    )

    assert client.list_connections()[0].id == first_id


def test_connection_id_matches_nickname(
    fake_repo,
    client_factory,
):
    client = client_factory(fake_repo)

    fake_repo.update_connection("demo", {"nickname": "renamed"})

    assert client.list_connections()[0].id == "renamed"


def test_editor_details_codec_preserves_display_name():
    details = ConnectionEditorDetails(
        id=ConnectionId("prod"),
        nickname="prod",
        host="prod",
        hostname="server.example",
        username="deploy",
        port=22,
        display_name="Production Server",
        aliases=(),
        authentication_method=AuthenticationMethod.KEY,
    )

    decoded = connection_editor_details_from_wire(
        connection_editor_details_to_wire(details)
    )

    assert decoded.display_name == "Production Server"
