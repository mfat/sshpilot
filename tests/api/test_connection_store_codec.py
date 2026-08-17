"""Connection-store transport codec round-trip and strictness tests."""

import pytest

from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.connection_store import (
    AddTagToConnectionsRequest,
    ConnectionMetadataSummary,
    ConnectionPlacementMode,
    ConnectionStoreSnapshot,
    CopyConnectionToGroupRequest,
    MoveConnectionsRequest,
    GroupId,
    GroupSummary,
    PlaceGroupRequest,
    RemoveConnectionFromGroupRequest,
    RenameTagRequest,
    ReorderConnectionRequest,
    SetGroupColorRequest,
)
from sshpilot.api.models.connections import (
    AssignConnectionToGroupRequest,
    ConnectionSummary,
    UpdateConnectionMetadataRequest,
)
from sshpilot.api.transport.codec import (
    connection_metadata_summary_from_wire,
    connection_metadata_summary_to_wire,
    connection_store_snapshot_from_wire,
    connection_store_snapshot_to_wire,
    add_tag_to_connections_request_from_wire,
    add_tag_to_connections_request_to_wire,
    assign_connection_to_group_request_from_wire,
    assign_connection_to_group_request_to_wire,
    copy_connection_to_group_request_from_wire,
    copy_connection_to_group_request_to_wire,
    move_connections_request_from_wire,
    move_connections_request_to_wire,
    group_summary_from_wire,
    group_summary_to_wire,
    place_group_request_from_wire,
    place_group_request_to_wire,
    remove_connection_from_group_request_from_wire,
    remove_connection_from_group_request_to_wire,
    rename_tag_request_from_wire,
    rename_tag_request_to_wire,
    reorder_connection_request_from_wire,
    reorder_connection_request_to_wire,
    set_group_color_request_from_wire,
    set_group_color_request_to_wire,
    update_connection_metadata_request_from_wire,
    update_connection_metadata_request_to_wire,
)


def _connection(conn_id="conn-1"):
    return ConnectionSummary(
        id=ConnectionId(conn_id),
        nickname=conn_id,
        host=conn_id,
        hostname=f"{conn_id}.example",
        username="user",
        port=22,
    )


def _group(group_id="group-1", parent_id=None):
    return GroupSummary(
        id=GroupId(group_id),
        name="Group One",
        parent_id=GroupId(parent_id) if parent_id else None,
        order=1,
        color="#ff0000",
        connection_ids=(ConnectionId("conn-1"),),
    )


def _metadata():
    return ConnectionMetadataSummary(
        connection_id=ConnectionId("conn-1"),
        values={"tags": ["web"], "pinned": True, "wol": {"mac": "aa:bb"}},
    )


def _snapshot():
    return ConnectionStoreSnapshot(
        generation=3,
        connections=(_connection(), _connection("conn-2")),
        groups=(_group(),),
        root_connection_ids=(ConnectionId("conn-2"),),
        metadata=(_metadata(),),
    )


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------
def test_group_summary_round_trip():
    group = _group()
    assert group_summary_from_wire(group_summary_to_wire(group)) == group


def test_group_summary_round_trip_without_parent():
    group = _group(parent_id=None)
    assert group_summary_from_wire(group_summary_to_wire(group)) == group


def test_metadata_summary_round_trip():
    meta = _metadata()
    assert (
        connection_metadata_summary_from_wire(
            connection_metadata_summary_to_wire(meta)
        )
        == meta
    )


def test_update_connection_metadata_request_round_trip():
    request = UpdateConnectionMetadataRequest(
        connection_id=ConnectionId("conn-1"),
        meta={"tags": ["web", "prod"], "pinned": True, "wol": {"mac": "aa:bb"}},
    )
    assert (
        update_connection_metadata_request_from_wire(
            update_connection_metadata_request_to_wire(request)
        )
        == request
    )


def test_update_connection_metadata_request_wire_is_transport_safe():
    request = UpdateConnectionMetadataRequest(
        connection_id=ConnectionId("conn-1"),
        meta={"tags": ["web", "prod"], "wol": {"mac": "aa:bb"}},
    )
    encoded = update_connection_metadata_request_to_wire(request)
    assert type(encoded["meta"]) is dict
    assert type(encoded["meta"]["tags"]) is list
    assert type(encoded["meta"]["wol"]) is dict


def test_snapshot_round_trip():
    snapshot = _snapshot()
    assert (
        connection_store_snapshot_from_wire(
            connection_store_snapshot_to_wire(snapshot)
        )
        == snapshot
    )


def test_set_group_color_round_trip():
    request = SetGroupColorRequest(group_id=GroupId("group-1"), color="#00ff00")
    assert (
        set_group_color_request_from_wire(
            set_group_color_request_to_wire(request)
        )
        == request
    )


