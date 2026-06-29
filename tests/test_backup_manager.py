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

    def _all_available_backends(self):
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
    ssh_config_path = ""
    isolated_mode = False

    def __init__(self, conns):
        self._conns = conns

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
