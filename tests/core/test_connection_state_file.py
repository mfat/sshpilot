"""Dedicated connection-state file read/write and legacy migration tests."""

import json
import os
import stat

import pytest

from sshpilot.core.connections.state_file import (
    ConnectionFileState,
    GroupFileState,
    probe_connection_state_file,
    read_connection_state,
    read_legacy_connection_state,
    write_connection_state,
)
from sshpilot.core.errors import CoreError, ErrorCode


def _state(**overrides):
    defaults = dict(
        version=1,
        non_ssh_connections=(
            {"nickname": "switch", "protocol": "telnet", "hostname": "192.168.1.1"},
        ),
        groups=(
            GroupFileState(
                id="servers",
                name="Servers",
                parent_id=None,
                order=0,
                color="#ff0000",
                connection_ids=("switch",),
            ),
        ),
        root_connections=("gateway",),
        metadata={"switch": {"tags": ["network"], "pinned": True}},
    )
    defaults.update(overrides)
    return ConnectionFileState(**defaults)


# ---------------------------------------------------------------------------
# Fresh file
# ---------------------------------------------------------------------------
def test_missing_file_initializes_defaults(tmp_path):
    state = read_connection_state(tmp_path / "connections.json")
    assert state.version == 1
    assert state.non_ssh_connections == ()
    assert state.groups == ()
    assert state.root_connections == ()
    assert state.metadata == {}


def test_write_then_read_round_trip(tmp_path):
    path = tmp_path / "connections.json"
    write_connection_state(path, _state())
    state = read_connection_state(path)
    assert state.version == 1
    assert state.non_ssh_connections == _state().non_ssh_connections
    assert state.groups == _state().groups
    assert state.root_connections == ("gateway",)
    assert state.metadata == {"switch": {"tags": ("network",), "pinned": True}}


def test_written_file_has_expected_schema(tmp_path):
    path = tmp_path / "connections.json"
    write_connection_state(path, _state())
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert isinstance(data["non_ssh_connections"], list)
    assert set(data["groups"]) == {"groups", "root_connections"}
    assert isinstance(data["metadata"], dict)


def test_written_file_is_mode_0600_for_new_files(tmp_path):
    path = tmp_path / "connections.json"
    write_connection_state(path, _state())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_mode_is_preserved(tmp_path):
    path = tmp_path / "connections.json"
    write_connection_state(path, _state())
    path.chmod(0o640)
    write_connection_state(path, _state())
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------
def _legacy_config(tmp_path, **overrides):
    data = {
        "connections": {
            "non_ssh": [
                {"nickname": "switch", "protocol": "telnet", "hostname": "192.168.1.1"}
            ]
        },
        "connection_groups": {
            "groups": {
                "servers": {
                    "id": "servers",
                    "name": "Servers",
                    "parent_id": None,
                    "children": [],
                    "connections": ["switch"],
                    "expanded": True,
                    "order": 0,
                    "color": "#ff0000",
                }
            },
            "connections": {"switch": "servers"},
            "root_connections": ["gateway"],
            "identity_schema_version": 1,
        },
        "connections_meta": {
            "switch": {"tags": ["network"], "pinned": True},
        },
        "unrelated": {"keep": "me"},
    }
    data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_legacy_migration_normalizes_all_sections(tmp_path):
    config = _legacy_config(tmp_path)
    state = read_legacy_connection_state(config)
    assert state.version == 1
    assert state.non_ssh_connections == (
        {"nickname": "switch", "protocol": "telnet", "hostname": "192.168.1.1"},
    )
    assert len(state.groups) == 1
    group = state.groups[0]
    assert group.id == "servers"
    assert group.name == "Servers"
    assert group.color == "#ff0000"
    assert group.connection_ids == ("switch",)
    assert state.root_connections == ("gateway",)
    assert state.metadata == {"switch": {"tags": ("network",), "pinned": True}}


def test_migration_does_not_modify_legacy_config(tmp_path):
    config = _legacy_config(tmp_path)
    before = config.read_text(encoding="utf-8")
    read_legacy_connection_state(config)
    assert config.read_text(encoding="utf-8") == before