def test_move_connections_round_trip():
    request = MoveConnectionsRequest(
        connection_ids=(ConnectionId("a"), ConnectionId("b")),
        target_group_id=GroupId("prod"),
        target_connection_id=ConnectionId("c"),
        position="above",
        expected_generation=4,
    )
    assert move_connections_request_from_wire(
        move_connections_request_to_wire(request)
    ) == request


def test_preserving_move_and_atomic_tag_round_trip():
    move = MoveConnectionsRequest(
        connection_ids=(ConnectionId("a"), ConnectionId("b")),
        source_group_id=GroupId("web"),
        target_group_id=GroupId("web"),
        target_connection_id=ConnectionId("c"),
        position="above",
        expected_generation=4,
        mode=ConnectionPlacementMode.PRESERVE,
    )
    assert move_connections_request_from_wire(move_connections_request_to_wire(move)) == move
    tag = AddTagToConnectionsRequest(
        connection_ids=(ConnectionId("a"), ConnectionId("b")),
        tag="prod",
        expected_generation=4,
    )
    assert add_tag_to_connections_request_from_wire(
        add_tag_to_connections_request_to_wire(tag)
    ) == tag


def test_move_wire_includes_explicit_semantics():
    encoded = move_connections_request_to_wire(
        MoveConnectionsRequest(
            connection_ids=(ConnectionId("a"),),
            source_group_id=GroupId("web"),
            target_group_id=GroupId("web"),
            mode=ConnectionPlacementMode.PRESERVE,
        )
    )
    assert encoded["mode"] == "preserve"
    assert encoded["source_group_id"] == "web"


def test_place_group_round_trip():
    request = PlaceGroupRequest(
        group_id=GroupId("group-1"),
        parent_id=GroupId("group-0"),
        index=2,
        expected_generation=4,
    )
    assert place_group_request_from_wire(place_group_request_to_wire(request)) == request


def test_place_group_round_trip_root():
    request = PlaceGroupRequest(group_id=GroupId("group-1"), parent_id=None, index=0)
    assert place_group_request_from_wire(place_group_request_to_wire(request)) == request


def test_place_group_wire_includes_generation():
    encoded = place_group_request_to_wire(
        PlaceGroupRequest(
            group_id=GroupId("g"),
            parent_id=GroupId("p"),
            index=1,
            expected_generation=9,
        )
    )
    assert encoded["expected_generation"] == 9


def test_place_group_wire_omits_null_generation():
    encoded = place_group_request_to_wire(
        PlaceGroupRequest(group_id=GroupId("g"), parent_id=None, index=0)
    )
    assert "expected_generation" not in encoded


def test_copy_connection_round_trip():
    request = CopyConnectionToGroupRequest(
        connection_id=ConnectionId("conn-1"), group_id=GroupId("group-1")
    )
    assert (
        copy_connection_to_group_request_from_wire(
            copy_connection_to_group_request_to_wire(request)
        )
        == request
    )


def test_assign_connection_to_group_round_trip():
    request = AssignConnectionToGroupRequest(
        connection_id=ConnectionId("conn-1"), group_id="group-b"
    )
    assert (
        assign_connection_to_group_request_from_wire(
            assign_connection_to_group_request_to_wire(request)
        )
        == request
    )


def test_assign_connection_to_group_wire_omits_empty_group_id():
    # group_id="" means "move to root"; the wire form must not carry a
    # spurious empty key, and decoding it back must still round-trip.
    request = AssignConnectionToGroupRequest(
        connection_id=ConnectionId("conn-1"), group_id=""
    )
    encoded = assign_connection_to_group_request_to_wire(request)
    assert "group_id" not in encoded
    assert assign_connection_to_group_request_from_wire(encoded) == request


def test_remove_connection_round_trip():
    request = RemoveConnectionFromGroupRequest(
        connection_id=ConnectionId("conn-1"), group_id=GroupId("group-1")
    )
    assert (
        remove_connection_from_group_request_from_wire(
            remove_connection_from_group_request_to_wire(request)
        )
        == request
    )


def test_reorder_connection_round_trip():
    request = ReorderConnectionRequest(
        connection_id=ConnectionId("conn-1"),
        target_connection_id=ConnectionId("conn-2"),
        group_id=GroupId("group-1"),
        position="above",
    )
    assert (
        reorder_connection_request_from_wire(
            reorder_connection_request_to_wire(request)
        )
        == request
    )


def test_reorder_connection_round_trip_root():
    request = ReorderConnectionRequest(
        connection_id=ConnectionId("conn-1"),
        target_connection_id=ConnectionId("conn-2"),
        group_id=None,
        position="below",
    )
    assert (
        reorder_connection_request_from_wire(
            reorder_connection_request_to_wire(request)
        )
        == request
    )


