"""Key and known-hosts core services."""
from __future__ import annotations

from pathlib import Path

from sshpilot.core.keys import KeyService, looks_like_private_key
from sshpilot.core.known_hosts import (
    filter_entries,
    load_known_hosts,
    plan_remove_entries,
    save_known_hosts,
)
from sshpilot.core.ssh import ProcessSpec


def test_looks_like_private_key(tmp_path: Path):
    key = tmp_path / "id_ed25519"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\ndata\n-----END OPENSSH PRIVATE KEY-----\n")
    assert looks_like_private_key(key) is True
    other = tmp_path / "notes.txt"
    other.write_text("hello")
    assert looks_like_private_key(other) is False


def test_discover_keys(tmp_path: Path):
    key = tmp_path / "id_test"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----\n")
    (tmp_path / "id_test.pub").write_text("ssh-ed25519 AAAA comment\n")
    service = KeyService(tmp_path)
    found = service.discover_keys()
    assert any(k.private_path.endswith("id_test") for k in found)


def test_known_hosts_round_trip(tmp_path: Path):
    path = tmp_path / "known_hosts"
    save_known_hosts(
        path,
        [
            "example.com ssh-ed25519 AAAAexample",
            "other.example ssh-rsa BBBB",
        ],
    )
    entries = load_known_hosts(path)
    assert len(entries) == 2
    filtered = filter_entries(entries, "other")
    assert len(filtered) == 1
    plan = plan_remove_entries(path, entries, [entries[0].line])
    save_known_hosts(path, plan.entries)
    remaining = load_known_hosts(path)
    assert len(remaining) == 1
    assert remaining[0].hostname == "other.example"


def test_process_spec_from_ssh_command():
    class _Cmd:
        command = ["ssh", "-F", "/tmp/cfg", "host"]
        env = {"FOO": "1"}

    spec = ProcessSpec.from_ssh_connection_command(_Cmd())
    assert spec.argv[0] == "ssh"
    assert spec.env["FOO"] == "1"
