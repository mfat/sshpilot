import copy
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import sshpilot.connection_manager as connection_manager_module
import sshpilot.session_manager as session_manager_module
from sshpilot.api import DaemonClient, InProcessClient
from sshpilot.api.models import DeleteConnectionRequest, UpdateConnectionRequest
from sshpilot.config import Config
from sshpilot.connection_identity import (
    canonical_connection_uuid,
    connection_id_from_uuid,
    connection_uuid_from_id,
    new_connection_uuid,
    transitional_connection_id,
)
from sshpilot.connection_manager import Connection, ConnectionManager
from sshpilot.groups import GroupManager
from sshpilot.daemon import DaemonServer
from sshpilot.session_manager import SessionManager


class FileConfig:
    """Small real-file config adapter for migration integration tests."""

    def __init__(self, path: Path, data=None):
        self.config_file = str(path)
        if data is None and path.exists():
            self.config_data = json.loads(path.read_text())
        else:
            self.config_data = copy.deepcopy(data or {"config_version": 3})
        self._connection_uuid_by_nickname = {}
        self._connection_nickname_by_uuid = {}
        Config.save_json_config(self, self.config_data, raise_on_error=True)

    def get_setting(self, key, default=None):
        current = self.config_data
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def set_setting(self, key, value):
        ConnectionManager._set_nested_setting(self.config_data, key, value)
        Config.save_json_config(self, self.config_data, raise_on_error=True)

    def save_json_config(self, data=None, *, raise_on_error=False):
        return Config.save_json_config(
            self,
            data,
            raise_on_error=raise_on_error,
        )

    def bind_connection_identities(self, connections):
        Config.bind_connection_identities(self, connections)


def make_manager(tmp_path, monkeypatch, ssh_text, config_data=None):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    (ssh_dir / "config").write_text(ssh_text, encoding="utf-8")
    config = FileConfig(tmp_path / "config.json", config_data)
    monkeypatch.setattr(connection_manager_module, "get_ssh_dir", lambda: str(ssh_dir))
    monkeypatch.setattr(
        connection_manager_module,
        "get_config_dir",
        lambda: str(tmp_path),
    )
    manager = ConnectionManager(config)
    return manager, config, ssh_dir / "config"


def test_uuid_helpers_are_canonical_strict_and_non_nil():
    value = "550E8400-E29B-41D4-A716-446655440000"
    canonical = "550e8400-e29b-41d4-a716-446655440000"

    assert canonical_connection_uuid(value) == canonical
    assert connection_id_from_uuid(value) == f"connection:{canonical}"
    assert connection_uuid_from_id(f"connection:{canonical}") == canonical
    assert connection_uuid_from_id(f"connection:{value}") == canonical
    with pytest.raises(ValueError):
        canonical_connection_uuid("00000000-0000-0000-0000-000000000000")
    with pytest.raises(ValueError):
        connection_uuid_from_id("wrong:" + canonical)
    with pytest.raises(ValueError):
        connection_uuid_from_id(f"connection:{canonical}:trailing")


def test_connection_uuid_is_immutable():
    first = Connection({"nickname": "first"})
    duplicate = Connection({"nickname": "copy", "uuid": first.uuid})
    assert first.uuid == duplicate.uuid

    original_data = dict(first.data)
    with pytest.raises(ValueError):
        first.update_data({"uuid": new_connection_uuid()})
    assert first.data == original_data


def test_old_ssh_config_groups_and_metadata_migrate_atomically(
    tmp_path,
    monkeypatch,
):
    config_data = {
        "config_version": 3,
        "connections_meta": {
            "alpha": {"pinned": True, "tags": ["prod"]},
        },
        "connection_groups": {
            "groups": {
                "group-1": {
                    "id": "group-1",
                    "name": "Production",
                    "parent_id": None,
                    "children": [],
                    "connections": ["alpha"],
                    "expanded": True,
                    "order": 0,
                }
            },
            "connections": {"alpha": "group-1"},
            "root_connections": [],
        },
    }
    manager, config, ssh_path = make_manager(
        tmp_path,
        monkeypatch,
        "Host alpha\n    HostName alpha.example\n    User alice\n",
        config_data,
    )

    connection = manager.get_connections()[0]
    public_id = InProcessClient.connection_id_for(connection)
    assert public_id == connection_id_from_uuid(connection.uuid)
    assert f"# sshpilot:ConnectionUUID alpha {connection.uuid}" in ssh_path.read_text()
    assert config.config_data["connection_identity_version"] == 1
    assert set(config.config_data["connections_meta"]) == {connection.uuid}
    groups = config.config_data["connection_groups"]
    assert groups["groups"]["group-1"]["connections"] == [connection.uuid]
    assert groups["connections"] == {connection.uuid: "group-1"}
    assert stat.S_IMODE(ssh_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(config.config_file).stat().st_mode) == 0o600
    assert Path(f"{ssh_path}.bak").read_text().startswith("Host alpha")

    reloaded = ConnectionManager(config)
    assert InProcessClient.connection_id_for(reloaded.get_connections()[0]) == public_id