def test_rename_tag_round_trip():
    request = RenameTagRequest(old_tag="web", new_tag="production")
    assert rename_tag_request_from_wire(rename_tag_request_to_wire(request)) == request


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------
def test_group_summary_wire_shape():
    encoded = group_summary_to_wire(_group(parent_id="group-0"))
    assert set(encoded) == {
        "id",
        "name",
        "parent_id",
        "order",
        "color",
        "connection_ids",
    }
    assert type(encoded["connection_ids"]) is list


def test_group_summary_wire_omits_null_parent():
    encoded = group_summary_to_wire(_group(parent_id=None))
    assert "parent_id" not in encoded


def test_metadata_summary_wire_shape():
    encoded = connection_metadata_summary_to_wire(_metadata())
    assert set(encoded) == {"connection_id", "values"}
    assert type(encoded["values"]) is dict


def test_snapshot_wire_shape():
    encoded = connection_store_snapshot_to_wire(_snapshot())
    assert set(encoded) == {
        "generation",
        "connections",
        "groups",
        "root_connection_ids",
        "metadata",
    }
    assert type(encoded["connections"]) is list
    assert type(encoded["groups"]) is list
    assert type(encoded["root_connection_ids"]) is list
    assert type(encoded["metadata"]) is list


def test_reorder_wire_omits_null_group():
    encoded = reorder_connection_request_to_wire(
        ReorderConnectionRequest(
            connection_id=ConnectionId("conn-1"),
            target_connection_id=ConnectionId("conn-2"),
            group_id=None,
            position="below",
        )
    )
    assert "group_id" not in encoded


def test_place_group_wire_omits_null_parent():
    encoded = place_group_request_to_wire(
        PlaceGroupRequest(group_id=GroupId("g"), parent_id=None, index=0)
    )
    assert "parent_id" not in encoded


# ---------------------------------------------------------------------------
# Strict rejection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "decoder,payload",
    [
        (group_summary_from_wire, {}),
        (group_summary_from_wire, {"id": "a", "name": "n", "order": 0, "color": "", "connection_ids": [], "extra": 1}),
        (group_summary_from_wire, {"id": "", "name": "n", "order": 0, "color": "", "connection_ids": []}),
        (group_summary_from_wire, {"id": "a", "name": "", "order": 0, "color": "", "connection_ids": []}),
        (group_summary_from_wire, {"id": "a", "name": "n", "order": -1, "color": "", "connection_ids": []}),
        (group_summary_from_wire, {"id": "a", "name": "n", "order": 0, "color": "", "connection_ids": {}}),
        (group_summary_from_wire, {"id": "a", "name": "n", "order": 0, "color": "", "connection_ids": ["a", "a"]}),
        (group_summary_from_wire, {"id": "a", "name": "n", "order": 0, "color": "", "connection_ids": [], "parent_id": 5}),
        (connection_metadata_summary_from_wire, {}),
        (connection_metadata_summary_from_wire, {"connection_id": "c"}),
        (connection_metadata_summary_from_wire, {"connection_id": "c", "values": []}),
        (connection_metadata_summary_from_wire, {"connection_id": "c", "values": {"api_password": "x"}}),
        (connection_store_snapshot_from_wire, {"generation": 0}),
        (connection_store_snapshot_from_wire, {"generation": 0, "connections": [], "groups": [], "root_connection_ids": [], "metadata": [], "extra": 1}),
        (connection_store_snapshot_from_wire, {"generation": -1, "connections": [], "groups": [], "root_connection_ids": [], "metadata": []}),
        (connection_store_snapshot_from_wire, {"generation": 0, "connections": {}, "groups": [], "root_connection_ids": [], "metadata": []}),
        (set_group_color_request_from_wire, {}),
        (set_group_color_request_from_wire, {"group_id": "g"}),
        (place_group_request_from_wire, {}),
        (place_group_request_from_wire, {"group_id": "g", "index": -1}),
        (place_group_request_from_wire, {"group_id": "g", "index": "1"}),
        (place_group_request_from_wire, {"group_id": "g", "index": 0, "expected_generation": "4"}),
        (place_group_request_from_wire, {"group_id": "g", "index": 0, "expected_generation": -1}),
        (place_group_request_from_wire, {"group_id": "g", "index": 0, "expected_generation": True}),
        (copy_connection_to_group_request_from_wire, {"connection_id": "c"}),
        (remove_connection_from_group_request_from_wire, {"group_id": "g"}),
        (reorder_connection_request_from_wire, {"connection_id": "a", "target_connection_id": "b"}),
        (reorder_connection_request_from_wire, {"connection_id": "a", "target_connection_id": "b", "position": "left"}),
        (rename_tag_request_from_wire, {"old_tag": ""}),
        (rename_tag_request_from_wire, {"old_tag": "a", "new_tag": " "}),
        (update_connection_metadata_request_from_wire, {}),
        (update_connection_metadata_request_from_wire, {"connection_id": "c"}),
        (update_connection_metadata_request_from_wire, {"connection_id": "c", "meta": []}),
        (update_connection_metadata_request_from_wire, {"connection_id": "c", "meta": {"tags": []}, "extra": 1}),
    ],
)
def test_connection_store_codecs_reject_malformed_wire(decoder, payload):
    with pytest.raises(ValueError):
        decoder(payload)


