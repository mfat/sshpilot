"""Editing SSH Host entries must preserve group membership.

Group membership is owned by the canonical connection repository and is
keyed by connection id. Renaming a connection (changing the ``Host`` alias
through the dialog) must follow group membership and root ordering to the new
id, and deleting a connection must drop its group membership and metadata
without touching sibling connections.
"""

from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore


def _repo(tmp_path, ssh_text: str) -> ConnectionRepository:
    root = tmp_path / "config"
    root.write_text(ssh_text)
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )


def _group_connection_ids(repo, group_id) -> tuple:
    for group in repo.snapshot().groups:
        if group.id == group_id:
            return tuple(group.connection_ids)
    return ()


def _id_for(repo, alias):
    return next(item.id for item in repo.snapshot().connections if item.ssh_alias == alias)


def test_rename_host_preserves_group_membership(tmp_path):
    repo = _repo(
        tmp_path,
        "Host old-server\n"
        "    HostName example.com\n"
        "    User alice\n"
        "    Port 22\n",
    )
    group_id = repo.create_group("Production Servers").id
    repo.assign_connection_to_group("old-server", group_id)
    repo.update_connection_metadata("old-server", {"pinned": True})
    assert _id_for(repo, "old-server") in _group_connection_ids(repo, group_id)

    repo.update_connection(
        "old-server",
        {
            "nickname": "new-server",
            "hostname": "example.com",
            "username": "alice",
            "port": 22,
            "protocol": "ssh",
        },
    )

    members = _group_connection_ids(repo, group_id)
    assert _id_for(repo, "new-server") in members
    assert len(members) == 1
    assert repo.get_editor_record("new-server") is not None
    assert repo.get_editor_record("old-server") is None
    metadata = {m.connection_id: m.values for m in repo.snapshot().metadata}
    assert metadata.get(_id_for(repo, "new-server")) == {"pinned": True}


def test_rename_one_host_keeps_siblings_in_group(tmp_path):
    repo = _repo(
        tmp_path,
        "Host server1\n"
        "    HostName server1.example.com\n"
        "    User alice\n"
        "    Port 22\n"
        "\n"
        "Host server2\n"
        "    HostName server2.example.com\n"
        "    User bob\n"
        "    Port 22\n",
    )
    group_id = repo.create_group("Web Servers").id
    repo.assign_connection_to_group("server1", group_id)
    repo.assign_connection_to_group("server2", group_id)
    assert len(_group_connection_ids(repo, group_id)) == 2

    repo.update_connection(
        "server1",
        {
            "nickname": "server1-renamed",
            "hostname": "server1.example.com",
            "username": "alice",
            "port": 22,
            "protocol": "ssh",
        },
    )

    members = _group_connection_ids(repo, group_id)
    assert _id_for(repo, "server1-renamed") in members
    assert _id_for(repo, "server2") in members
    assert len(members) == 2


def test_delete_removes_group_membership_and_metadata(tmp_path):
    repo = _repo(
        tmp_path,
        "Host server1\n"
        "    HostName server1.example.com\n"
        "    User alice\n"
        "    Port 22\n"
        "\n"
        "Host server2\n"
        "    HostName server2.example.com\n"
        "    User bob\n"
        "    Port 22\n",
    )
    group_id = repo.create_group("Web Servers").id
    repo.assign_connection_to_group("server1", group_id)
    repo.assign_connection_to_group("server2", group_id)
    repo.update_connection_metadata("server1", {"pinned": True})

    repo.delete_connection("server1")

    members = _group_connection_ids(repo, group_id)
    assert _id_for(repo, "server2") in members
    metadata = {m.connection_id: m.values for m in repo.snapshot().metadata}
    assert "server1" not in metadata
    assert "server2" not in metadata
