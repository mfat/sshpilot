"""Metadata-operation tests for the headless connection repository."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.repository import (  # noqa: E402
    ConnectionRepository,
)
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402
from sshpilot.core.connections.state_file import read_identity_state_v2  # noqa: E402
from sshpilot.core.errors import CoreError, ErrorCode  # noqa: E402
from sshpilot.api.models.common import ConnectionId  # noqa: E402
from sshpilot.api.models.connection_store import AddTagToConnectionsRequest  # noqa: E402


def _repo(tmp_path, ssh_text: str = ""):
    root = tmp_path / "ssh_config"
    if ssh_text:
        root.write_text(ssh_text)
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    ), root, tmp_path / "connections.json"


def _state(path: Path) -> dict:
    raw = json.loads(path.read_text())
    if raw.get("version") != 2:
        return raw
    state = read_identity_state_v2(path)
    aliases = {
        identity.uuid: identity.projection.alias
        for identity in state.identities
        if not identity.tombstone
    }
    return {
        "version": 1,
        "non_ssh_connections": list(state.non_ssh_connections),
        "groups": {"groups": {}, "root_connections": []},
        "metadata": {
            aliases[uuid]: values
            for uuid, values in state.metadata.items()
            if uuid in aliases
        },
    }


def _seed_web(repo):
    return repo.create_connection(
        {"nickname": "web", "hostname": "example.com", "protocol": "ssh"}
    )


# ---------------------------------------------------------------------------
# Update / merge semantics
# ---------------------------------------------------------------------------


def test_update_metadata_merges(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    repo.update_connection_metadata("web", {"pinned": True})
    repo.update_connection_metadata("web", {"tags": ["prod", "web"]})
    snap = repo.snapshot()
    values = snap.metadata[0].values
    assert values["pinned"] is True
    assert values["tags"] == ("prod", "web")


def test_metadata_pins_tags_and_wol(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    repo.update_connection_metadata(
        "web",
        {"pinned": True, "wol": {"mac": "aa:bb:cc:dd:ee:ff", "port": 9}},
    )
    repo.update_connection_metadata("web", {"tags": ["prod", "web"]})
    snap = repo.snapshot()
    values = snap.metadata[0].values
    assert values["pinned"] is True
    assert values["wol"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert values["tags"] == ("prod", "web")


def test_none_removes_requested_key_only(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    repo.update_connection_metadata(
        "web", {"pinned": True, "tags": ["a"], "keep": 1}
    )
    repo.update_connection_metadata("web", {"pinned": None})
    snap = repo.snapshot()
    values = snap.metadata[0].values
    assert "pinned" not in values
    assert values["tags"] == ("a",)
    assert values["keep"] == 1


def test_metadata_persists_to_state_file(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    repo.update_connection_metadata("web", {"pinned": True})
    stored = _state(state)
    assert stored["metadata"]["web"]["pinned"] is True


def test_metadata_for_unknown_connection_rejected(tmp_path):
    repo, root, state = _repo(tmp_path)
    with pytest.raises(CoreError) as exc:
        repo.update_connection_metadata("ghost", {"pinned": True})
    assert exc.value.code is ErrorCode.CONNECTION_NOT_FOUND


def test_metadata_rejects_secret_like_keys(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    with pytest.raises(ValueError):
        repo.update_connection_metadata("web", {"password": "hunter2"})
    with pytest.raises(ValueError):
        repo.update_connection_metadata("web", {"token": "abc"})


def test_empty_metadata_removes_entry(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    repo.update_connection_metadata("web", {"pinned": True})
    repo.update_connection_metadata("web", {"pinned": None})
    snap = repo.snapshot()
    assert snap.metadata == ()
    assert "web" not in _state(state)["metadata"]


# ---------------------------------------------------------------------------
# Tag rename
# ---------------------------------------------------------------------------


def test_rename_tag_case_insensitive_and_dedupes(tmp_path):
    repo, root, state = _repo(tmp_path)
    for name in ("web", "db"):
        repo.create_connection(
            {"nickname": name, "hostname": f"{name}.example", "protocol": "ssh"}
        )
    repo.update_connection_metadata("web", {"tags": ["Prod", "prod", "web"]})
    repo.update_connection_metadata("db", {"tags": ["prod", "db"]})
    repo.rename_tag("PROD", "production")
    snap = repo.snapshot()
    web_values = next(m for m in snap.metadata if m.connection_id == "web").values
    db_values = next(m for m in snap.metadata if m.connection_id == "db").values
    assert web_values["tags"] == ("production", "web")
    assert db_values["tags"] == ("production", "db")
    stored = _state(state)
    assert stored["metadata"]["web"]["tags"] == ["production", "web"]


def test_add_tag_to_connections_is_atomic_and_preserves_metadata(tmp_path):
    repo, root, state = _repo(tmp_path)
    for name in ("a", "b", "c"):
        repo.create_connection(
            {"nickname": name, "hostname": f"{name}.example", "protocol": "ssh"}
        )
    repo.update_connection_metadata("a", {"tags": ["web"], "pinned": True})
    repo.update_connection_metadata("b", {"tags": ["prod"], "keep": 7})
    before = repo.snapshot()
    events = []
    repo.add_listener(events.append)
    changed = repo.add_tag_to_connections(
        AddTagToConnectionsRequest(
            connection_ids=(ConnectionId("a"), ConnectionId("b"), ConnectionId("c")),
            tag=" Web ",
            expected_generation=before.generation,
        )
    )
    assert changed == 2
    assert len(events) == 1
    values = {item.connection_id: item.values for item in repo.snapshot().metadata}
    assert values["a"]["tags"] == ("web",)
    assert values["a"]["pinned"] is True
    assert values["b"]["tags"] == ("prod", "Web")
    assert values["b"]["keep"] == 7
    assert values["c"]["tags"] == ("Web",)


def test_add_tag_to_connections_rejects_invalid_batch_without_partial_update(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    before = repo.snapshot()
    with pytest.raises(CoreError) as exc:
        repo.add_tag_to_connections(
            AddTagToConnectionsRequest(
                connection_ids=(ConnectionId("web"), ConnectionId("ghost")),
                tag="prod",
                expected_generation=before.generation,
            )
        )
    assert exc.value.code is ErrorCode.CONNECTION_NOT_FOUND
    assert repo.snapshot() == before


def test_add_tag_to_connections_is_idempotent_and_rejects_stale_generation(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    repo.update_connection_metadata("web", {"tags": ["prod"]})
    before = repo.snapshot()
    events = []
    repo.add_listener(events.append)
    assert repo.add_tag_to_connections(
        AddTagToConnectionsRequest(
            connection_ids=(ConnectionId("web"),),
            tag="PROD",
            expected_generation=before.generation,
        )
    ) == 0
    assert repo.snapshot().generation == before.generation
    assert events == []
    with pytest.raises(CoreError) as exc:
        repo.add_tag_to_connections(
            AddTagToConnectionsRequest(
                connection_ids=(ConnectionId("web"),),
                tag="new",
                expected_generation=before.generation - 1,
            )
        )
    assert exc.value.code is ErrorCode.STALE_CONNECTION_STATE
    assert repo.snapshot().generation == before.generation


def test_rename_tag_with_no_tags_is_noop(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    repo.update_connection_metadata("web", {"pinned": True})
    events = []
    repo.add_listener(lambda change: events.append(change))
    repo.rename_tag("a", "b")
    snap = repo.snapshot()
    assert events == []  # no tag changed: no event, no generation bump
    assert snap.generation == 2


def test_metadata_values_never_appear_in_repr(tmp_path):
    repo, root, state = _repo(tmp_path)
    _seed_web(repo)
    repo.update_connection_metadata("web", {"wol": {"mac": "aa:bb:cc:dd:ee:ff"}})
    snap = repo.snapshot()
    assert "aa:bb:cc:dd:ee:ff" not in repr(snap.metadata[0])


def test_non_ssh_tags_survive_reload_and_appear_in_snapshot(tmp_path):
    """Plugin-protocol tags live in non_ssh_metadata; the UI snapshot must
    still project them after a fresh repository load (config reload path)."""
    repo, root, state = _repo(tmp_path)
    repo.create_connection(
        {
            "nickname": "lab-switch",
            "hostname": "10.0.0.5",
            "protocol": "telnet",
            "port": 2323,
        }
    )
    repo.update_connection_metadata(
        "lab-switch",
        {"tags": ["telnet", "lab"], "wol_mac": "", "wol_port": 9},
    )

    live = {
        item.connection_id: dict(item.values)
        for item in repo.snapshot().metadata
    }
    assert live["lab-switch"]["tags"] == ("telnet", "lab")

    fresh = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=state,
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    reloaded = {
        item.connection_id: dict(item.values)
        for item in fresh.snapshot().metadata
    }
    assert reloaded["lab-switch"]["tags"] == ("telnet", "lab")
