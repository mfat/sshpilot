"""Tests for the .spbk credential gather/restore in BackupManager."""

import sshpilot.backup_manager as bm
import sshpilot.credential_manager as cmod
import sshpilot.secret_storage as ss
from sshpilot.secret_storage import password_spec, sudo_password_spec
from sshpilot.backup_archive import read_spbk


class FakeMgr:
    """Minimal SecretManager stand-in: a dict store + the two lookups used here."""

    def __init__(self):
        self.data = {}

    def store(self, spec, secret):
        self.data[spec.keyring_account] = secret
        return True

    def lookup_everywhere(self, spec):
        v = self.data.get(spec.keyring_account)
        return (v, "libsecret") if v else None

    def all_available_backends(self):
        return []


class FakeConn:
    def __init__(self, nickname, hostname, username):
        self.nickname = nickname
        self.hostname = hostname
        self.host = ""
        self.username = username
        self.port = 22
        self.keyfile = ""
        self.identity_files = []

    def get_effective_host(self):
        return self.hostname


class FakeConfig:
    def get_setting(self, key, default=None):
        return default

    def get_default_config(self):
        return {"some": "config"}


class FakeConnMgr:
    def __init__(self, conns, ssh_config_path="", known_hosts_path=None, isolated_mode=False):
        self._conns = conns
        self.ssh_config_path = ssh_config_path
        self.known_hosts_path = known_hosts_path
        self.isolated_mode = isolated_mode

    def get_connections(self):
        return list(self._conns)

    def load_ssh_config(self):
        pass


def test_spbk_export_gathers_selected_credentials_and_restores(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))   # no real config dir writes

    fake = FakeMgr()
    fake.data[password_spec("h.example", "alice").keyring_account] = "pw-a"
    fake.data[sudo_password_spec("h.example", "alice").keyring_account] = "sudo-a"
    # a secret for a connection NOT selected for export — must not be included
    fake.data[password_spec("other", "bob").keyring_account] = "pw-b"

    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)      # export-side gather
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)        # restore-side store

    selected = FakeConn("A", "h.example", "alice")
    other = FakeConn("B", "other", "bob")
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([selected, other]))

    path = str(tmp_path / "backup.spbk")
    ok, err = mgr.export_backup(path, connections=[selected], passphrase="secret")
    assert ok, err

    manifest = read_spbk(path, "secret")
    creds = {(c["type"], c["id"]): c for c in manifest["credentials"]}
    assert creds[("password", "alice@h.example")]["secret"] == "pw-a"
    assert creds[("sudo", "sudo:alice@h.example")]["secret"] == "sudo-a"
    assert ("password", "bob@other") not in creds         # non-selected connection excluded

    # Restore into an empty store.
    fake.data.clear()
    restored = mgr._restore_credentials(manifest)
    assert restored == 2
    assert fake.data[password_spec("h.example", "alice").keyring_account] == "pw-a"
    assert fake.data[sudo_password_spec("h.example", "alice").keyring_account] == "sudo-a"


def test_restore_count_reflects_failed_stores(monkeypatch, tmp_path):
    # A locked/unavailable backend makes store() return False; _restore_credentials returns the
    # number actually saved, so the UI can detect a partial restore (and prompt to unlock).
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))

    class LockedMgr(FakeMgr):
        def store(self, spec, secret):
            return False                          # e.g. a locked session vault

    monkeypatch.setattr(ss, "get_secret_manager", lambda: LockedMgr())
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    manifest = {"credentials": [
        {"id": "u@h", "type": "password", "host": "h", "username": "u", "secret": "pw"},
        {"id": "v@h", "type": "password", "host": "h", "username": "v", "secret": "pw2"},
    ]}
    assert mgr._restore_credentials(manifest) == 0     # nothing saved -> UI shows "0 of 2"

    # ...and when the backend works, all are restored.
    ok = FakeMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: ok)
    assert mgr._restore_credentials(manifest) == 2


def test_spbk_export_plaintext_when_no_passphrase(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))
    fake = FakeMgr()
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    path = str(tmp_path / "plain.spbk")
    ok, err = mgr.export_backup(path, connections=[], passphrase=None)
    assert ok, err
    from sshpilot.backup_archive import spbk_is_encrypted
    assert spbk_is_encrypted(path) is False
    assert read_spbk(path)["credentials"] == []


def test_spbk_export_honors_category_options(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host h.example\n", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"theme": "dark"}', encoding="utf-8")

    fake = FakeMgr()
    fake.data[password_spec("h.example", "alice").keyring_account] = "pw-a"
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)

    selected = FakeConn("A", "h.example", "alice")
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([selected], str(ssh_config)))
    path = str(tmp_path / "options.spbk")
    ok, err = mgr.export_backup(
        path,
        connections=[selected],
        passphrase="secret",
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": False,
        },
    )
    assert ok, err

    manifest = read_spbk(path, "secret")
    assert manifest["backup_options"]["app_settings"] is False
    assert manifest["app_config"] == {}
    assert manifest["ssh_config"] == ""
    assert manifest["known_hosts"] is None
    assert manifest["credentials"] == []
    assert manifest["private_keys"] == []


