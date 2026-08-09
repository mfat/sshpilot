"""Tests for the extended canonical connection/group records."""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.models import (  # noqa: E402
    ConnectionRecord,
    GroupRecord,
    generate_duplicate_nickname,
    generate_group_slug,
)


def test_host_and_aliases_fields():
    record = ConnectionRecord.from_dict(
        {
            "id": "web",
            "nickname": "web",
            "host": "web",
            "__host_tokens": ["web", "www", "web2"],
        }
    )
    assert record.id == "web"
    assert record.host == "web"
    assert record.aliases == ("www", "web2")


def test_aliases_default_to_empty_tuple():
    record = ConnectionRecord.from_dict({"id": "solo", "nickname": "solo"})
    assert record.host == "solo"
    assert record.aliases == ()


def test_aliases_from_aliases_key():
    record = ConnectionRecord.from_dict(
        {"id": "a", "nickname": "a", "aliases": ["x", "y"], "host": "a"}
    )
    assert record.aliases == ("x", "y")


def test_generation_exact_nonnegative_int():
    record = ConnectionRecord.from_dict({"id": "c", "nickname": "c", "generation": 7})
    assert record.generation == 7
    assert type(record.generation) is int
    # Non-numeric generation falls back to zero rather than raising.
    assert ConnectionRecord.from_dict({"id": "d", "nickname": "d", "generation": "x"}).generation == 0
    # Negative is permitted at the model layer (domain enforces semantics).
    record = ConnectionRecord(id="e", nickname="e", generation=-1)
    assert record.generation == -1


def test_source_is_internal_persistence_metadata():
    record = ConnectionRecord.from_dict(
        {"id": "c", "nickname": "c", "source": "/tmp/config", "generation": 1}
    )
    assert record.source == "/tmp/config"
    assert record.generation == 1


def test_no_uuid_survives_construction():
    record = ConnectionRecord.from_dict(
        {"id": "c", "nickname": "c", "uuid": "deadbeef", "data": {"uuid": "deadbeef"}}
    )
    payload = record.to_dict()
    assert "uuid" not in payload
    assert "uuid" not in record.data


def test_to_dict_round_trip_preserves_identity_fields():
    record = ConnectionRecord.from_dict(
        {"id": "web", "nickname": "web", "host": "web", "aliases": ["www"]}
    )
    payload = record.to_dict()
    assert payload["id"] == "web"
    assert payload["nickname"] == "web"


def test_group_record_connection_ids_order_preserved():
    group = GroupRecord(id="g", name="G")
    group.connection_ids = ["b", "a", "c"]
    assert group.connection_ids == ["b", "a", "c"]
    again = GroupRecord.from_dict(group.to_dict())
    assert again.connection_ids == ["b", "a", "c"]


def test_generate_duplicate_nickname_and_group_slug():
    existing = {"web", "web-Copy", "web-Copy-2"}
    assert generate_duplicate_nickname("web", existing) == "web-Copy-3"
    assert generate_group_slug("Prod & QA", {"prod-qa"}) == "prod-qa-2"
    assert generate_group_slug("!!!", set()) == "group"
