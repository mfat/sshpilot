from dataclasses import fields

import pytest

from sshpilot.api import ErrorCode, SshPilotError
from sshpilot.api.models import ConnectionDetails, ConnectionSummary
from sshpilot.api.models.common import ConnectionId


def test_list_and_get_connections_preserve_order_and_identity(
    fake_manager,
    fake_connection,
    client_factory,
    group_manager,
):
    second = type(fake_connection)(
        nickname="second",
        hostname="second.example",
        username="bob",
        port=2202,
    )
    fake_manager.connections.append(second)
    client = client_factory(fake_manager, group_manager=group_manager)

    summaries = client.list_connections()
    details = client.get_connection(summaries[0].id)

    assert [item.nickname for item in summaries] == ["demo", "second"]
    assert all(isinstance(item, ConnectionSummary) for item in summaries)
    assert isinstance(details, ConnectionDetails)
    assert details.id == summaries[0].id
    assert details.groups[0].name == "Production"
    assert client.connection_id_for(fake_connection) == summaries[0].id


def test_unknown_connection_has_structured_not_found_error(fake_manager, client_factory):
    client = client_factory(fake_manager)
    missing_id = ConnectionId("connection:v1:missing")

    with pytest.raises(SshPilotError) as caught:
        client.get_connection(missing_id)

    assert caught.value.code is ErrorCode.CONNECTION_NOT_FOUND
    assert caught.value.connection_id == missing_id
    assert "missing" not in caught.value.message


def test_connection_dtos_never_return_internal_models_or_secrets(
    fake_manager,
    fake_connection,
    client_factory,
):
    client = client_factory(fake_manager)
    summary = client.list_connections()[0]
    details = client.get_connection(summary.id)
    public_fields = {item.name for item in fields(details)}

    assert summary is not fake_connection
    assert details is not fake_connection
    assert "password" not in public_fields
    assert "key_passphrase" not in public_fields
    assert "do-not-expose" not in repr(details)
    assert "do-not-expose-either" not in repr(details)


def test_existing_connection_id_is_stable_across_equivalent_reload(
    fake_manager,
    fake_connection,
    client_factory,
):
    client = client_factory(fake_manager)
    first_id = client.list_connections()[0].id
    replacement = type(fake_connection)(
        nickname=fake_connection.nickname,
        hostname="changed.example",
        username="different",
    )
    fake_manager.connections[:] = [replacement]

    assert client.list_connections()[0].id == first_id

