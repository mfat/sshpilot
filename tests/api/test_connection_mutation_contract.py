import time
from dataclasses import fields

import pytest

from sshpilot.api import ErrorCode, EventType, SshPilotError
from sshpilot.api.models import (
    ConnectionMutationResult,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    UpdateConnectionRequest,
)


def _wait_for(events, count=1):
    deadline = time.monotonic() + 2.0
    while len(events) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(events) >= count


def test_create_update_delete_have_shared_behaviour(fake_repo, client_factory):
    client = client_factory(fake_repo)
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

    assert isinstance(created, ConnectionMutationResult)
    assert created.nickname == "new"
    assert events[0].type is EventType.CONNECTION_CREATED
    assert events[0].payload.id == created.connection_id

    old_id = created.connection_id
    updated = client.update_connection(
        old_id,
        UpdateConnectionRequest(
            nickname="renamed",
            hostname="renamed.example",
        ),
    )

    assert updated.connection_id == "renamed"
    assert updated.nickname == "renamed"
    assert updated.changed is True
    assert "nickname" in updated.changed_fields
    assert "hostname" in updated.changed_fields

    noop = client.update_connection(
        updated.connection_id,
        UpdateConnectionRequest(
            nickname="renamed",
            hostname="renamed.example",
        ),
    )
    assert noop.changed is False
    assert noop.changed_fields == ()
    assert noop.generation == updated.generation

    deleted = client.delete_connection(
        DeleteConnectionRequest(connection_id=updated.connection_id)
    )

    assert isinstance(deleted, DeleteConnectionResult)
    assert deleted == DeleteConnectionResult(
        connection_id=updated.connection_id,
        deleted=True,
    )
    subscription.unsubscribe()


def test_duplicate_and_not_found_errors_emit_no_events(fake_repo, client_factory):
    client = client_factory(fake_repo)
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
    missing_id = type(client.list_connections()[0].id)("missing")
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


def test_failed_mutations_emit_no_events(fake_repo, client_factory, monkeypatch):
    fake_repo.fail_next = True
    client = client_factory(fake_repo)
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
    fake_repo,
    client_factory,
):
    client = client_factory(fake_repo)
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

    details = client.get_connection(created.connection_id)
    details_fields = {field.name for field in fields(details)}
    assert "password" not in details_fields
    assert "passphrase" not in details_fields


def test_basic_update_preserves_advanced_state_without_passing_secrets(
    fake_repo,
    client_factory,
):
    fake_repo.update_connection("demo", {
        "keyfile": "/private/key-path",
        "password": "must-not-reach-manager-update",
        "token": "must-not-reach-manager-update",
    })
    client = client_factory(fake_repo)
    connection_id = client.list_connections()[0].id

    updated = client.update_connection(
        connection_id,
        UpdateConnectionRequest(username="changed"),
    )

    details = client.get_connection(updated.connection_id)
    assert details.identity_configured is True
    assert fake_repo.last_update["keyfile"] == "/private/key-path"