def test_duplicate_malformed_and_uppercase_uuids_are_repaired(
    tmp_path,
    monkeypatch,
):
    duplicate = "550e8400-e29b-41d4-a716-446655440000"
    manager, _config, ssh_path = make_manager(
        tmp_path,
        monkeypatch,
        (
            "Host first\n"
            f"    # sshpilot:ConnectionUUID first {duplicate.upper()}\n"
            "Host second\n"
            f"    # sshpilot:ConnectionUUID second {duplicate}\n"
            "Host third\n"
            "    # sshpilot:ConnectionUUID third malformed\n"
        ),
    )

    identities = [connection.uuid for connection in manager.get_connections()]
    assert identities[0] == duplicate
    assert len(set(identities)) == 3
    text = ssh_path.read_text()
    assert duplicate.upper() not in text
    assert "malformed" not in text


def test_multi_host_block_persists_one_uuid_per_connection(
    tmp_path,
    monkeypatch,
):
    manager, _config, ssh_path = make_manager(
        tmp_path,
        monkeypatch,
        "Host alpha beta\n    User alice\n",
    )

    assert len(manager.get_connections()) == 2
    assert len({connection.uuid for connection in manager.get_connections()}) == 2
    text = ssh_path.read_text()
    assert text.count("# sshpilot:ConnectionUUID") == 2


def test_non_ssh_records_receive_durable_distinct_uuids(
    tmp_path,
    monkeypatch,
):
    duplicate = "550e8400-e29b-41d4-a716-446655440000"
    manager, config, _ssh_path = make_manager(
        tmp_path,
        monkeypatch,
        "# empty\n",
        {
            "config_version": 3,
            "connections": {
                "non_ssh": [
                    {
                        "nickname": "serial-a",
                        "protocol": "serial",
                        "uuid": duplicate,
                    },
                    {
                        "nickname": "serial-b",
                        "protocol": "serial",
                        "uuid": duplicate,
                    },
                    {
                        "nickname": "serial-c",
                        "protocol": "serial",
                        "uuid": "malformed",
                    },
                ]
            },
        },
    )

    connections = manager.get_connections()
    identities = [connection.uuid for connection in connections]
    assert identities[0] == duplicate
    assert len(identities) == len(set(identities)) == 3
    stored = config.config_data["connections"]["non_ssh"]
    assert [record["uuid"] for record in stored] == identities

    reloaded = ConnectionManager(config)
    assert [connection.uuid for connection in reloaded.get_connections()] == identities


def test_ambiguous_legacy_group_nickname_is_not_rebound(
    tmp_path,
    monkeypatch,
):
    manager, config, _ssh_path = make_manager(
        tmp_path,
        monkeypatch,
        "# empty\n",
        {
            "config_version": 3,
            "connections": {
                "non_ssh": [
                    {"nickname": "duplicate", "protocol": "serial"},
                    {"nickname": "duplicate", "protocol": "serial"},
                ]
            },
            "connection_groups": {
                "groups": {
                    "group-1": {
                        "id": "group-1",
                        "name": "Ambiguous",
                        "connections": ["duplicate"],
                    }
                },
                "connections": {"duplicate": "group-1"},
                "root_connections": [],
            },
        },
    )

    assert len(manager.get_connections()) == 2
    groups = config.config_data["connection_groups"]
    assert groups["groups"]["group-1"]["connections"] == []
    assert groups["connections"] == {}


def test_disabled_migration_never_exposes_ephemeral_identity(
    tmp_path,
    monkeypatch,
):
    manager, _config, ssh_path = make_manager(
        tmp_path,
        monkeypatch,
        "Host alpha\n    HostName alpha.example\n",
    )
    migrated = ssh_path.read_text()
    ssh_path.write_text(
        migrated.replace(
            next(
                line for line in migrated.splitlines()
                if "sshpilot:ConnectionUUID" in line
            ) + "\n",
            "",
        )
    )

    disabled = ConnectionManager(
        manager.config,
        identity_migration_enabled=False,
    )

    assert disabled.get_connections() == []
    assert disabled.identity_migration_error is not None
    assert "# sshpilot:ConnectionUUID" not in ssh_path.read_text()


