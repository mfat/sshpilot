"""Daemon production composition tests (M3 connection-store ownership).

``cli._production_core_services()`` must compose the headless
``ConnectionRepository`` (SSH config store + ``connections.json``) and the
daemon compatibility providers without ever constructing ``Config``,
``ConnectionManager``, or ``GroupManager``, and without touching a
developer's real config.
"""

from __future__ import annotations

import json

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.daemon.connection_launch_provider import DaemonConnectionLaunchProvider
from sshpilot.daemon.connection_secret_provider import DaemonConnectionSecretProvider
from sshpilot.daemon.key_service import DaemonKeyService
from sshpilot.daemon.known_hosts_service import KnownHostsService
from sshpilot.platform.paths import get_config_dir, get_ssh_dir


def _compose(tmp_path, monkeypatch, *, config_json=None):
    """Run the production composition against isolated headless paths."""
    ssh_dir = tmp_path / "ssh"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(ssh_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    if config_json is not None:
        app_dir = get_config_dir()
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "config.json").write_text(
            json.dumps(config_json), encoding="utf-8"
        )
    from sshpilot.daemon import cli

    return cli._production_core_services()


def test_production_composition_installs_connection_repository(tmp_path, monkeypatch):
    services = _compose(tmp_path, monkeypatch)

    assert isinstance(services.connections, ConnectionApplicationService)
    assert isinstance(services.connections._repository, ConnectionRepository)


def test_production_composition_installs_daemon_providers(tmp_path, monkeypatch):
    services = _compose(tmp_path, monkeypatch)

    assert isinstance(
        services.connections._launch_provider, DaemonConnectionLaunchProvider
    )
    assert isinstance(
        services.connections._secret_provider, DaemonConnectionSecretProvider
    )


def test_production_composition_installs_known_hosts_and_keys(tmp_path, monkeypatch):
    services = _compose(tmp_path, monkeypatch)

    assert isinstance(services.known_hosts, KnownHostsService)
    assert isinstance(services.keys, DaemonKeyService)


def test_production_repository_uses_daemon_owned_paths(tmp_path, monkeypatch):
    services = _compose(tmp_path, monkeypatch)
    repository = services.connections._repository

    # Non-isolated default: ~/.ssh/config as the root SSH config, and
    # <config-dir>/connections.json for daemon-owned non-SSH state.
    assert repository._ssh_store._root_path == get_ssh_dir() / "config"
    assert repository._state_path == get_config_dir() / "connections.json"


def test_production_composition_honors_isolated_config(tmp_path, monkeypatch):
    services = _compose(
        tmp_path, monkeypatch, config_json={"ssh": {"use_isolated_config": True}}
    )
    repository = services.connections._repository

    assert repository._isolated is True
    assert repository._ssh_store._root_path == get_config_dir() / "ssh_config"


def test_production_composition_migrates_legacy_state(tmp_path, monkeypatch):
    config_json = {
        "connections": {
            "non_ssh": [
                {
                    "id": "telnet-box",
                    "nickname": "telnet-box",
                    "protocol": "telnet",
                    "host": "box",
                    "username": "user",
                }
            ]
        },
        "connection_groups": {
            "groups": {"servers": {"name": "servers"}},
            "root_connections": [],
        },
    }
    services = _compose(tmp_path, monkeypatch, config_json=config_json)

    repository = services.connections._repository
    snapshot = repository.snapshot()
    ids = {connection.id for connection in snapshot.connections}
    assert "telnet-box" in ids

    # The dedicated state file now exists; legacy values remain untouched.
    state_path = get_config_dir() / "connections.json"
    assert state_path.exists()
    legacy = json.loads((get_config_dir() / "config.json").read_text(encoding="utf-8"))
    assert legacy["connections"]["non_ssh"][0]["id"] == "telnet-box"
    assert "connection_groups" in legacy


def test_production_composition_does_not_load_plugins_or_legacy_managers(
    tmp_path, monkeypatch
):
    """The daemon must not construct legacy persistence objects at startup."""
    import ast
    from pathlib import Path

    cli_path = Path("src/sshpilot/daemon/cli.py")
    source = cli_path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(cli_path))

    forbidden = (
        "ConnectionManager",
        "GroupManager",
        "load_plugins",
        "AuthoritativeConfigurationBackend",
    )
    for name in forbidden:
        assert name not in source, f"cli.py must not reference {name}"


def test_production_composition_connections_capabilities(tmp_path, monkeypatch):
    services = _compose(tmp_path, monkeypatch)
    capabilities = services.connections.get_capabilities()
    values = {item.value for item in capabilities.supported}
    assert "connections.read" in values
    assert "connections.write" in values