def test_snapshot_decoder_rejects_non_summary_members():
    with pytest.raises(ValueError):
        connection_store_snapshot_from_wire(
            {
                "generation": 0,
                "connections": [{"nope": 1}],
                "groups": [],
                "root_connection_ids": [],
                "metadata": [],
            }
        )


def test_snapshot_decoder_rejects_non_group_members():
    with pytest.raises(ValueError):
        connection_store_snapshot_from_wire(
            {
                "generation": 0,
                "connections": [],
                "groups": [{"id": "g"}],
                "root_connection_ids": [],
                "metadata": [],
            }
        )


def test_snapshot_decoder_rejects_non_metadata_members():
    with pytest.raises(ValueError):
        connection_store_snapshot_from_wire(
            {
                "generation": 0,
                "connections": [_connection()],
                "groups": [],
                "root_connection_ids": [],
                "metadata": [{"connection_id": "c"}],
            }
        )


def test_snapshot_decoder_rejects_duplicate_group_ids():
    wire = {
        "generation": 0,
        "connections": [],
        "groups": [
            {"id": "g", "name": "n", "order": 0, "color": "", "connection_ids": []},
            {"id": "g", "name": "n", "order": 0, "color": "", "connection_ids": []},
        ],
        "root_connection_ids": [],
        "metadata": [],
    }
    with pytest.raises(ValueError):
        connection_store_snapshot_from_wire(wire)


def test_snapshot_decoder_rejects_duplicate_connection_ids():
    conn_wire = {
        "id": "conn-1",
        "nickname": "conn-1",
        "host": "conn-1",
        "hostname": "conn-1.example",
        "username": "user",
        "port": 22,
        "protocol": "ssh",
        "health": "unknown",
        "groups": [],
    }
    wire = {
        "generation": 0,
        "connections": [conn_wire, conn_wire],
        "groups": [],
        "root_connection_ids": [],
        "metadata": [],
    }
    with pytest.raises(ValueError):
        connection_store_snapshot_from_wire(wire)


def test_snapshot_decoder_rejects_unknown_root_connection():
    wire = {
        "generation": 0,
        "connections": [],
        "groups": [],
        "root_connection_ids": ["missing"],
        "metadata": [],
    }
    with pytest.raises(ValueError):
        connection_store_snapshot_from_wire(wire)


def test_snapshot_decoder_rejects_group_parent_cycle():
    wire = {
        "generation": 0,
        "connections": [],
        "groups": [
            {"id": "group-a", "name": "A", "parent_id": "group-b", "order": 0, "color": "", "connection_ids": []},
            {"id": "group-b", "name": "B", "parent_id": "group-a", "order": 0, "color": "", "connection_ids": []},
        ],
        "root_connection_ids": [],
        "metadata": [],
    }
    with pytest.raises(ValueError):
        connection_store_snapshot_from_wire(wire)


def test_metadata_decoder_rejects_oversized_values():
    wire = {"connection_id": "c", "values": {"data": "x" * (256 * 1024)}}
    with pytest.raises(ValueError):
        connection_metadata_summary_from_wire(wire)


def test_metadata_decoder_rejects_secret_like_nested_keys():
    wire = {"connection_id": "c", "values": {"nested": {"token": "abc"}}}
    with pytest.raises(ValueError):
        connection_metadata_summary_from_wire(wire)


def test_metadata_decoder_rejects_non_json_safe_value():
    with pytest.raises(TypeError):
        connection_metadata_summary_from_wire(
            {"connection_id": "c", "values": {"ok": object()}}
        )


# ---------------------------------------------------------------------------
# Type guards on encoding
# ---------------------------------------------------------------------------
def test_encoders_reject_wrong_object_types():
    with pytest.raises(TypeError):
        group_summary_to_wire(object())
    with pytest.raises(TypeError):
        connection_metadata_summary_to_wire(object())
    with pytest.raises(TypeError):
        connection_store_snapshot_to_wire(object())
    with pytest.raises(TypeError):
        set_group_color_request_to_wire(object())
    with pytest.raises(TypeError):
        place_group_request_to_wire(object())
    with pytest.raises(TypeError):
        copy_connection_to_group_request_to_wire(object())
    with pytest.raises(TypeError):
        remove_connection_from_group_request_to_wire(object())
    with pytest.raises(TypeError):
        reorder_connection_request_to_wire(object())
    with pytest.raises(TypeError):
        rename_tag_request_to_wire(object())