def test_rename_and_host_changes_keep_public_identity_and_group_reference(
    tmp_path,
    monkeypatch,
):
    manager, config, _ssh_path = make_manager(
        tmp_path,
        monkeypatch,
        "Host alpha\n    HostName old.example\n",
    )
    group_manager = GroupManager(config, connection_manager=manager)
    group_id = group_manager.create_group("Production")
    group_manager.move_connection("alpha", group_id)
    manager.emit = lambda *_args: None
    client = InProcessClient(manager, group_manager=group_manager)
    original_object = manager.get_connections()[0]
    before = client.list_connections()[0]
    legacy = transitional_connection_id("ssh", "alpha")

    updated = client.update_connection(
        before.id,
        UpdateConnectionRequest(
            nickname="renamed",
            hostname="new.example",
            username="new-user",
            port=2202,
        ),
    )

    assert updated.id == before.id
    assert manager.get_connections()[0] is original_object
    assert group_manager.groups[group_id]["connections"] == [
        manager.get_connections()[0].uuid
    ]
    assert client.get_connection(updated.id).nickname == "renamed"
    with pytest.raises(Exception):
        client.get_connection(legacy)

    result = client.delete_connection(DeleteConnectionRequest(updated.id))
    assert result.connection_id == updated.id
    assert group_manager.groups[group_id]["connections"] == []


def test_saved_session_references_migrate_to_stable_id(
    tmp_path,
    monkeypatch,
):
    manager, _config, _ssh_path = make_manager(
        tmp_path,
        monkeypatch,
        "Host alpha\n    HostName alpha.example\n",
    )
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(
        json.dumps(
            {
                "sessions": {
                    "work": {
                        "tabs": [
                            {"type": "ssh", "nickname": "alpha"},
                            {
                                "type": "split",
                                "panes": [[{"nickname": "alpha"}]],
                            },
                        ]
                    }
                },
                "previous": None,
            }
        )
    )
    monkeypatch.setattr(
        session_manager_module,
        "get_config_dir",
        lambda: str(tmp_path),
    )

    sessions = SessionManager(connection_manager=manager)
    expected = connection_id_from_uuid(manager.get_connections()[0].uuid)
    entries = sessions.sessions["work"]["tabs"]
    assert entries[0]["connection_id"] == expected
    assert entries[1]["panes"][0][0]["connection_id"] == expected
    assert entries[0]["nickname"] == "alpha"
    assert stat.S_IMODE(sessions_path.stat().st_mode) == 0o600


