"""One-time split of the shared sidecar into one workspace per SSH root."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.state_file import (  # noqa: E402
    read_identity_state_v2,
)
from sshpilot.core.connections.workspace_split import (  # noqa: E402
    WorkspaceSplitSkipped,
    split_workspaces,
)


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


def _identity(alias, source, hostname, *, name="", tombstone=False, generation=None):
    payload = {
        "display_name": name or alias,
        "tombstone": tombstone,
        "projection": {
            "alias": alias,
            "hostname": hostname,
            "port": 22,
            "username": "",
            "username_is_explicit": False,
            "identity_files": [],
            "source": str(source),
            "declaration_order": 0,
        },
    }
    if tombstone:
        payload["retired_generation"] = generation or 5
    return payload


@pytest.fixture()
def rig(tmp_path):
    """Two roots, each pulling one host in through an ``Include``.

    An entry declared in a fragment records the *fragment* as its source, not
    the root, so this is the shape that a naive root-path comparison misfiles.
    """
    ssh = tmp_path / "ssh"
    cfg = tmp_path / "cfg"
    ssh.mkdir()
    cfg.mkdir()

    (ssh / "sshpilot-imported.conf").write_text(
        "Host imported-default\n    HostName imp-default.example\n"
    )
    (ssh / "config").write_text(
        f"Include {ssh / 'sshpilot-imported.conf'}\n"
        "Host web\n    HostName default.example\n"
    )
    (cfg / "sshpilot-imported.conf").write_text(
        "Host imported-iso\n    HostName imp-iso.example\n"
    )
    (cfg / "ssh_config").write_text(
        f"Include {cfg / 'sshpilot-imported.conf'}\n"
        "Host iso-host\n    HostName iso.example\n"
    )

    shared = cfg / "connections.json"
    shared.write_text(
        json.dumps(
            {
                "version": 2,
                "sidecar_generation": 42,
                "last_reconciled_ssh_revision": "rev",
                "observed_ssh_revision": "rev",
                "identities": {
                    _uuid(1): _identity(
                        "web", ssh / "config", "default.example", name="My Web"
                    ),
                    _uuid(2): _identity(
                        "imported-default",
                        ssh / "sshpilot-imported.conf",
                        "imp-default.example",
                    ),
                    _uuid(3): _identity(
                        "iso-host", cfg / "ssh_config", "iso.example", name="Iso Host"
                    ),
                    _uuid(4): _identity(
                        "imported-iso",
                        cfg / "sshpilot-imported.conf",
                        "imp-iso.example",
                    ),
                    _uuid(5): _identity(
                        "gone-iso",
                        cfg / "ssh_config",
                        "old.example",
                        tombstone=True,
                    ),
                    _uuid(6): _identity(
                        "gone-default",
                        ssh / "config",
                        "older.example",
                        tombstone=True,
                    ),
                },
                "groups": [
                    {
                        "id": "g1",
                        "name": "Servers",
                        "order": 0,
                        "color": "rgb(1,2,3)",
                        "parent_id": None,
                        "members": [
                            {"kind": "ssh_uuid", "id": _uuid(1)},
                            {"kind": "ssh_uuid", "id": _uuid(3)},
                            {"kind": "non_ssh_id", "id": "tel"},
                        ],
                    }
                ],
                "root_connections": [
                    {"kind": "ssh_uuid", "id": _uuid(2)},
                    {"kind": "ssh_uuid", "id": _uuid(4)},
                ],
                "metadata": {
                    _uuid(1): {"tags": ["prod"]},
                    _uuid(3): {"tags": ["lab"]},
                },
                "non_ssh_connections": [
                    {
                        "id": "tel",
                        "nickname": "tel",
                        "protocol": "telnet",
                        "hostname": "10.0.0.5",
                        "port": 23,
                    }
                ],
                "non_ssh_metadata": {"tel": {"pinned": True}},
                "legacy_orphans": [],
                "pending_ambiguities": [],
            }
        )
    )
    return {
        "ssh": ssh,
        "cfg": cfg,
        "shared": shared,
        "isolated": cfg / "connections-isolated.json",
    }


def _split(rig, *, non_ssh_to_isolated=False):
    return split_workspaces(
        shared_path=rig["shared"],
        isolated_path=rig["isolated"],
        default_root=rig["ssh"] / "config",
        isolated_root=rig["cfg"] / "ssh_config",
        config_dir=rig["cfg"],
        non_ssh_to_isolated=non_ssh_to_isolated,
    )


def _aliases(state):
    return sorted(identity.projection.alias for identity in state.identities)


def test_identities_follow_the_root_that_declared_them(rig):
    default_state, isolated_state = _split(rig)

    assert _aliases(default_state) == ["gone-default", "imported-default", "web"]
    assert _aliases(isolated_state) == ["gone-iso", "imported-iso", "iso-host"]


def test_identities_declared_in_an_included_fragment_are_not_misfiled(rig):
    """The case a root-path comparison gets wrong.

    ``projection.source`` is the declaring file, so a host pulled in through
    an ``Include`` records the fragment's path and never equals either root.
    """
    default_state, isolated_state = _split(rig)

    assert "imported-default" in _aliases(default_state)
    assert "imported-default" not in _aliases(isolated_state)
    assert "imported-iso" in _aliases(isolated_state)
    assert "imported-iso" not in _aliases(default_state)


def test_the_two_workspaces_share_no_identity(rig):
    default_state, isolated_state = _split(rig)

    default_uuids = {identity.uuid for identity in default_state.identities}
    isolated_uuids = {identity.uuid for identity in isolated_state.identities}
    assert default_uuids.isdisjoint(isolated_uuids)


def test_folders_exist_in_both_workspaces_with_their_own_members(rig):
    """A folder is something the user made, not a property of one root."""
    default_state, isolated_state = _split(rig)

    assert [group.id for group in default_state.groups] == ["g1"]
    assert [group.id for group in isolated_state.groups] == ["g1"]
    for state in (default_state, isolated_state):
        group = state.groups[0]
        assert group.name == "Servers"
        assert group.color == "rgb(1,2,3)"
        assert group.order == 0

    default_members = {ref.value for ref in default_state.groups[0].members}
    isolated_members = {ref.value for ref in isolated_state.groups[0].members}
    assert _uuid(1) in default_members and _uuid(3) not in default_members
    assert _uuid(3) in isolated_members and _uuid(1) not in isolated_members


def test_root_order_and_metadata_are_partitioned(rig):
    default_state, isolated_state = _split(rig)

    assert [ref.value for ref in default_state.root_connections] == [_uuid(2)]
    assert [ref.value for ref in isolated_state.root_connections] == [_uuid(4)]
    assert dict(default_state.metadata) == {_uuid(1): {"tags": ["prod"]}}
    assert dict(isolated_state.metadata) == {_uuid(3): {"tags": ["lab"]}}


def test_non_ssh_connections_go_to_the_mode_the_user_is_in(rig):
    """Nothing declares which root a telnet/RDP entry belongs to.

    They are handed to the workspace that is active at migration time, so
    nothing appears to vanish on upgrade.
    """
    default_state, isolated_state = _split(rig, non_ssh_to_isolated=False)

    assert [item["nickname"] for item in default_state.non_ssh_connections] == ["tel"]
    assert isolated_state.non_ssh_connections == ()
    assert dict(isolated_state.non_ssh_metadata) == {}
    # The folder reference to it moves with it.
    assert "tel" in {ref.value for ref in default_state.groups[0].members}
    assert "tel" not in {ref.value for ref in isolated_state.groups[0].members}


def test_non_ssh_connections_follow_an_isolated_mode_install(rig):
    default_state, isolated_state = _split(rig, non_ssh_to_isolated=True)

    assert [item["nickname"] for item in isolated_state.non_ssh_connections] == ["tel"]
    assert default_state.non_ssh_connections == ()


def test_revisions_are_cleared_so_each_workspace_reconciles_honestly(rig):
    default_state, isolated_state = _split(rig)

    for state in (default_state, isolated_state):
        assert state.observed_ssh_revision is None
        assert state.last_reconciled_ssh_revision is None
        # Generation never goes backwards.
        assert state.sidecar_generation == 42


def test_the_original_file_is_backed_up_before_anything_is_written(rig):
    _split(rig)

    backups = list(rig["cfg"].glob("connections.json.pre-workspace-split-*"))
    assert len(backups) == 1
    original = json.loads(backups[0].read_text())
    assert len(original["identities"]) == 6


def test_running_twice_is_stable_and_keeps_one_backup(rig):
    """Idempotency is by partitioning from the backup, not the live file.

    A crash between the two writes and the marker must not cause the second
    run to re-partition an already-pruned default workspace.
    """
    _split(rig)
    first = (rig["shared"].read_bytes(), rig["isolated"].read_bytes())

    _split(rig)
    second = (rig["shared"].read_bytes(), rig["isolated"].read_bytes())

    assert first == second
    assert len(list(rig["cfg"].glob("connections.json.pre-workspace-split-*"))) == 1


def test_a_pending_identity_transaction_defers_the_split(rig):
    intent = rig["shared"].with_name(rig["shared"].name + ".pending")
    intent.write_text("{}")

    with pytest.raises(WorkspaceSplitSkipped) as excinfo:
        _split(rig)

    assert excinfo.value.reason == "pending identity transaction"
    assert not rig["isolated"].exists()
    assert not list(rig["cfg"].glob("connections.json.pre-workspace-split-*"))


def test_a_corrupt_sidecar_is_never_replaced(rig):
    rig["shared"].write_text("{ not json")

    with pytest.raises(WorkspaceSplitSkipped):
        _split(rig)

    assert rig["shared"].read_text() == "{ not json"
    assert not rig["isolated"].exists()


def test_a_missing_sidecar_is_nothing_to_split(rig):
    rig["shared"].unlink()

    with pytest.raises(WorkspaceSplitSkipped) as excinfo:
        _split(rig)

    assert excinfo.value.reason == "no shared sidecar"


def test_the_split_can_be_disabled_for_support(rig, monkeypatch):
    monkeypatch.setenv("SSHPILOT_SKIP_WORKSPACE_SPLIT", "1")

    with pytest.raises(WorkspaceSplitSkipped):
        _split(rig)

    assert not rig["isolated"].exists()


def test_split_files_are_readable_as_valid_v2_state(rig):
    _split(rig)

    assert read_identity_state_v2(rig["shared"]) is not None
    assert read_identity_state_v2(rig["isolated"]) is not None


def test_the_marker_gates_the_migration_not_the_isolated_file(rig):
    """Idempotency must survive the user deleting the isolated sidecar.

    Keying "already split?" off the isolated file's existence would re-split
    an already-partitioned default workspace and pull the other root's
    entries back into it.
    """
    from sshpilot.core.connections.workspace_split import (
        already_split,
        ensure_workspaces_split,
    )

    def run():
        return ensure_workspaces_split(
            config_dir=rig["cfg"],
            shared_path=rig["shared"],
            isolated_path=rig["isolated"],
            default_root=rig["ssh"] / "config",
            isolated_root=rig["cfg"] / "ssh_config",
            non_ssh_to_isolated=False,
        )

    assert already_split(rig["cfg"]) is False
    assert run() is True
    assert already_split(rig["cfg"]) is True
    default_after_first = rig["shared"].read_bytes()

    # The user removes the isolated workspace; the split must not re-run.
    rig["isolated"].unlink()
    assert run() is True

    assert rig["shared"].read_bytes() == default_after_first
    assert not rig["isolated"].exists()


def test_a_deferred_split_is_not_recorded_as_done(rig):
    """A boot where recovery owns the sidecar must retry next time."""
    from sshpilot.core.connections.workspace_split import (
        already_split,
        ensure_workspaces_split,
    )

    intent = rig["shared"].with_name(rig["shared"].name + ".pending")
    intent.write_text("{}")

    done = ensure_workspaces_split(
        config_dir=rig["cfg"],
        shared_path=rig["shared"],
        isolated_path=rig["isolated"],
        default_root=rig["ssh"] / "config",
        isolated_root=rig["cfg"] / "ssh_config",
        non_ssh_to_isolated=False,
    )

    assert done is False
    assert already_split(rig["cfg"]) is False
    assert not rig["isolated"].exists()


def test_a_fresh_install_records_the_split_without_writing_a_sidecar(rig):
    """Nothing to split is still "split": it must not retry every boot."""
    from sshpilot.core.connections.workspace_split import (
        already_split,
        ensure_workspaces_split,
    )

    rig["shared"].unlink()

    assert ensure_workspaces_split(
        config_dir=rig["cfg"],
        shared_path=rig["shared"],
        isolated_path=rig["isolated"],
        default_root=rig["ssh"] / "config",
        isolated_root=rig["cfg"] / "ssh_config",
        non_ssh_to_isolated=False,
    ) is True
    assert already_split(rig["cfg"]) is True
    assert not rig["shared"].exists()
    assert not rig["isolated"].exists()


def test_a_first_ever_start_records_the_split_without_a_config_dir(tmp_path):
    """A fresh install has no config directory yet.

    There is nothing to split, but the outcome still has to be recorded, or
    every start retries and logs a failure at the user.
    """
    from sshpilot.core.connections.workspace_split import (
        already_split,
        ensure_workspaces_split,
    )

    config_dir = tmp_path / "never-created"
    assert not config_dir.exists()

    assert ensure_workspaces_split(
        config_dir=config_dir,
        shared_path=config_dir / "connections.json",
        isolated_path=config_dir / "connections-isolated.json",
        default_root=tmp_path / "ssh" / "config",
        isolated_root=config_dir / "ssh_config",
        non_ssh_to_isolated=False,
    ) is True
    assert already_split(config_dir) is True
