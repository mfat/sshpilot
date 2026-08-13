"""Tests for non-UUID connection identity (Host alias as connection ID)."""

from __future__ import annotations

import threading
from pathlib import Path

from sshpilot.core.connections.models import ConnectionRecord, generate_group_slug
from sshpilot.core.connections.service import ConnectionService
from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.runtime_identity import RuntimeIdAllocator, new_session_id
from sshpilot.ssh_config_document import HostBlock
from sshpilot.ssh_config_formatter import (
    SSH_UUID_MARKER,
    format_ssh_config_entry,
    merged_block_lines,
)


def test_saved_connection_id_is_nickname():
    record = ConnectionRecord.from_dict(
        {"nickname": "production", "hostname": "10.0.0.5", "username": "root"},
        connection_id="production",
    )
    assert record.id == "production"
    assert record.nickname == "production"


def test_connection_record_serialization_has_no_uuid_field():
    record = ConnectionRecord.from_dict(
        {"nickname": "production", "hostname": "10.0.0.5", "username": "root", "uuid": "legacy"}
    )
    payload = record.to_dict()
    assert payload["id"] == "production"
    assert payload["nickname"] == "production"
    assert "uuid" not in payload
    assert record.id == record.nickname


def test_domain_create_uses_nickname_as_id():
    svc = ConnectionService(autosave=False)
    created = svc.create({"nickname": "production", "hostname": "10.0.0.5", "username": "root"})
    assert created.id == "production"
    assert svc.get("production") is not None


def test_domain_alias_rename_is_delete_plus_create():
    svc = ConnectionService(autosave=False)
    events = []
    svc.add_listener(events.append)
    svc.create({"nickname": "production", "hostname": "old.example.com", "username": "root"})
    svc.update("production", {"nickname": "production-new", "hostname": "new.example.com"})
    assert svc.get("production") is None
    assert svc.get("production-new") is not None
    kinds = [e.kind.value for e in events]
    assert "deleted" in kinds
    assert "created" in kinds


def test_domain_alias_preserving_edit_is_update():
    svc = ConnectionService(autosave=False)
    events = []
    svc.add_listener(events.append)
    svc.create({"nickname": "production", "hostname": "old.example.com", "username": "root"})
    events.clear()
    svc.update("production", {"hostname": "new.example.com"})
    assert [e.kind.value for e in events] == ["updated"]
    assert svc.get("production").hostname == "new.example.com"


def test_duplicate_creates_new_alias_backed_id():
    svc = ConnectionService(autosave=False)
    svc.create({"nickname": "production", "hostname": "10.0.0.5", "username": "root"})
    dup = svc.duplicate("production")
    assert dup.id == dup.nickname
    assert dup.id != "production"
    assert svc.get(dup.id) is not None


def test_group_ids_use_slugs():
    assert generate_group_slug("Production Servers", set()) == "production-servers"
    assert generate_group_slug("Production", {"production"}) == "production-2"


def test_group_membership_survives_rename():
    svc = ConnectionService(autosave=False)
    group = svc.create_group("Production")
    svc.create({"nickname": "web", "hostname": "1.2.3.4", "username": "root", "group_id": group.id})
    svc.update("web", {"nickname": "web-new"})
    updated = svc.list_groups()[0]
    assert "web" not in updated.connection_ids
    assert "web-new" in updated.connection_ids


def test_format_ssh_config_entry_omits_uuid_marker():
    text = format_ssh_config_entry(
        {"nickname": "production", "hostname": "10.0.0.5", "username": "root", "uuid": "legacy"}
    )
    assert SSH_UUID_MARKER not in text
    assert text.startswith("Host production\n")


def test_merged_block_drops_legacy_uuid_comments():
    old = HostBlock(
        tokens=["production"],
        lines=[
            "Host production\n",
            f"{SSH_UUID_MARKER} production legacy-id\n",
            "    HostName 10.0.0.5\n",
            "    User root\n",
        ],
    )
    lines = merged_block_lines(
        old,
        {"nickname": "production", "hostname": "10.0.0.5", "username": "root"},
    )
    joined = "".join(lines)
    assert SSH_UUID_MARKER not in joined


def _load(text: str, tmp_path):
    cfg_path = tmp_path / "config"
    cfg_path.write_text(text)
    return load_ssh_configuration(cfg_path, isolated=False)


def test_load_skips_wildcard_and_negated_aliases(tmp_path):
    result = _load(
        """
Host demo
  HostName 1.2.3.4
  User root

Host *
  StrictHostKeyChecking accept-new

Host !negated
  User nobody
""",
        tmp_path,
    )
    assert [c.id for c in result.connections] == ["demo"]
    assert len(result.rules) == 2


def test_load_multi_pattern_concrete_aliases(tmp_path):
    result = _load(
        """
Host production staging qa
  HostName 10.0.0.5
  User root
""",
        tmp_path,
    )
    assert sorted(c.id for c in result.connections) == ["production", "qa", "staging"]


def test_load_ignores_legacy_uuid_comments(tmp_path):
    result = _load(
        f"""
Host production
  {SSH_UUID_MARKER} production 550e8400-e29b-41d4-a716-446655440000
  HostName 10.0.0.5
  User root
""",
        tmp_path,
    )
    assert len(result.connections) == 1
    assert result.connections[0].id == "production"


def test_runtime_session_ids_use_counters():
    first = str(new_session_id())
    second = str(new_session_id())
    assert first.startswith("session-")
    assert second.startswith("session-")
    assert first != second


def test_runtime_id_allocator_is_thread_safe():
    allocator = RuntimeIdAllocator("session")
    seen = []
    lock = threading.Lock()

    def worker():
        local = [allocator.allocate() for _ in range(50)]
        with lock:
            seen.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(seen) == len(set(seen)) == 400


def test_no_active_uuid_imports_in_src():
    root = Path(__file__).resolve().parents[1] / "src" / "sshpilot"
    offenders = []
    for path in root.rglob("*.py"):
        if "plugins/examples" in str(path):
            continue
        text = path.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "import uuid" in line or "from uuid" in line:
                offenders.append(f"{path}:{i}:{line.strip()}")
    assert offenders == []
