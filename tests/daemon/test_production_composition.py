"""Daemon production composition tests (M3 connection-store ownership).

``cli._production_core_services()`` must compose the headless
``ConnectionRepository`` (SSH config store + ``connections.json``) and the
daemon compatibility providers without ever constructing ``Config``,
``ConnectionManager``, or ``GroupManager``, and without touching a
developer's real config.
"""

from __future__ import annotations

import json
import logging

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.daemon.connection_launch_provider import DaemonConnectionLaunchProvider
from sshpilot.daemon.connection_secret_provider import DaemonConnectionSecretProvider
from sshpilot.daemon.identity_service import DaemonIdentityService
from sshpilot.daemon.key_service import DaemonKeyService
from sshpilot.daemon.known_hosts_service import KnownHostsService
from sshpilot.daemon.operation_runtime import OperationRuntime
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


def test_production_composition_shares_operation_runtime_with_identity_service(
    tmp_path, monkeypatch
):
    services = _compose(tmp_path, monkeypatch)

    assert isinstance(services.identity, DaemonIdentityService)
    assert isinstance(services.operations, OperationRuntime)
    assert services.identity._operations is services.operations


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


def test_production_composition_survives_unsafe_legacy_metadata(
    tmp_path, monkeypatch, caplog
):
    ssh_dir = tmp_path / "ssh"
    xdg_config_dir = tmp_path / "config"
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(ssh_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_dir))

    ssh_dir.mkdir(parents=True)
    (ssh_dir / "config").write_text(
        "Host web\n    HostName web.example\n", encoding="utf-8"
    )
    config_dir = xdg_config_dir / "sshpilot"
    config_dir.mkdir(parents=True)
    legacy_path = config_dir / "config.json"
    legacy_path.write_text(
        json.dumps(
            {"connections_meta": {"old-host": {"password": "secret"}}}
        ),
        encoding="utf-8",
    )
    legacy_before = legacy_path.read_bytes()

    from sshpilot.daemon import cli

    with caplog.at_level(logging.WARNING):
        services = cli._production_core_services()

    repository = services.connections._repository
    assert [connection.id for connection in repository.snapshot().connections] == [
        "web"
    ]
    assert (config_dir / "connections.json").exists()
    assert legacy_path.read_bytes() == legacy_before
    assert repository.snapshot().metadata == ()
    assert "secret" not in caplog.text


def test_production_composition_survives_legacy_group_parent_cycle(
    tmp_path, monkeypatch, caplog
):
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "config").write_text(
        "Host web\n    HostName web.example\n", encoding="utf-8"
    )
    config_json = {
        "connection_groups": {
            "groups": {
                "a": {"name": "A", "parent_id": "b", "connections": []},
                "b": {"name": "B", "parent_id": "a", "connections": []},
            }
        }
    }

    with caplog.at_level(logging.WARNING):
        services = _compose(tmp_path, monkeypatch, config_json=config_json)

    repository = services.connections._repository
    assert [connection.id for connection in repository.snapshot().connections] == [
        "web"
    ]
    assert not (get_config_dir() / "connections.json").exists()
    assert "reason=ValueError" in caplog.text


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
    )
    for name in forbidden:
        assert name not in source, f"cli.py must not reference {name}"


def test_production_composition_connections_capabilities(tmp_path, monkeypatch):
    services = _compose(tmp_path, monkeypatch)
    capabilities = services.connections.get_capabilities()
    values = {item.value for item in capabilities.supported}
    assert "connections.read" in values
    assert "connections.write" in values


def test_production_overrides_service_always_injects_controlmaster_args(
    tmp_path, monkeypatch
):
    """Task 6: the multiplex args are injected unconditionally; the loaded
    ``ssh.controlmaster`` value decides whether the fragment is composed."""
    from sshpilot.core.settings import load_settings_strict, save_settings
    from sshpilot.core.settings.defaults import CONFIG_VERSION

    services = _compose(tmp_path, monkeypatch)
    overrides = services.ssh_overrides
    assert overrides._controlmaster_extra is not None
    assert "ControlMaster=auto" in overrides._controlmaster_extra

    config_path = get_config_dir() / "config.json"

    def _set_controlmaster(enabled):
        save_settings(
            config_path,
            {"config_version": CONFIG_VERSION, "ssh": {"controlmaster": enabled}},
        )
        load_settings_strict(config_path)

    _set_controlmaster(False)
    ssh = overrides.get_ssh_config()
    assert "ControlMaster=auto" not in " ".join(ssh["ssh_overrides"])

    # Same long-lived service composes the fragment once the file flips.
    _set_controlmaster(True)
    ssh = overrides.get_ssh_config()
    assert "ControlMaster=auto" in " ".join(ssh["ssh_overrides"])


def test_production_daemon_export_discovers_both_saved_connections(
    tmp_path, monkeypatch
):
    """The production composition wires ``connections_source`` so the daemon
    export discovers every saved connection and exports their selected
    credentials (regression: the repository object used to be passed and
    ``_selected_views`` could not iterate it)."""
    import sshpilot.backup_manager as bm
    import sshpilot.credential_manager as cmod
    import sshpilot.secret_storage as ss
    from sshpilot.backup_archive import read_spbk
    from sshpilot.platform.paths import get_config_dir, get_ssh_dir
    from sshpilot.secret_storage import password_spec

    # A dict-backed SecretManager shared by the daemon service and the
    # credential-gather path (the production process uses the singleton).
    class FakeMgr:
        def __init__(self):
            self.data = {}
            self._selected = "auto"
            self.active_backend_name = "libsecret"

        def store(self, spec, secret):
            self.data[spec.keyring_account] = secret
            return True

        def lookup(self, spec):
            return self.data.get(spec.keyring_account)

        def lookup_everywhere(self, spec):
            value = self.data.get(spec.keyring_account)
            return (value, "libsecret") if value is not None else None

        def all_available_backends(self):
            return []

        def persists_secrets(self):
            return True

        def registered_backends(self):
            return ["libsecret"]

        def available_backends(self, *, cheap=False):
            return ["libsecret"]

        def get_backend(self, name):
            return None

        def set_selected(self, name):
            self._selected = name or "auto"

        def selected_backend(self):
            return None

        def unlock_selected(self, secret, progress=None):
            return True

    # ``BackupManager`` resolves its paths through ``platform_utils``; point it
    # at the isolated config/ssh dirs the composition already uses.
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(get_config_dir()))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(get_ssh_dir()))

    fake = FakeMgr()
    fake.data[password_spec("one.example", "alice").keyring_account] = "pw-1"
    fake.data[password_spec("two.example", "bob").keyring_account] = "pw-2"
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)

    services = _compose(tmp_path, monkeypatch)
    repository = services.connections._repository
    repository.create_connection(
        {"nickname": "one", "hostname": "one.example", "username": "alice", "port": 22}
    )
    repository.create_connection(
        {"nickname": "two", "hostname": "two.example", "username": "bob", "port": 22}
    )
    assert {r.nickname for r in repository.list_records()} == {"one", "two"}

    destination = tmp_path / "production.spbk"
    result = services.secrets.export_backup(
        destination=str(destination),
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": True,
            "private_keys": False,
        },
        owner_client_id="client-1",
    )
    assert result.status.value == "success", result.message
    assert result.counts["credentials"] == 2

    manifest = read_spbk(str(destination), None)
    creds = {(c["type"], c["id"]): c["secret"] for c in manifest["credentials"]}
    assert creds[("password", "alice@one.example")] == "pw-1"
    assert creds[("password", "bob@two.example")] == "pw-2"
