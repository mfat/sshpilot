"""Strict event-payload validation for the connection-store change event."""

import pytest

from sshpilot.api import EventType
from sshpilot.api.events import (
    CoreEvent,
    EventPublisher,
    _validate_dto_value,
    validate_event_payload,
)
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.connection_store import (
    ConnectionMetadataSummary,
    ConnectionStoreSnapshot,
    GroupId,
    GroupSummary,
)
from sshpilot.api.models.connections import ConnectionSummary


def _connection(conn_id="conn-1"):
    return ConnectionSummary(
        id=ConnectionId(conn_id),
        nickname=conn_id,
        host=conn_id,
        hostname=f"{conn_id}.example",
        username="user",
        port=22,
    )


def _snapshot():
    return ConnectionStoreSnapshot(
        generation=1,
        connections=(_connection(),),
        groups=(
            GroupSummary(
                id=GroupId("group-1"),
                name="Servers",
                order=0,
                color="",
                connection_ids=(ConnectionId("conn-1"),),
            ),
        ),
        root_connection_ids=(),
        metadata=(
            ConnectionMetadataSummary(
                connection_id=ConnectionId("conn-1"),
                values={"tags": ["web"]},
            ),
        ),
    )


def test_store_changed_accepts_exact_snapshot():
    snapshot = _snapshot()
    validated = validate_event_payload(
        EventType.CONNECTION_STORE_CHANGED, snapshot
    )
    assert validated is snapshot


def test_store_changed_accepts_frozen_tag_tuples():
    snapshot = ConnectionStoreSnapshot(
        generation=1,
        connections=(_connection(),),
        groups=(),
        root_connection_ids=(ConnectionId("conn-1"),),
        metadata=(
            ConnectionMetadataSummary(
                connection_id=ConnectionId("conn-1"),
                values={"tags": ("web",)},
            ),
        ),
    )
    assert snapshot.metadata[0].values["tags"] == ("web",)
    assert (
        validate_event_payload(EventType.CONNECTION_STORE_CHANGED, snapshot)
        is snapshot
    )


def test_store_changed_accepts_nested_immutable_metadata():
    snapshot = ConnectionStoreSnapshot(
        generation=1,
        connections=(_connection(),),
        groups=(),
        root_connection_ids=(ConnectionId("conn-1"),),
        metadata=(
            ConnectionMetadataSummary(
                connection_id=ConnectionId("conn-1"),
                values={
                    "tags": ("web", "prod"),
                    "nested": {"deep": ("a", "b")},
                },
            ),
        ),
    )
    assert (
        validate_event_payload(EventType.CONNECTION_STORE_CHANGED, snapshot)
        is snapshot
    )


def test_dto_mapping_rejects_arbitrary_object_values():
    with pytest.raises(TypeError, match="unsupported public value type"):
        _validate_dto_value(
            {"nested": {"bad": object()}},
            path="connection_store.changed.payload",
        )


def test_dto_mapping_rejects_non_string_keys():
    with pytest.raises(TypeError, match="keys must be strings"):
        _validate_dto_value(
            {"nested": {123: "value"}},
            path="connection_store.changed.payload",
        )


def test_store_changed_rejects_arbitrary_dictionary():
    with pytest.raises(TypeError):
        validate_event_payload(
            EventType.CONNECTION_STORE_CHANGED,
            {"generation": 1, "connections": [], "groups": []},
        )


def test_store_changed_rejects_list_payload():
    with pytest.raises(TypeError):
        validate_event_payload(EventType.CONNECTION_STORE_CHANGED, [])


def test_store_changed_rejects_other_dto_payload():
    with pytest.raises(TypeError):
        validate_event_payload(EventType.CONNECTION_STORE_CHANGED, _connection())


def test_store_changed_rejects_unsafe_metadata_in_payload():
    # The snapshot model validates its metadata at construction, so an event
    # carrying secret-like metadata can never be built.
    with pytest.raises(ValueError):
        ConnectionMetadataSummary(
            connection_id=ConnectionId("conn-1"),
            values={"api_password": "x"},
        )


def test_error_occurred_still_rejects_sensitive_detail_keys():
    # The ERROR_OCCURRED envelope keeps its own strict sensitive-key filter;
    # permissive DTO mappings must not weaken error-detail filtering.
    with pytest.raises(ValueError, match="disallowed sensitive detail key"):
        validate_event_payload(
            EventType.ERROR_OCCURRED,
            {
                "code": "internal_error",
                "message": "Operation failed",
                "details": {"password": "must-not-cross-wire"},
            },
        )


def test_core_event_constructs_with_store_snapshot():
    event = CoreEvent(
        type=EventType.CONNECTION_STORE_CHANGED,
        payload=_snapshot(),
        sequence=0,
    )
    assert event.type is EventType.CONNECTION_STORE_CHANGED


def test_publisher_delivers_store_changed_event():
    publisher = EventPublisher()
    received = []
    publisher.subscribe(received.append)
    snapshot = _snapshot()
    event = publisher.publish(EventType.CONNECTION_STORE_CHANGED, snapshot)
    assert received == [event]
    assert event.payload == snapshot


def test_legacy_connection_events_remain_valid():
    summary = _connection("legacy")
    for event_type in (
        EventType.CONNECTION_CREATED,
        EventType.CONNECTION_UPDATED,
        EventType.CONNECTION_DELETED,
    ):
        assert validate_event_payload(event_type, summary) is summary


def test_unknown_event_type_rejected():
    with pytest.raises(TypeError):
        validate_event_payload("connection_store.changed", _snapshot())
