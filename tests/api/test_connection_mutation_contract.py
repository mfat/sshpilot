import time
from dataclasses import fields

import pytest

from sshpilot.api import ErrorCode, EventType, SshPilotError
from sshpilot.api.models import (
    ConnectionDetails,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    UpdateConnectionRequest,
)
from sshpilot.connection_identity import transitional_connection_id


def _wait_for(events, count=1):
    deadline = time.monotonic() + 2.0
    while len(events) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(events) >= count


def test_create_update_delete_have_shared_behaviour(fake_manager, client_factory):
    client = client_factory(fake_manager)
    events = []

    def _record(event):
        events.append(event)

    subscription = client.subscribe_events(_record)
    created = client.create_connection(
        CreateConnectionRequest(
            nickname="new",
            hostname="new.example",
            username="bob",
            port=2202,
        )
    )
    _wait_for(events)

    assert isinstance(created, ConnectionDetails)
    assert created.nickname == "new"
    assert events[0].type is EventType.CONNECTION_CREATED
    assert events[0].payload.id == created.id

    old_id = created.id
    updated = client.update_connection(
        old_id,
        UpdateConnectionRequest(
            nickname="renamed",
            hostname="renamed.example",
        ),
    )
    _wait_for(events, 2)

    assert updated.id == old_id
    assert updated.nickname == "renamed"
    assert events[1].type is EventType.CONNECTION_UPDATED
    assert events[1].payload.id == updated.id

    deleted = client.delete_connection(
        DeleteConnectionRequest(connection_id=updated.id)
    )
    _wait_for(events, 3)

    assert isinstance(deleted, DeleteConnectionResult)
    assert deleted == DeleteConnectionResult(
        connection_id=updated.id,
        deleted=True,
    )
    assert events[2].type is EventType.CONNECTION_DELETED
    assert events[2].payload.id == updated.id
    assert [event.type for event in events] == [
        EventType.CONNECTION_CREATED,
        EventType.CONNECTION_UPDATED,
        EventType.CONNECTION_DELETED,
    ]
    subscription.unsubscribe()


def test_duplicate_and_not_found_errors_emit_no_events(fake_manager, client_factory):
    client = client_factory(fake_manager)
    events = []
    subscription = client.subscribe_events(events.append)

    with pytest.raises(SshPilotError) as duplicate:
        client.create_connection(
            CreateConnectionRequest(
                nickname="demo",
                hostname="duplicate.example",
            )
        )
    with pytest.raises(SshPilotError) as duplicate_case:
        client.create_connection(
            CreateConnectionRequest(
                nickname="DEMO",
                hostname="duplicate-case.example",
            )
        )
    missing_id = type(client.list_connections()[0].id)("connection:v1:missing")
    with pytest.raises(SshPilotError) as update_missing:
        client.update_connection(
            missing_id,
            UpdateConnectionRequest(username="nobody"),
        )
    with pytest.raises(SshPilotError) as delete_missing:
        client.delete_connection(
            DeleteConnectionRequest(connection_id=missing_id)
        )

    assert duplicate.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert duplicate_case.value.code is ErrorCode.CONNECTION_ALREADY_EXISTS
    assert update_missing.value.code is ErrorCode.CONNECTION_NOT_FOUND
    assert delete_missing.value.code is ErrorCode.CONNECTION_NOT_FOUND
    assert events == []
    subscription.unsubscribe()


def test_transitional_id_is_input_only_for_update(
    fake_manager,
    fake_connection,
    client_factory,
):
    client = client_factory(fake_manager)
    canonical_id = client.list_connections()[0].id
    legacy_id = type(canonical_id)(
        transitional_connection_id(
            fake_connection.protocol,
            fake_connection.nickname,
        )
    )

    updated = client.update_connection(
        legacy_id,
        UpdateConnectionRequest(username="updated-user"),
    )

    assert updated.id == canonical_id
    assert updated.id != legacy_id


def test_failed_mutations_emit_no_events(fake_manager, client_factory, monkeypatch):
    monkeypatch.setattr(fake_manager, "create_connection", lambda _data: None)
    client = client_factory(fake_manager)
    events = []
    subscription = client.subscribe_events(events.append)

    with pytest.raises(SshPilotError) as caught:
        client.create_connection(
            CreateConnectionRequest(
                nickname="new",
                hostname="new.example",
            )
        )

    assert caught.value.code is ErrorCode.PERSISTENCE_FAILED
    assert events == []
    subscription.unsubscribe()


def test_mutation_dtos_and_results_exclude_secret_fields(
    fake_manager,
    client_factory,
):
    client = client_factory(fake_manager)
    created = client.create_connection(
        CreateConnectionRequest(
            nickname="public",
            hostname="public.example",
        )
    )

    public_fields = {field.name for field in fields(created)}
    assert "password" not in public_fields
    assert "passphrase" not in public_fields
    assert "secret" not in repr(created).lower()


def test_basic_update_preserves_advanced_state_without_passing_secrets(
    fake_manager,
    fake_connection,
    client_factory,
):
    fake_connection.keyfile = "/private/key-path"
    fake_connection.data.update(
        {
            "keyfile": fake_connection.keyfile,
            "password": "must-not-reach-manager-update",
            "token": "must-not-reach-manager-update",
        }
    )
    client = client_factory(fake_manager)
    connection_id = client.list_connections()[0].id

    updated = client.update_connection(
        connection_id,
        UpdateConnectionRequest(username="changed"),
    )

    assert updated.identity_configured is True
    assert fake_manager.last_update["keyfile"] == "/private/key-path"
    assert "password" not in fake_manager.last_update
    assert "token" not in fake_manager.last_update
