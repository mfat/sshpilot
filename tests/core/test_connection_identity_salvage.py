"""Salvage coverage: a damaged sidecar must cost only the damaged entries."""

from __future__ import annotations

import json
from pathlib import Path

from sshpilot.core.connections.identity_salvage import salvage_identity_state_v2
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.connections.state_file import read_identity_state_v2


def _repo(tmp_path: Path, text: str):
    root = tmp_path / "ssh_config"
    root.write_text(text, encoding="utf-8")
    state = tmp_path / "connections.json"
    return (
        ConnectionRepository(
            ssh_store=SshConfigStore(root, isolated=True),
            state_path=state,
            legacy_config_path=tmp_path / "config.json",
            isolated=True,
        ),
        root,
        state,
    )


def _populated(tmp_path: Path):
    """A sidecar holding everything that lives only in this file."""
    repo, root, state = _repo(
        tmp_path,
        "Host alpha\n    HostName a.example\nHost beta\n    HostName b.example\n",
    )
    group = repo.create_group("Work")
    repo.assign_connection_to_group("alpha", group.id)
    repo.update_connection_metadata("alpha", {"tags": ["prod"]})
    repo.update_connection(
        "alpha",
        {
            "nickname": "alpha",
            "hostname": "a.example",
            "protocol": "ssh",
            "display_name": "Alpha Box",
        },
    )
    for protocol, nickname in (
        ("telnet", "switch1"),
        ("docker", "web-ctr"),
        ("serial", "uart0"),
    ):
        repo.create_connection(
            {"nickname": nickname, "hostname": "10.0.0.9", "protocol": protocol}
        )
    del repo
    return root, state


def _uuid_for(document, alias: str) -> str:
    return next(
        key for key, entry in document["identities"].items()
        if entry["projection"]["alias"] == alias
    )


def test_salvage_keeps_every_entry_that_still_validates(tmp_path):
    """One broken entry must not cost the user the whole file."""
    _root, state = _populated(tmp_path)
    document = json.loads(state.read_text(encoding="utf-8"))
    document["identities"][_uuid_for(document, "beta")]["projection"] = "broken"
    document["non_ssh_connections"][1] = {"nickname": None}

    salvaged, report = salvage_identity_state_v2(
        json.dumps(document).encode("utf-8")
    )

    assert report.unreadable is False
    assert report.identities_kept == 1 and report.identities_dropped == 1
    assert report.non_ssh_kept == 2 and report.non_ssh_dropped == 1
    alpha = next(
        item for item in salvaged.identities if not item.tombstone
    )
    assert alpha.projection.alias == "alpha"
    assert alpha.display_name == "Alpha Box"
    assert dict(salvaged.metadata)[alpha.uuid] == {"tags": ["prod"]}
    assert [group.name for group in salvaged.groups] == ["Work"]
    assert {
        str(item.get("nickname")) for item in salvaged.non_ssh_connections
    } == {"switch1", "uart0"}


def test_salvage_of_unparseable_bytes_reports_itself_honestly(tmp_path):
    salvaged, report = salvage_identity_state_v2(b"{not-json")
    assert report.unreadable is True
    assert salvaged.identities == ()


def test_salvage_repairs_placement_rather_than_dropping_records(tmp_path):
    """Dangling, duplicated and orphaned placement is rebuilt, not discarded.

    ``IdentityStateV2`` needs every active connection placed exactly once, so
    a document that violates that would otherwise be unreadable in full.
    """
    _root, state = _populated(tmp_path)
    document = json.loads(state.read_text(encoding="utf-8"))
    alpha = _uuid_for(document, "alpha")
    # A reference to a record that no longer exists, a duplicate placement,
    # and a member entry that will not parse at all.
    document["groups"][0]["members"].append(
        {"kind": "ssh_uuid", "id": "11111111-1111-4111-8111-111111111111"}
    )
    document["groups"][0]["members"].append({"kind": "nonsense"})
    document["root_connections"].append({"kind": "ssh_uuid", "id": alpha})
    document["groups"][0]["parent_id"] = "no-such-group"

    salvaged, report = salvage_identity_state_v2(
        json.dumps(document).encode("utf-8")
    )

    assert report.placements_repaired >= 3
    assert report.groups_kept == 1
    assert salvaged.groups[0].parent_id is None
    placed = [
        reference.value for group in salvaged.groups for reference in group.members
    ] + [reference.value for reference in salvaged.root_connections]
    assert len(placed) == len(set(placed))
    assert alpha in placed


def test_salvage_breaks_a_group_parent_cycle(tmp_path):
    _root, state = _populated(tmp_path)
    document = json.loads(state.read_text(encoding="utf-8"))
    first = document["groups"][0]
    document["groups"].append(
        {"id": "g2", "name": "Second", "members": [], "parent_id": first["id"]}
    )
    first["parent_id"] = "g2"

    salvaged, _report = salvage_identity_state_v2(
        json.dumps(document).encode("utf-8")
    )

    parents = {group.id: group.parent_id for group in salvaged.groups}
    assert None in parents.values()
    assert len(salvaged.groups) == 2


def test_damaged_sidecar_recovers_in_the_repository_and_stays_writable(tmp_path):
    """End to end: the app comes up usable and keeps what it could read."""
    root, state = _populated(tmp_path)
    document = json.loads(state.read_text(encoding="utf-8"))
    document["identities"][_uuid_for(document, "beta")]["projection"] = "broken"
    document["non_ssh_connections"][1] = {"nickname": None}
    damaged = json.dumps(document).encode("utf-8")
    state.write_bytes(damaged)

    repo, _root, _state = _repo(tmp_path, root.read_text(encoding="utf-8"))

    assert repo._identity_state_unavailable is False
    visible = {item.id for item in repo.snapshot().connections}
    # "beta" lost its identity entry but is still an SSH host, so it comes
    # back through reconciliation; only the broken non-SSH record is gone.
    assert visible == {"alpha", "beta", "switch1", "uart0"}
    recovered = read_identity_state_v2(state)
    alpha = next(
        item for item in recovered.identities
        if not item.tombstone and item.projection.alias == "alpha"
    )
    assert alpha.display_name == "Alpha Box"
    assert dict(recovered.metadata)[alpha.uuid] == {"tags": ["prod"]}
    assert any(
        reference.value == alpha.uuid
        for group in recovered.groups
        for reference in group.members
    )
    quarantined = [item for item in tmp_path.iterdir() if ".corrupt-" in item.name]
    assert [item.read_bytes() for item in quarantined] == [damaged]
    assert repo.create_connection(
        {"nickname": "fresh", "hostname": "fresh.example", "protocol": "ssh"}
    ).id == "fresh"


def test_repeated_damage_never_clobbers_an_earlier_quarantine(tmp_path):
    root, state = _populated(tmp_path)
    first_damage = b'{"version": 2, "identities": "one"}'
    state.write_bytes(first_damage)
    repo, _root, _state = _repo(tmp_path, root.read_text(encoding="utf-8"))
    del repo
    second_damage = b'{"version": 2, "identities": "two"}'
    state.write_bytes(second_damage)
    repo, _root, _state = _repo(tmp_path, root.read_text(encoding="utf-8"))

    assert repo._identity_state_unavailable is False
    kept = sorted(
        item.read_bytes()
        for item in tmp_path.iterdir()
        if ".corrupt-" in item.name
    )
    assert kept == sorted([first_damage, second_damage])