def test_migration_ignores_unrelated_legacy_fields(tmp_path):
    config = _legacy_config(tmp_path)
    state = read_legacy_connection_state(config)
    assert state.metadata == {"switch": {"tags": ("network",), "pinned": True}}


def test_v1_reader_preserves_historical_compatibility_normalization(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "non_ssh_connections": [
                    {"id": "switch", "protocol": "telnet"},
                    "malformed-non-ssh",
                ],
                "groups": {
                    "groups": {
                        "work": {
                            "id": "work",
                            "name": "Work",
                            "connection_ids": ["switch", 7, "", None],
                        },
                        "malformed": "ignored",
                    },
                    "root_connections": [7, "gateway", "", None],
                },
                "metadata": {
                    "switch": {"pinned": True},
                    "ignored-list": ["not an object"],
                    "ignored-string": "not an object",
                },
            }
        ),
        encoding="utf-8",
    )

    state = read_connection_state(path)

    assert state.non_ssh_connections == (
        {"id": "switch", "protocol": "telnet"},
    )
    assert len(state.groups) == 1
    assert state.groups[0].connection_ids == ("switch", "7", "None")
    assert state.root_connections == ("7", "gateway", "None")
    assert state.metadata == {"switch": {"pinned": True}}


def test_probe_delegates_tolerant_v1_payload_to_authoritative_reader(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "non_ssh_connections": [],
                "groups": {"groups": {}, "root_connections": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    parsed = read_connection_state(path)
    assert probe_connection_state_file(path).value == "v1"
    assert parsed == ConnectionFileState()


def test_missing_legacy_config_yields_defaults(tmp_path):
    state = read_legacy_connection_state(tmp_path / "missing.json")
    assert state.version == 1
    assert state.groups == ()
    assert state.metadata == {}


def test_migration_rejects_secret_like_metadata_keys(tmp_path):
    config = _legacy_config(
        tmp_path,
        connections_meta={
            "switch": {"api_password": "hunter2", "tags": ["network"]},
        },
    )
    with pytest.raises(CoreError) as excinfo:
        read_legacy_connection_state(config)
    assert excinfo.value.diagnostic_category == "invalid_metadata"


def test_migration_rejects_unusable_metadata_entries(tmp_path):
    config = _legacy_config(
        tmp_path,
        connections_meta={
            "switch": {"tags": [1, 2, 3], "bad": {"nested": float("nan")}},
        },
    )
    with pytest.raises(CoreError):
        read_legacy_connection_state(config)


# ---------------------------------------------------------------------------
# Malformed / unsafe files
# ---------------------------------------------------------------------------
def test_malformed_json_is_an_error(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CoreError) as excinfo:
        read_connection_state(path)
    assert excinfo.value.code is ErrorCode.CONNECTION_STATE_IO_ERROR
    assert excinfo.value.diagnostic_category == "invalid_json"
    assert excinfo.value.diagnostic_reason == "state file contains invalid JSON"


def test_non_object_root_is_an_error(tmp_path):
    path = tmp_path / "connections.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(CoreError):
        read_connection_state(path)


def test_invalid_utf8_is_an_error(tmp_path):
    path = tmp_path / "connections.json"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(CoreError):
        read_connection_state(path)


def test_oversized_file_is_an_error(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    monkeypatch.setattr(
        "sshpilot.core.connections.state_file._MAX_STATE_BYTES", 16
    )
    with pytest.raises(CoreError):
        read_connection_state(path)


def test_unsafe_metadata_in_file_is_an_error(tmp_path):
    path = tmp_path / "connections.json"
    data = {
        "version": 1,
        "non_ssh_connections": [],
        "groups": {"groups": {}, "root_connections": []},
        "metadata": {"conn-1": {"api_password": "x"}},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CoreError):
        read_connection_state(path)


# ---------------------------------------------------------------------------
# Symlink handling
# ---------------------------------------------------------------------------
def test_read_refuses_symlink(tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps({"version": 1}), encoding="utf-8")
    link = tmp_path / "connections.json"
    link.symlink_to(target)
    with pytest.raises(CoreError) as excinfo:
        read_connection_state(link)
    assert excinfo.value.code is ErrorCode.CONNECTION_STATE_IO_ERROR


def test_write_refuses_symlink(tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps({"version": 1}), encoding="utf-8")
    link = tmp_path / "connections.json"
    link.symlink_to(target)
    with pytest.raises(CoreError):
        write_connection_state(link, _state())
    assert target.read_text(encoding="utf-8") == json.dumps({"version": 1})


def test_write_refuses_symlink_swap_before_replace(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    write_connection_state(path, _state())

    def swap_to_symlink(*_args, **_kwargs):
        path.unlink()
        path.symlink_to(tmp_path / "victim")
        raise OSError("simulated")
        return True

    monkeypatch.setattr(
        "sshpilot.core.connections.state_file.os.replace", swap_to_symlink
    )
    with pytest.raises(CoreError):
        write_connection_state(path, _state())
    assert (tmp_path / "victim").exists() is False


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------
def test_write_type_error_on_wrong_state(tmp_path):
    with pytest.raises(TypeError):
        write_connection_state(tmp_path / "connections.json", object())


def test_write_cleans_up_temp_files_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    monkeypatch.setattr(
        "sshpilot.core.connections.state_file.os.replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(CoreError):
        write_connection_state(path, _state())
    leftovers = [
        p for p in tmp_path.iterdir() if p.name.startswith(".connections-")
    ]
    assert leftovers == []


def test_write_mkstemp_failure_is_core_error(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"

    def fail_mkstemp(*_args, **_kwargs):
        raise OSError("no space")

    monkeypatch.setattr(
        "sshpilot.core.connections.state_file.tempfile.mkstemp", fail_mkstemp
    )
    with pytest.raises(CoreError) as excinfo:
        write_connection_state(path, _state())
    assert excinfo.value.code is ErrorCode.CONNECTION_STATE_IO_ERROR
    assert not path.exists()


def test_write_failure_leaves_previous_file_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    write_connection_state(path, _state())
    before = path.read_bytes()
    monkeypatch.setattr(
        "sshpilot.core.connections.state_file.os.replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(CoreError):
        write_connection_state(path, _state(version=1))
    assert path.read_bytes() == before


def test_existing_file_metadata_failure_is_an_error(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    write_connection_state(path, _state())

    def fail_lstat(*_args, **_kwargs):
        raise OSError("stale handle")

    monkeypatch.setattr(
        "sshpilot.core.connections.state_file.os.lstat", fail_lstat
    )
    with pytest.raises(CoreError):
        write_connection_state(path, _state())


# ---------------------------------------------------------------------------
# Durability details
# ---------------------------------------------------------------------------
def test_parent_directory_fsync_uses_o_directory(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "dir" / "connections.json"
    opened_flags = []

    original_open = os.open

    def spy_open(target, flags, *args, **kwargs):
        opened_flags.append(flags)
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)
    write_connection_state(path, _state())
    if hasattr(os, "O_DIRECTORY"):
        assert any(flags & os.O_DIRECTORY for flags in opened_flags)
    else:
        assert opened_flags


def test_parent_directory_fsync_failure_reported(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    write_connection_state(path, _state())

    def fail_fsync(fd):
        raise OSError("fsync failed")

    original_fsync = os.fsync
    dir_fds = []

    def spy_fsync(fd):
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                dir_fds.append(fd)
        except OSError:
            pass
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(
        "sshpilot.core.connections.state_file.os.fsync", fail_fsync
    )
    # Replacement already succeeded; the durability error is surfaced as a
    # CoreError (documented behavior: durability unknown).
    with pytest.raises(CoreError):
        write_connection_state(path, _state())


# ---------------------------------------------------------------------------
# Error hygiene
# ---------------------------------------------------------------------------
def test_errors_never_contain_paths(tmp_path):
    target = tmp_path / "secret-dir" / "connections.json"
    target.parent.mkdir()
    target.write_text("{broken", encoding="utf-8")
    with pytest.raises(CoreError) as excinfo:
        read_connection_state(target)
    message = str(excinfo.value)
    assert "connections.json" not in message
    assert str(tmp_path) not in message
    assert "secret-dir" not in message


def test_symlink_error_does_not_contain_path(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "connections.json"
    link.symlink_to(target)
    with pytest.raises(CoreError) as excinfo:
        read_connection_state(link)
    assert str(tmp_path) not in str(excinfo.value)
