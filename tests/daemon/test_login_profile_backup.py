"""Login profiles in backups: embedded section, secrets as credentials, restore."""


from sshpilot.api.models.secrets import SecretTransferMessageCode
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.login_profiles.models import LINK_EXPLICIT
from sshpilot.core.login_profiles.service import LoginProfileService
from sshpilot.credential_model import Credential, credential_to_spec
from sshpilot.daemon.connection_secret_provider import DaemonLoginProfileSecretStore
from sshpilot.daemon.login_profile_backup import (
    SECTION_KEY,
    ConnectionStoreBackupRestore,
    ConnectionStoreBackupSnapshot,
)
from sshpilot.daemon.secret_transfer import _backup_manager


class _Manager:
    def __init__(self):
        self.values = {}

    def store(self, spec, value):
        self.values[spec.keyring_account, spec.attributes.get("host")] = value
        return True

    def lookup(self, spec):
        return self.values.get((spec.keyring_account, spec.attributes.get("host")))

    def delete(self, spec):
        return self.values.pop((spec.keyring_account, spec.attributes.get("host")), None) is not None


def _env(tmp_path, name):
    base = tmp_path / name
    base.mkdir()
    root = base / "config"
    root.write_text("Host web1\n    HostName web1.example.com\n    User alice\n")
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=base / "connections.json",
        legacy_config_path=base / "config.json",
        isolated=False,
    )
    manager = _Manager()
    service = LoginProfileService(
        repo, base / "login_profiles.json",
        secret_store=DaemonLoginProfileSecretStore(lambda: manager),
    )
    return repo, service, manager


def test_snapshot_embeds_profiles_and_exports_secrets(tmp_path):
    repo, service, _manager = _env(tmp_path, "a")
    profile = service.create_profile({"name": "Deploy", "username": "deploy"})
    service.set_password(profile.id, "pw1")
    service.set_sudo_password(profile.id, "pw2")
    service.assign_connections(["web1"], LINK_EXPLICIT, profile.id)

    snapshot = ConnectionStoreBackupSnapshot(repo.snapshot_for_backup, service)
    section = snapshot()
    assert profile.id in section[SECTION_KEY]["profiles"]
    assert "pw1" not in repr(section)

    creds = snapshot.backup_credentials()
    assert {(c["username"], c["secret"]) for c in creds} == {("login", "pw1"), ("sudo", "pw2")}
    # Each exported credential maps back to the exact spec the profile uses.
    for c in creds:
        spec = credential_to_spec(Credential(
            id=c["id"], type=c["type"], host=c["host"], username=c["username"],
            secret=c["secret"], metadata=c["metadata"],
        ))
        expected = DaemonLoginProfileSecretStore._spec(profile.id, c["username"])
        assert spec.keyring_account == expected.keyring_account
        assert spec.attributes == expected.attributes


def test_backup_manager_includes_profile_credentials(tmp_path):
    repo, service, _manager = _env(tmp_path, "a")
    profile = service.create_profile({"name": "Deploy"})
    service.set_password(profile.id, "pw1")
    mgr = _backup_manager(
        tmp_path / "settings.json",
        connection_store_snapshot=ConnectionStoreBackupSnapshot(repo.snapshot_for_backup, service),
    )
    creds = mgr._gather_credentials([])
    assert [c["secret"] for c in creds] == ["pw1"]


def test_restore_recreates_profiles_and_links(tmp_path):
    repo_a, service_a, _ = _env(tmp_path, "a")
    profile = service_a.create_profile({"name": "Deploy", "username": "deploy"})
    service_a.assign_connections(["web1"], LINK_EXPLICIT, profile.id)
    section = ConnectionStoreBackupSnapshot(repo_a.snapshot_for_backup, service_a)()

    repo_b, service_b, _ = _env(tmp_path, "b")
    (tmp_path / "b" / "config").write_text((tmp_path / "a" / "config").read_text())
    restore = ConnectionStoreBackupRestore(repo_b.restore_connection_store, service_b)
    result = restore(section, mode="merge")
    assert result.warnings == ()
    assert service_b.get_profile(profile.id).name == "Deploy"
    assert service_b.effective_for("web1").profile.id == profile.id


def test_invalid_profiles_section_is_a_warning_not_a_failure(tmp_path):
    repo, service, _ = _env(tmp_path, "a")
    section = dict(repo.snapshot_for_backup())
    section[SECTION_KEY] = {"version": 99}
    result = ConnectionStoreBackupRestore(repo.restore_connection_store, service)(section)
    assert [w.code for w in result.warnings] == [
        SecretTransferMessageCode.CONNECTION_STORE_RESTORE_FAILED
    ]