def test_migration_write_failure_exposes_no_ephemeral_connection(
    tmp_path,
    monkeypatch,
):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)
    ssh_path = ssh_dir / "config"
    original = "Host alpha\n    HostName alpha.example\n"
    ssh_path.write_text(original)
    config = FileConfig(tmp_path / "config.json")
    monkeypatch.setattr(connection_manager_module, "get_ssh_dir", lambda: str(ssh_dir))
    monkeypatch.setattr(
        connection_manager_module,
        "get_config_dir",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(
        ConnectionManager,
        "_safe_write_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    manager = ConnectionManager(config)

    assert manager.get_connections() == []
    assert manager.identity_migration_error is not None
    assert ssh_path.read_text() == original


def test_cross_file_migration_failure_is_restartable(
    tmp_path,
    monkeypatch,
):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)
    ssh_path = ssh_dir / "config"
    ssh_path.write_text("Host alpha\n    HostName alpha.example\n")
    config = FileConfig(
        tmp_path / "config.json",
        {
            "config_version": 3,
            "connections_meta": {"alpha": {"pinned": True}},
        },
    )
    monkeypatch.setattr(connection_manager_module, "get_ssh_dir", lambda: str(ssh_dir))
    monkeypatch.setattr(
        connection_manager_module,
        "get_config_dir",
        lambda: str(tmp_path),
    )
    real_save = config.save_json_config
    monkeypatch.setattr(
        config,
        "save_json_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    failed = ConnectionManager(config)

    assert failed.get_connections() == []
    assert failed.identity_migration_error is not None
    assert "# sshpilot:ConnectionUUID" in ssh_path.read_text()

    monkeypatch.setattr(config, "save_json_config", real_save)
    resumed = ConnectionManager(config)
    assert len(resumed.get_connections()) == 1
    assert config.config_data["connection_identity_version"] == 1
    assert set(config.config_data["connections_meta"]) == {
        resumed.get_connections()[0].uuid
    }


def test_create_persists_uuid_and_rolls_back_before_registration_on_failure(
    tmp_path,
    monkeypatch,
):
    manager, _config, ssh_path = make_manager(
        tmp_path,
        monkeypatch,
        "# empty\n",
    )
    emitted = []
    manager.emit = lambda signal, connection: emitted.append((signal, connection))
    first = manager.create_connection(
        {
            "nickname": "first",
            "hostname": "first.example",
            "username": "alice",
            "port": 22,
            "protocol": "ssh",
        }
    )
    assert first is not None
    assert first.uuid in ssh_path.read_text()
    assert InProcessClient.connection_id_for(first) == connection_id_from_uuid(first.uuid)

    before = list(manager.get_connections())
    monkeypatch.setattr(
        manager,
        "_safe_write_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    failed = manager.create_connection(
        {
            "nickname": "second",
            "hostname": "second.example",
            "username": "bob",
            "port": 22,
            "protocol": "ssh",
        }
    )
    assert failed is None
    assert manager.get_connections() == before
    assert [signal for signal, _connection in emitted].count("connection-added") == 1


def test_config_atomic_failure_keeps_primary_and_cleans_temporary(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"original": true}\n')
    holder = SimpleNamespace(
        config_file=str(config_path),
        config_data={"replacement": True},
    )
    real_replace = os.replace

    def fail_replace(source, destination):
        if destination == str(config_path):
            raise OSError("disk full")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        Config.save_json_config(
            holder,
            holder.config_data,
            raise_on_error=True,
        )

    assert json.loads(config_path.read_text()) == {"original": True}
    assert not list(tmp_path.glob(".sshpilot-config-*.tmp"))
    assert Path(f"{config_path}.bak").exists()


def test_config_writer_refuses_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "config.json"
    link.symlink_to(target)
    holder = SimpleNamespace(config_file=str(link), config_data={"x": 1})

    with pytest.raises(OSError):
        Config.save_json_config(
            holder,
            holder.config_data,
            raise_on_error=True,
        )
    assert target.read_text() == "{}"


def test_ssh_migration_refuses_symlinked_primary(tmp_path, monkeypatch):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)
    target = tmp_path / "target-ssh-config"
    target.write_text("Host alpha\n    HostName alpha.example\n")
    (ssh_dir / "config").symlink_to(target)
    config = FileConfig(tmp_path / "config.json")
    monkeypatch.setattr(connection_manager_module, "get_ssh_dir", lambda: str(ssh_dir))
    monkeypatch.setattr(
        connection_manager_module,
        "get_config_dir",
        lambda: str(tmp_path),
    )

    manager = ConnectionManager(config)

    assert manager.get_connections() == []
    assert manager.identity_migration_error is not None
    assert "# sshpilot:ConnectionUUID" not in target.read_text()


def test_config_writer_does_not_overwrite_existing_backup(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"original": true}\n')
    backup_path = Path(f"{config_path}.bak")
    backup_path.write_text('{"first_backup": true}\n')
    holder = SimpleNamespace(
        config_file=str(config_path),
        config_data={"replacement": True},
    )

    Config.save_json_config(holder, holder.config_data, raise_on_error=True)

    assert json.loads(config_path.read_text()) == {"replacement": True}
    assert json.loads(backup_path.read_text()) == {"first_backup": True}


def test_daemon_migrates_before_readiness_and_restart_preserves_id(
    tmp_path,
    monkeypatch,
):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)
    ssh_path = ssh_dir / "config"
    ssh_path.write_text("Host alpha\n    HostName alpha.example\n")
    config_path = tmp_path / "config.json"
    FileConfig(config_path)
    monkeypatch.setattr(connection_manager_module, "get_ssh_dir", lambda: str(ssh_dir))
    monkeypatch.setattr(
        connection_manager_module,
        "get_config_dir",
        lambda: str(tmp_path),
    )

    def core_factory():
        manager = ConnectionManager(FileConfig(config_path))
        if manager.identity_migration_error is not None:
            raise RuntimeError("migration failed")
        return InProcessClient(manager, client_name="sshpilotd")

    ids = []
    for index in range(2):
        socket_dir = tmp_path / f"daemon-{index}"
        socket_dir.mkdir(mode=0o700)
        socket_path = socket_dir / "sshpilotd.sock"
        server = DaemonServer(core_factory, socket_path=socket_path)
        server.start_in_thread()
        assert "# sshpilot:ConnectionUUID" in ssh_path.read_text()
        first = DaemonClient(socket_path=socket_path)
        second = DaemonClient(socket_path=socket_path)
        try:
            first_id = first.list_connections()[0].id
            assert second.list_connections()[0].id == first_id
            ids.append(first_id)
        finally:
            first.close()
            second.close()
            server.shutdown()
            assert server.wait_stopped()

    assert ids[0] == ids[1]
