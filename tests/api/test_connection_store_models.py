"""Connection-store protocol model construction and validation tests."""

import pytest

from sshpilot.api.models.common import ConnectionId, require_identifier
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
    thaw_safe_metadata,
    validate_safe_metadata,
)
from sshpilot.api.models.connections import (
    ConnectionSummary,
    UpdateConnectionMetadataRequest,
)


def _conn(connection_id="web", nickname="web", hostname="example.com"):
    return ConnectionSummary(
        id=ConnectionId(connection_id),
        nickname=nickname,
        host=connection_id,
        hostname=hostname,
        username="alice",
        port=22,
    )


def _group(group_id="prod", name="Production", **kwargs):
    return GroupSummary(
        id=GroupId(group_id),
        name=name,
        connection_ids=kwargs.pop("connection_ids", ()),
        **kwargs,
    )


def _snapshot(*, generation=0, connections=None, groups=None,
              root_connection_ids=None, metadata=None):
    connections = tuple(connections or (_conn(),))
    groups = tuple(groups or ())
    root_connection_ids = tuple(
        root_connection_ids
        if root_connection_ids is not None
        else (c.id for c in connections if c.id == "web")
    )
    metadata = tuple(metadata or ())
    return ConnectionStoreSnapshot(
        generation=generation,
        connections=connections,
        groups=groups,
        root_connection_ids=root_connection_ids,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# GroupSummary
# ---------------------------------------------------------------------------
def test_group_summary_constructs():
    group = _group("prod", "Production", parent_id=GroupId("top"), order=1, color="#ff0000")
    assert group.id == "prod"
    assert group.name == "Production"
    assert group.parent_id == "top"
    assert group.order == 1
    assert group.color == "#ff0000"


def test_group_summary_rejects_empty_id():
    with pytest.raises(ValueError):
        _group("", "x")


def test_group_summary_rejects_empty_name():
    with pytest.raises(ValueError):
        _group("prod", "  ")


def test_group_summary_rejects_non_tuple_connection_ids():
    with pytest.raises(TypeError):
        _group("prod", "Production", connection_ids=["web"])


def test_group_summary_rejects_duplicate_connection_ids():
    with pytest.raises(ValueError):
        _group("prod", "Production", connection_ids=("web", "web"))


def test_group_summary_rejects_self_parent():
    with pytest.raises(ValueError):
        _group("prod", "Production", parent_id=GroupId("prod"))


def test_group_summary_rejects_negative_order():
    with pytest.raises(ValueError):
        _group("prod", "Production", order=-1)


def test_group_summary_rejects_bool_order():
    with pytest.raises(ValueError):
        _group("prod", "Production", order=True)


def test_group_summary_rejects_nul():
    with pytest.raises(ValueError):
        _group("prod", "Prod\x00uction")


# ---------------------------------------------------------------------------
# ConnectionMetadataSummary
# ---------------------------------------------------------------------------
def test_metadata_constructs_with_safe_values():
    meta = ConnectionMetadataSummary(
        connection_id=ConnectionId("web"),
        values={"tags": ["prod", "web"], "pinned": True, "wol": {"port": 9}},
    )
    assert meta.connection_id == "web"
    assert meta.values["tags"] == ("prod", "web")
    assert meta.values["pinned"] is True


def test_metadata_deep_copies_values():
    original = {"nested": {"list": [1, 2]}}
    meta = ConnectionMetadataSummary(connection_id=ConnectionId("web"), values=original)
    original["nested"]["list"].append(3)
    assert meta.values["nested"]["list"] == (1, 2)


def test_metadata_values_absent_from_repr():
    meta = ConnectionMetadataSummary(
        connection_id=ConnectionId("web"),
        values={"tags": ["prod", "should-not-appear"]},
    )
    assert "should-not-appear" not in repr(meta)


def test_metadata_rejects_mapping_passed_as_list():
    with pytest.raises(TypeError):
        ConnectionMetadataSummary(connection_id=ConnectionId("web"), values=[1, 2])


def test_metadata_rejects_non_json_safe_value():
    with pytest.raises(TypeError):
        ConnectionMetadataSummary(connection_id=ConnectionId("web"), values={"x": object()})


@pytest.mark.parametrize(
    "value",
    [1, 1.5, True, None, "str", [1, 2], {"a": {"b": [1]}}],
)
def test_metadata_accepts_json_safe_values(value):
    meta = ConnectionMetadataSummary(connection_id=ConnectionId("web"), values={"x": value})
    assert thaw_safe_metadata(meta.values["x"]) == value


def test_metadata_is_recursively_immutable():
    meta = ConnectionMetadataSummary(
        connection_id=ConnectionId("web"),
        values={"nested": {"items": [1, {"enabled": True}]}},
    )
    with pytest.raises(TypeError):
        meta.values["nested"] = {}
    with pytest.raises(TypeError):
        meta.values["nested"]["items"][1]["enabled"] = False
    with pytest.raises(AttributeError):
        meta.values["nested"]["items"].append(2)


def test_metadata_rejects_nan_float():
    with pytest.raises(ValueError):
        ConnectionMetadataSummary(connection_id=ConnectionId("web"), values={"x": float("nan")})


def test_metadata_rejects_inf_float():
    with pytest.raises(ValueError):
        ConnectionMetadataSummary(connection_id=ConnectionId("web"), values={"x": float("inf")})


def test_metadata_rejects_excessive_depth():
    deep = current = {}
    for _ in range(10):
        current["n"] = {}
        current = current["n"]
    with pytest.raises(ValueError):
        ConnectionMetadataSummary(connection_id=ConnectionId("web"), values=deep)


def test_metadata_rejects_nul_in_key():
    with pytest.raises(ValueError):
        ConnectionMetadataSummary(connection_id=ConnectionId("web"), values={"bad\x00key": 1})


def test_metadata_rejects_nul_in_string_value():
    with pytest.raises(ValueError):
        ConnectionMetadataSummary(connection_id=ConnectionId("web"), values={"k": "a\x00b"})


@pytest.mark.parametrize(
    "key",
    ["password", "my_password", "passphrase", "secret", "token", "credential",
     "cookie", "private_key", "API_PASSWORD"],
)
def test_metadata_rejects_secret_like_keys(key):
    with pytest.raises(ValueError):
        ConnectionMetadataSummary(connection_id=ConnectionId("web"), values={key: "x"})


def test_metadata_rejects_oversized_values():
    with pytest.raises(ValueError):
        ConnectionMetadataSummary(
            connection_id=ConnectionId("web"),
            values={"big": "x" * (300 * 1024)},
        )


def test_metadata_rejects_bool_as_int_size_source():
    # True is not a dict key type allowed for nested dicts; top-level keys are
    # validated as exact strings.
    with pytest.raises(TypeError):
        ConnectionMetadataSummary(connection_id=ConnectionId("web"), values={True: 1})


# ---------------------------------------------------------------------------
# ConnectionStoreSnapshot
# ---------------------------------------------------------------------------
def test_snapshot_constructs():
    snapshot = _snapshot()
    assert snapshot.generation == 0
    assert len(snapshot.connections) == 1
    assert snapshot.root_connection_ids == ("web",)


def test_snapshot_rejects_negative_generation():
    with pytest.raises(ValueError):
        _snapshot(generation=-1)


def test_snapshot_rejects_duplicate_connection_ids():
    with pytest.raises(ValueError):
        _snapshot(connections=(_conn("a"), _conn("a")))


def test_snapshot_rejects_duplicate_group_ids():
    with pytest.raises(ValueError):
        _snapshot(groups=(_group("g"), _group("g")))


def test_snapshot_rejects_metadata_for_unknown_connection():
    with pytest.raises(ValueError):
        _snapshot(metadata=[ConnectionMetadataSummary(connection_id=ConnectionId("ghost"), values={})])


def test_snapshot_rejects_duplicate_metadata_ids():
    meta = ConnectionMetadataSummary(connection_id=ConnectionId("web"), values={})
    with pytest.raises(ValueError):
        _snapshot(metadata=[meta, meta])


def test_snapshot_rejects_group_referencing_unknown_connection():
    with pytest.raises(ValueError):
        _snapshot(groups=(_group("g", connection_ids=("ghost",),),))


def test_snapshot_rejects_root_referencing_unknown_connection():
    with pytest.raises(ValueError):
        _snapshot(root_connection_ids=("ghost",))


def test_snapshot_rejects_duplicate_root_ids():
    with pytest.raises(ValueError):
        _snapshot(connections=(_conn("a"), _conn("b")),
                  root_connection_ids=("a", "a"))


def test_snapshot_rejects_root_connection_in_group():
    with pytest.raises(ValueError):
        _snapshot(
            groups=(_group("g", connection_ids=("web",),),),
            root_connection_ids=("web",),
        )


def test_snapshot_rejects_ungrouped_connection_missing_from_root():
    with pytest.raises(ValueError):
        _snapshot(connections=(_conn("a"),), root_connection_ids=())


def test_snapshot_rejects_unknown_group_parent():
    with pytest.raises(ValueError):
        _snapshot(groups=(_group("child", parent_id=GroupId("ghost")),))


def test_snapshot_rejects_group_parent_cycles():
    a = _group("a", parent_id=GroupId("b"))
    b = _group("b", parent_id=GroupId("a"))
    with pytest.raises(ValueError):
        _snapshot(groups=(a, b))


def test_snapshot_accepts_valid_hierarchy():
    conn = _conn()
    a = _group("a", "A")
    b = _group("b", "B", parent_id=GroupId("a"))
    snapshot = _snapshot(
        groups=(a, b),
        connections=(conn,),
        root_connection_ids=("web",),
    )
    assert snapshot.groups[1].parent_id == "a"


# ---------------------------------------------------------------------------
# Group mutation requests
# ---------------------------------------------------------------------------
def test_set_group_color_constructs():
    request = SetGroupColorRequest(group_id=GroupId("prod"), color="#ff0000")
    assert request.color == "#ff0000"


def test_set_group_color_rejects_empty_group_id():
    with pytest.raises(ValueError):
        SetGroupColorRequest(group_id=GroupId(""), color="#ff0000")


def test_set_group_color_rejects_nul():
    with pytest.raises(ValueError):
        SetGroupColorRequest(group_id=GroupId("prod"), color="#ff\x0000")


def test_move_connections_constructs_and_rejects_invalid_targets():
    request = MoveConnectionsRequest(
        connection_ids=(ConnectionId("a"), ConnectionId("b")),
        target_group_id=GroupId("prod"),
        target_connection_id=ConnectionId("c"),
        position="below",
        expected_generation=3,
    )
    assert request.connection_ids == (ConnectionId("a"), ConnectionId("b"))
    with pytest.raises(ValueError):
        MoveConnectionsRequest(connection_ids=())
    with pytest.raises(ValueError):
        MoveConnectionsRequest(
            connection_ids=(ConnectionId("a"), ConnectionId("a"))
        )
    with pytest.raises(ValueError):
        MoveConnectionsRequest(
            connection_ids=(ConnectionId("a"),),
            target_connection_id=ConnectionId("a"),
        )
    with pytest.raises(ValueError):
        MoveConnectionsRequest(
            connection_ids=(ConnectionId("a"),), position="above"
        )


def test_place_group_constructs():
    request = PlaceGroupRequest(
        group_id=GroupId("a"),
        parent_id=GroupId("b"),
        index=0,
        expected_generation=7,
    )
    assert request.index == 0
    assert request.expected_generation == 7


def test_place_group_constructs_without_generation():
    request = PlaceGroupRequest(group_id=GroupId("a"), parent_id=None, index=0)
    assert request.expected_generation is None


def test_place_group_rejects_negative_index():
    with pytest.raises(ValueError):
        PlaceGroupRequest(group_id=GroupId("a"), parent_id=None, index=-1)


def test_place_group_rejects_bool_index():
    with pytest.raises(ValueError):
        PlaceGroupRequest(group_id=GroupId("a"), parent_id=None, index=True)


def test_place_group_rejects_negative_generation():
    with pytest.raises(ValueError):
        PlaceGroupRequest(
            group_id=GroupId("a"), parent_id=None, index=0, expected_generation=-1
        )


def test_place_group_rejects_bool_generation():
    with pytest.raises(ValueError):
        PlaceGroupRequest(
            group_id=GroupId("a"), parent_id=None, index=0, expected_generation=True
        )


def test_copy_connection_to_group_constructs():
    request = CopyConnectionToGroupRequest(connection_id=ConnectionId("web"), group_id=GroupId("prod"))
    assert request.connection_id == "web"


def test_copy_connection_to_group_rejects_empty():
    with pytest.raises(ValueError):
        CopyConnectionToGroupRequest(connection_id=ConnectionId(""), group_id=GroupId("prod"))


def test_remove_connection_from_group_constructs():
    request = RemoveConnectionFromGroupRequest(connection_id=ConnectionId("web"), group_id=GroupId("prod"))
    assert request.group_id == "prod"


def test_reorder_connection_constructs():
    request = ReorderConnectionRequest(
        connection_id=ConnectionId("a"),
        target_connection_id=ConnectionId("b"),
        group_id=GroupId("prod"),
        position="above",
    )
    assert request.position == "above"


def test_reorder_connection_rejects_invalid_position():
    with pytest.raises(ValueError):
        ReorderConnectionRequest(
            connection_id=ConnectionId("a"),
            target_connection_id=ConnectionId("b"),
            position="left",
        )


def test_reorder_connection_rejects_self_target():
    with pytest.raises(ValueError):
        ReorderConnectionRequest(
            connection_id=ConnectionId("a"),
            target_connection_id=ConnectionId("a"),
        )


def test_preserving_move_and_atomic_tag_requests_construct():
    move = MoveConnectionsRequest(
        connection_ids=(ConnectionId("a"), ConnectionId("b")),
        source_group_id=GroupId("web"),
        target_group_id=GroupId("web"),
        target_connection_id=ConnectionId("c"),
        position="below",
        expected_generation=4,
        mode=ConnectionPlacementMode.PRESERVE,
    )
    assert move.mode is ConnectionPlacementMode.PRESERVE
    tag = AddTagToConnectionsRequest(
        connection_ids=(ConnectionId("a"), ConnectionId("b")),
        tag=" prod ",
        expected_generation=4,
    )
    assert tag.tag == " prod "


def test_atomic_tag_request_rejects_invalid_values():
    with pytest.raises(ValueError):
        AddTagToConnectionsRequest(connection_ids=(), tag="prod")
    with pytest.raises(ValueError):
        AddTagToConnectionsRequest(
            connection_ids=(ConnectionId("a"), ConnectionId("a")), tag="prod"
        )
    with pytest.raises(ValueError):
        AddTagToConnectionsRequest(connection_ids=(ConnectionId("a"),), tag=" ")
    with pytest.raises(ValueError):
        AddTagToConnectionsRequest(connection_ids=(ConnectionId("a"),), tag="pro\x00d")


def test_rename_tag_constructs():
    request = RenameTagRequest(old_tag="prod", new_tag="production")
    assert request.old_tag == "prod"


def test_rename_tag_rejects_empty_names():
    with pytest.raises(ValueError):
        RenameTagRequest(old_tag=" ", new_tag="production")
    with pytest.raises(ValueError):
        RenameTagRequest(old_tag="prod", new_tag=" ")


def test_rename_tag_rejects_nul():
    with pytest.raises(ValueError):
        RenameTagRequest(old_tag="prod", new_tag="prod\x00uction")


# ---------------------------------------------------------------------------
# UpdateConnectionMetadataRequest hardening
# ---------------------------------------------------------------------------
def test_update_metadata_uses_safe_validation():
    request = UpdateConnectionMetadataRequest(
        connection_id=ConnectionId("web"),
        meta={"tags": ["prod"]},
    )
    assert thaw_safe_metadata(request.meta) == {"tags": ["prod"]}


def test_update_metadata_rejects_secret_keys():
    with pytest.raises(ValueError):
        UpdateConnectionMetadataRequest(
            connection_id=ConnectionId("web"),
            meta={"password": "hunter2"},
        )


def test_update_metadata_rejects_nul():
    with pytest.raises(ValueError):
        UpdateConnectionMetadataRequest(
            connection_id=ConnectionId("web"),
            meta={"tags": ["a\x00b"]},
        )


def test_update_metadata_deep_copies_input():
    original = {"tags": ["prod"]}
    request = UpdateConnectionMetadataRequest(connection_id=ConnectionId("web"), meta=original)
    original["tags"].append("extra")
    assert request.meta["tags"] == ("prod",)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def test_require_identifier_rejects_empty():
    with pytest.raises(ValueError):
        require_identifier("", "field")


def test_validate_safe_metadata_returns_fresh_copy():
    original = {"a": {"b": [1]}}
    result = validate_safe_metadata(original)
    assert thaw_safe_metadata(result) == original
    original["a"]["b"].append(2)
    assert result["a"]["b"] == (1,)