def test_spbk_private_key_roundtrip_is_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "config"))
    key_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    key_path.write_bytes(b"PRIVATE KEY\n")
    pub_path.write_bytes(b"PUBLIC KEY\n")
    key_path.chmod(0o600)

    selected = FakeConn("A", "h.example", "alice")
    selected.keyfile = str(key_path)
    selected.identity_files = [str(key_path)]
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([selected]))
    path = str(tmp_path / "keys.spbk")
    ok, err = mgr.export_backup(
        path,
        connections=[selected],
        passphrase="secret",
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert ok, err

    manifest = read_spbk(path, "secret")
    assert len(manifest["private_keys"]) == 1
    key_path.unlink()
    pub_path.unlink()

    success, error, restored, restored_keys = mgr.apply_imported_manifest(
        manifest,
        mode="replace",
        create_backup=False,
        restore_options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert success, error
    assert restored == 0
    assert restored_keys == 1
    assert key_path.read_bytes() == b"PRIVATE KEY\n"
    assert pub_path.read_bytes() == b"PUBLIC KEY\n"
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"


def test_spbk_restore_clamps_private_key_permissions(monkeypatch, tmp_path):
    """Backed-up private key modes must not restore group/other access."""
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "config"))
    key_path = tmp_path / "id_ed25519"
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    manifest = {
        "version": 1,
        "format": "spbk",
        "backup_options": {
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
        "app_config": {},
        "private_keys": [{
            "path": str(key_path),
            "mode": 0o644,
            "content_b64": "UFJJVkFURSBLRVkK",
        }],
    }

    success, error, restored, restored_keys = mgr.apply_imported_manifest(
        manifest,
        mode="replace",
        create_backup=False,
        restore_options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert success, error
    assert restored == 0
    assert restored_keys == 1
    assert key_path.read_bytes() == b"PRIVATE KEY\n"
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"


def test_spbk_private_key_merge_skips_existing(monkeypatch, tmp_path):
    """Merge mode must not overwrite private keys that already exist on disk."""
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "config"))
    key_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    key_path.write_bytes(b"EXISTING PRIVATE\n")
    pub_path.write_bytes(b"EXISTING PUBLIC\n")

    selected = FakeConn("A", "h.example", "alice")
    selected.keyfile = str(key_path)
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([selected]))
    path = str(tmp_path / "keys.spbk")
    ok, err = mgr.export_backup(
        path,
        connections=[selected],
        passphrase="secret",
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert ok, err
    manifest = read_spbk(path, "secret")

    success, error, restored, restored_keys = mgr.apply_imported_manifest(
        manifest,
        mode="merge",
        create_backup=False,
        restore_options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert success, error
    assert restored == 0
    assert restored_keys == 0
    assert key_path.read_bytes() == b"EXISTING PRIVATE\n"
    assert pub_path.read_bytes() == b"EXISTING PUBLIC\n"


def test_spbk_private_key_merge_skips_public_when_private_exists(monkeypatch, tmp_path):
    """Merge mode treats a private key and its .pub file as one skipped key-pair entry."""
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "config"))
    key_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    key_path.write_bytes(b"BACKUP PRIVATE\n")
    pub_path.write_bytes(b"BACKUP PUBLIC\n")

    selected = FakeConn("A", "h.example", "alice")
    selected.keyfile = str(key_path)
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([selected]))
    path = str(tmp_path / "keys.spbk")
    ok, err = mgr.export_backup(
        path,
        connections=[selected],
        passphrase="secret",
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert ok, err
    manifest = read_spbk(path, "secret")

    key_path.write_bytes(b"EXISTING PRIVATE\n")
    pub_path.unlink()

    success, error, restored, restored_keys = mgr.apply_imported_manifest(
        manifest,
        mode="merge",
        create_backup=False,
        restore_options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert success, error
    assert restored == 0
    assert restored_keys == 0
    assert key_path.read_bytes() == b"EXISTING PRIVATE\n"
    assert not pub_path.exists()


def test_spbk_private_key_merge_restores_missing(monkeypatch, tmp_path):
    """Merge mode still restores keys whose paths do not yet exist."""
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "config"))
    key_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    key_path.write_bytes(b"PRIVATE KEY\n")
    pub_path.write_bytes(b"PUBLIC KEY\n")

    selected = FakeConn("A", "h.example", "alice")
    selected.keyfile = str(key_path)
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([selected]))
    path = str(tmp_path / "keys.spbk")
    ok, err = mgr.export_backup(
        path,
        connections=[selected],
        passphrase="secret",
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert ok, err
    manifest = read_spbk(path, "secret")
    key_path.unlink()
    pub_path.unlink()

    success, error, restored, restored_keys = mgr.apply_imported_manifest(
        manifest,
        mode="merge",
        create_backup=False,
        restore_options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert success, error
    assert restored == 0
    assert restored_keys == 1
    assert key_path.read_bytes() == b"PRIVATE KEY\n"
    assert pub_path.read_bytes() == b"PUBLIC KEY\n"


def test_spbk_restore_honors_secret_option(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))
    fake = FakeMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)

    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    manifest = {
        "version": 1,
        "format": "spbk",
        "backup_options": {
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": True,
            "private_keys": False,
        },
        "app_config": {},
        "credentials": [
            {"id": "u@h", "type": "password", "host": "h", "username": "u", "secret": "pw"},
        ],
    }
    success, error, restored, restored_keys = mgr.apply_imported_manifest(
        manifest,
        mode="replace",
        create_backup=False,
        restore_options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": False,
        },
    )
    assert success, error
    assert restored == 0
    assert restored_keys == 0
    assert fake.data == {}
