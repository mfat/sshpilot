"""Unsaved targets the daemon can open by id but never stores."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core.connections.repository import ConnectionRepository  # noqa: E402
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402
from sshpilot.core.connections.transient import (  # noqa: E402
    MAX_TRANSIENT_CONNECTIONS,
    TRANSIENT_ID_PREFIX,
)
from sshpilot.daemon.connection_launch_provider import (  # noqa: E402
    DaemonConnectionLaunchProvider,
)


def _repo(tmp_path, config=""):
    root = tmp_path / "ssh_config"
    if config:
        root.write_text(config)
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    ), root


def test_ssh_target_is_openable_but_never_stored(tmp_path):
    repo, root = _repo(tmp_path, "Host saved\n    HostName 10.0.0.9\n")
    before = root.read_text()

    record = repo.open_transient_connection(
        {"nickname": "box", "hostname": "box.example", "username": "alice",
         "port": 2222, "protocol": "ssh"}
    )

    assert record.id.startswith(TRANSIENT_ID_PREFIX)
    assert repo.get_record(record.id).hostname == "box.example"
    assert root.read_text() == before
    assert [r.id for r in repo.list_records()] == ["saved"]
    assert record.id not in {c.id for c in repo.snapshot().connections}


def test_ssh_target_keeps_the_users_host_patterns(tmp_path):
    repo, _root = _repo(
        tmp_path, "Host *.corp\n    User corpuser\n    ServerAliveInterval 7\n"
    )
    record = repo.open_transient_connection(
        {"nickname": "box", "hostname": "box.corp", "port": 2222, "protocol": "ssh"}
    )
    provider = DaemonConnectionLaunchProvider(repo.get_record)

    argv, _env = provider.prepare_terminal_launch(
        record.id, interaction_policy="broker"
    )

    # Connects to the host the user named, through a config that holds the
    # typed values first and then includes the real one.
    assert argv[-1] == "box.corp"
    config = Path(argv[argv.index("-F") + 1])
    text = config.read_text()
    assert text.index("Host box.corp") < text.index("Include")
    assert str(tmp_path / "ssh_config") in text
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"


def test_plugin_target_carries_its_fields(tmp_path):
    repo, _root = _repo(tmp_path)
    record = repo.open_transient_connection(
        {"nickname": "lab", "hostname": "10.0.0.5", "port": 2323,
         "protocol": "telnet", "host": "10.0.0.5"}
    )
    stored = repo.get_record(record.id)
    assert stored.protocol == "telnet"
    assert stored.data["host"] == "10.0.0.5"
    assert stored.port == 2323
    assert repo.list_records() == ()


def test_discard_removes_record_and_its_config(tmp_path):
    repo, _root = _repo(tmp_path)
    record = repo.open_transient_connection(
        {"nickname": "box", "hostname": "box.example", "protocol": "ssh"}
    )
    config = Path(record.source)
    assert config.exists()

    assert repo.discard_transient_connection(record.id)
    assert repo.get_record(record.id) is None
    assert not config.exists()


def test_oldest_targets_are_evicted(tmp_path):
    repo, _root = _repo(tmp_path)
    first = repo.open_transient_connection(
        {"nickname": "t", "hostname": "first.example", "protocol": "telnet"}
    )
    for index in range(MAX_TRANSIENT_CONNECTIONS):
        repo.open_transient_connection(
            {"nickname": "t", "hostname": f"h{index}.example", "protocol": "telnet"}
        )
    assert repo.get_record(first.id) is None


def test_unknown_ids_still_miss(tmp_path):
    repo, _root = _repo(tmp_path)
    assert repo.get_record(TRANSIENT_ID_PREFIX + "nope") is None
    assert repo.get_record("nope") is None
