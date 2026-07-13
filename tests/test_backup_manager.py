"""Tests for the .spbk credential gather/restore in BackupManager."""

import json
import os

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


class ModeConfig(FakeConfig):
    def __init__(self, config_file, isolated):
        self.config_file = str(config_file)
        self.config_data = {"ssh": {"use_isolated_config": isolated}}

    def get_setting(self, key, default=None):
        if key == "ssh.use_isolated_config":
            return self.config_data.get("ssh", {}).get("use_isolated_config", default)
        return default

    def load_json_config(self):
        with open(self.config_file, encoding="utf-8") as f:
            self.config_data = json.load(f)
        return self.config_data

    def get_default_config(self):
        return {"ssh": {"use_isolated_config": self.get_setting("ssh.use_isolated_config", False)}}


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


def test_restore_replace_keeps_current_default_mode(monkeypatch, tmp_path):
    """An isolated backup restored while in default mode stays in default mode AND never
    touches the user's global ~/.ssh/known_hosts (known_hosts is isolated-only)."""
    config_dir = tmp_path / "config"
    ssh_dir = tmp_path / "ssh"
    config_dir.mkdir()
    ssh_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(ssh_dir))

    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"ssh": {"use_isolated_config": False}}),
        encoding="utf-8",
    )
    config = ModeConfig(config_file, isolated=False)
    ssh_config_path = ssh_dir / "config"
    known_hosts_path = ssh_dir / "known_hosts"
    known_hosts_path.write_text("EXISTING GLOBAL\n", encoding="utf-8")   # the user's real file
    mgr = bm.BackupManager(
        config,
        FakeConnMgr([], str(ssh_config_path), str(known_hosts_path), isolated_mode=False),
    )
    manifest = {
        "version": 1,
        "isolated_mode": True,
        "backup_options": {
            "app_settings": True,
            "ssh_config": True,
            "known_hosts": True,
            "secrets": False,
            "private_keys": False,
        },
        "ssh_config": "Host isolated-backup\n",
        "known_hosts": "isolated.example ssh-ed25519 AAAA\n",
        "app_config": {"ssh": {"use_isolated_config": True}, "ui": {"theme": "dark"}},
    }

    success, error = mgr._apply_parsed(manifest, mode="replace", create_backup=False)

    assert success, error
    assert ssh_config_path.read_text(encoding="utf-8") == "Host isolated-backup\n"
    # Global known_hosts is left exactly as it was — never overwritten in default mode.
    assert known_hosts_path.read_text(encoding="utf-8") == "EXISTING GLOBAL\n"
    restored_config = json.loads(config_file.read_text(encoding="utf-8"))
    assert restored_config["ssh"]["use_isolated_config"] is False
    assert restored_config["ui"]["theme"] == "dark"


def test_default_mode_export_excludes_global_known_hosts(monkeypatch, tmp_path):
    """A default-mode backup must never include the user's global ~/.ssh/known_hosts, even
    when the 'Known hosts' category is enabled."""
    config_dir = tmp_path / "config"
    ssh_dir = tmp_path / "ssh"
    config_dir.mkdir()
    ssh_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(ssh_dir))
    (ssh_dir / "known_hosts").write_text("global.example ssh-ed25519 ZZZ\n", encoding="utf-8")
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({"ssh": {"use_isolated_config": False}}), encoding="utf-8")

    config = ModeConfig(config_file, isolated=False)
    mgr = bm.BackupManager(
        config,
        FakeConnMgr([], str(ssh_dir / "config"), str(ssh_dir / "known_hosts"),
                    isolated_mode=False),
    )
    path = str(tmp_path / "b.spbk")
    ok, err = mgr.export_backup(
        path, connections=[], passphrase=None,
        options={"app_settings": False, "ssh_config": False, "known_hosts": True,
                 "secrets": False, "private_keys": False})
    assert ok, err
    manifest = read_spbk(path)
    assert manifest["known_hosts"] is None     # the global file was NOT captured


def test_restore_replace_keeps_current_isolated_mode(monkeypatch, tmp_path):
    """A default backup restored while in isolated mode must stay in isolated mode."""
    config_dir = tmp_path / "config"
    default_ssh_dir = tmp_path / "ssh"
    config_dir.mkdir()
    default_ssh_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(default_ssh_dir))

    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"ssh": {"use_isolated_config": True}}),
        encoding="utf-8",
    )
    config = ModeConfig(config_file, isolated=True)
    isolated_config_path = config_dir / "ssh_config"
    isolated_known_hosts_path = config_dir / "known_hosts"
    mgr = bm.BackupManager(
        config,
        FakeConnMgr(
            [],
            str(isolated_config_path),
            str(isolated_known_hosts_path),
            isolated_mode=True,
        ),
    )
    manifest = {
        "version": 1,
        "isolated_mode": False,
        "backup_options": {
            "app_settings": True,
            "ssh_config": True,
            "known_hosts": True,
            "secrets": False,
            "private_keys": False,
        },
        "ssh_config": "Host default-backup\n",
        "known_hosts": "default.example ssh-ed25519 BBBB\n",
        "app_config": {"ssh": {"use_isolated_config": False}, "ui": {"theme": "light"}},
    }

    success, error = mgr._apply_parsed(manifest, mode="replace", create_backup=False)

    assert success, error
    assert isolated_config_path.read_text(encoding="utf-8") == "Host default-backup\n"
    assert (
        isolated_known_hosts_path.read_text(encoding="utf-8")
        == "default.example ssh-ed25519 BBBB\n"
    )
    assert not (default_ssh_dir / "config").exists()
    restored_config = json.loads(config_file.read_text(encoding="utf-8"))
    assert restored_config["ssh"]["use_isolated_config"] is True
    assert restored_config["ui"]["theme"] == "light"


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


def test_restore_preserves_local_secret_backend(monkeypatch, tmp_path):
    # The secrets.* subtree (backend choice + absolute vault paths) is machine-specific.
    # Restore must keep the LOCAL selection, not import the source machine's (which would point
    # the backend at a file that doesn't exist here and silently break autofill).
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))

    class LocalConfig(FakeConfig):
        def __init__(self):
            self.config_data = {"secrets": {"backend": "keepassxc",
                                            "keepassxc": {"database": "/home/me/local.kdbx"}}}

    mgr = bm.BackupManager(LocalConfig(), FakeConnMgr([]))
    imported = {"secrets": {"backend": "bitwarden", "bitwarden": {"profile": "/home/you/bw"}},
                "other": 1}
    out = mgr._app_config_for_restore(imported)
    assert out["secrets"]["backend"] == "keepassxc"                 # local kept
    assert out["secrets"]["keepassxc"]["database"] == "/home/me/local.kdbx"
    assert "bitwarden" not in out["secrets"]                         # source's not imported
    assert out["other"] == 1                                         # non-secret settings still flow


def test_restore_skips_existing_secrets(monkeypatch, tmp_path):
    # Non-destructive: a secret already present in the selected backend is left untouched,
    # counted as skipped, and never re-stored (can't clobber a rotated password).
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))

    class LookupMgr(FakeMgr):
        def lookup(self, spec):                   # selected-backend lookup used by the skip check
            return self.data.get(spec.keyring_account)

    stored = LookupMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: stored)
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    manifest = {"credentials": [
        {"id": "u@h", "type": "password", "host": "h", "username": "u", "secret": "new-u"},
        {"id": "v@h", "type": "password", "host": "h", "username": "v", "secret": "new-v"},
    ]}
    # Pre-seed one of them with a *different* (rotated) value that must survive.
    from sshpilot.credential_model import Credential, credential_to_spec
    existing_spec = credential_to_spec(Credential(
        id="u@h", type="password", host="h", username="u", secret="ROTATED", metadata={}))
    stored.data[existing_spec.keyring_account] = "ROTATED"

    restored = mgr._restore_credentials(manifest)
    assert restored == 1                          # only the absent 'v' was stored
    assert mgr.last_import_skipped_credentials == 1
    assert stored.data[existing_spec.keyring_account] == "ROTATED"   # not clobbered


def test_restore_reports_zero_for_non_persisting_backend(monkeypatch, tmp_path):
    # The "agent" backend's store() returns True but writes nothing. Restore must NOT count
    # those as successes; it reports 0 and flags that secrets were not persisted.
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))

    class AgentMgr(FakeMgr):
        def persists_secrets(self):
            return False
        def store(self, spec, secret):           # mimics SSHAgentBackend: lies about success
            return True

    stored = AgentMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: stored)
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    manifest = {"credentials": [
        {"id": "u@h", "type": "password", "host": "h", "username": "u", "secret": "pw"},
    ]}
    assert mgr._restore_credentials(manifest) == 0
    assert mgr.last_import_secrets_persisted is False
    assert stored.data == {}                       # store() was never actually called


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


def test_spbk_replace_mode_never_overwrites_private_key(monkeypatch, tmp_path):
    """Data-loss red line: even in REPLACE mode an existing private key is left untouched;
    only keys whose path doesn't exist are written, and the rest are reported as skipped."""
    import base64
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "config"))
    key_path = tmp_path / "id_ed25519"
    pub_path = tmp_path / "id_ed25519.pub"
    key_path.write_bytes(b"EXISTING PRIVATE\n")
    pub_path.write_bytes(b"EXISTING PUBLIC\n")
    new_path = tmp_path / "id_new"

    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    manifest = {
        "version": 1, "format": "spbk",
        "backup_options": {"app_settings": False, "ssh_config": False, "known_hosts": False,
                           "secrets": False, "private_keys": True},
        "app_config": {},
        "private_keys": [
            {"path": str(key_path), "mode": 0o600,
             "content_b64": base64.b64encode(b"BACKUP DIFFERENT\n").decode(),
             "public_path": str(pub_path), "public_mode": 0o644,
             "public_content_b64": base64.b64encode(b"BACKUP PUB\n").decode()},
            {"path": str(new_path), "mode": 0o600,
             "content_b64": base64.b64encode(b"NEW KEY\n").decode()},
        ],
    }
    success, error, restored, restored_keys = mgr.apply_imported_manifest(
        manifest, mode="replace", create_backup=False,
        restore_options={"app_settings": False, "ssh_config": False, "known_hosts": False,
                         "secrets": False, "private_keys": True})
    assert success, error
    assert key_path.read_bytes() == b"EXISTING PRIVATE\n"     # NOT overwritten, despite replace
    assert pub_path.read_bytes() == b"EXISTING PUBLIC\n"      # nor its .pub
    assert new_path.read_bytes() == b"NEW KEY\n"              # only the non-existing one written
    assert restored_keys == 1
    assert mgr.last_import_skipped_keys == 1                  # existing key protected + counted


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


# --- cross-device re-homing (P1) --------------------------------------------

def test_rebase_home_path_helper():
    assert bm._rebase_home_path(
        "/home/alice/.ssh/id", "/home/alice", "/home/bob") == "/home/bob/.ssh/id"
    assert bm._rebase_home_path(                       # outside source home -> unchanged
        "/etc/ssh/id", "/home/alice", "/home/bob") == "/etc/ssh/id"
    assert bm._rebase_home_path(                       # no recorded source home -> unchanged
        "/home/alice/.ssh/id", None, "/home/bob") == "/home/alice/.ssh/id"


def test_rebase_home_in_text_helper():
    txt = "Host x\n    IdentityFile /home/alice/.ssh/id\n"
    out = bm._rebase_home_in_text(txt, "/home/alice", "/home/bob")
    assert "/home/bob/.ssh/id" in out and "/home/alice" not in out
    assert bm._rebase_home_in_text(txt, None, "/home/bob") == txt   # no source home -> no-op


def test_private_key_restore_rehomes_to_current_home(monkeypatch, tmp_path):
    import base64
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "config"))
    newhome = tmp_path / "home_bob"
    newhome.mkdir()
    monkeypatch.setenv("HOME", str(newhome))
    manifest = {
        "source_home": "/home/alice",
        "private_keys": [{
            "path": "/home/alice/.ssh/id_ed25519",
            "mode": 0o600,
            "content_b64": base64.b64encode(b"PRIVATE\n").decode(),
        }],
    }
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    written, skipped = mgr._restore_private_keys(manifest)
    assert (written, skipped) == (1, 0)
    dest = newhome / ".ssh" / "id_ed25519"
    assert dest.read_bytes() == b"PRIVATE\n"            # landed under THIS machine's home


def test_passphrase_credential_rehomed_to_current_home(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))
    newhome = tmp_path / "home_bob"
    newhome.mkdir()
    monkeypatch.setenv("HOME", str(newhome))

    class LookupMgr(FakeMgr):
        def lookup(self, spec):
            return self.data.get(spec.keyring_account)

    stored = LookupMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: stored)
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    manifest = {"source_home": "/home/alice", "credentials": [{
        "id": "/home/alice/.ssh/id_ed25519", "type": "key",
        "host": None, "username": None, "secret": "pp",
        "metadata": {"key_path": "/home/alice/.ssh/id_ed25519"}}]}
    assert mgr._restore_credentials(manifest) == 1
    keys = list(stored.data.keys())
    assert any(str(newhome) in k for k in keys)         # passphrase filed under re-homed path
    assert all("/home/alice" not in k for k in keys)    # never under the source home


# --- Include-aware config tree backup + fragment merge -----------------------

def _mk_config_tree(tmp_path):
    """A default-mode config root with a main file that Includes a fragment dir."""
    root = tmp_path / "dotssh"
    (root / "conf.d").mkdir(parents=True)
    main = root / "config"
    main.write_text("Include conf.d/*.conf\nHost existing-main\n    HostName m.example\n",
                    encoding="utf-8")
    (root / "conf.d" / "base.conf").write_text(
        "Host in-fragment\n    HostName f.example\n", encoding="utf-8")
    return root, main


def test_export_bundles_include_tree_and_skips_out_of_root(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "cfg"))
    root, main = _mk_config_tree(tmp_path)
    # An included file OUTSIDE the config root must be skipped (not bundled).
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "extra.conf").write_text("Host outside-host\n", encoding="utf-8")
    main.write_text(main.read_text() + f"Include {outside}/extra.conf\n", encoding="utf-8")

    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([], ssh_config_path=str(main)))
    data = mgr._build_export_data({"app_settings": False, "ssh_config": True,
                                   "known_hosts": False, "secrets": False,
                                   "private_keys": False})
    tree = data["ssh_config_files"]
    frag_rel = os.path.join("conf.d", "base.conf")
    assert set(tree) >= {"config", frag_rel}
    assert data["ssh_config_main_rel"] == "config"
    assert "Host in-fragment" in tree[frag_rel]
    assert any("extra.conf" in p for p in mgr.last_export_skipped_config_files)
    assert not any("outside-host" in c for c in tree.values())   # never bundled its content


def test_replace_restores_tree_rebased_and_maps_main(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "cfg"))
    newhome = tmp_path / "home_bob"
    newhome.mkdir()
    monkeypatch.setenv("HOME", str(newhome))
    target_root = tmp_path / "dotssh"
    target_root.mkdir()
    main = target_root / "config"

    import_data = {
        "source_home": "/home/alice",
        "ssh_config_main_rel": "config",
        "ssh_config_files": {
            "config": "Host a\n    IdentityFile /home/alice/.ssh/id\n",
            "conf.d/work.conf": "Host work\n    HostName work.example\n",
        },
    }
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([], ssh_config_path=str(main)))
    ok, err = mgr._import_replace(import_data, {"app_settings": False, "ssh_config": True,
                                               "known_hosts": False, "secrets": False,
                                               "private_keys": False})
    assert ok, err
    main_text = main.read_text()
    assert f"IdentityFile {newhome}/.ssh/id" in main_text     # home rebased
    assert "/home/alice" not in main_text
    assert (target_root / "conf.d" / "work.conf").read_text().count("Host work") == 1


def test_merge_fragment_dedups_drops_globals_keeps_unindented(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "cfg"))
    root, main = _mk_config_tree(tmp_path)
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([], ssh_config_path=str(main)))

    imported = (
        "Host in-fragment\n    HostName dup.example\n"        # exists (in a fragment) -> skip
        "Host newhost\nHostName n.example\nPort 2222\nUser alice\n"  # UNINDENTED -> keep whole
        "Host *\n    ProxyJump evil.example\n"               # wildcard/global -> drop
        "Match host foo\n    User m\n"                        # Match -> drop
        "Host existing-main brand-new\n    HostName x\n"      # partial collision -> split+report
    )
    mgr._merge_ssh_config_fragment(str(main), [imported])

    fragment = root / bm._IMPORT_FRAGMENT_NAME
    frag = fragment.read_text()
    assert "Host newhost" in frag and "Port 2222" in frag and "User alice" in frag  # not truncated
    assert "Host *" not in frag and "ProxyJump" not in frag         # global NOT injected
    assert "in-fragment" not in frag                                 # dedup saw the fragment
    # Partial collision: the new name is imported (split), the existing one is not re-added.
    assert "Host brand-new" in frag
    assert "existing-main" not in frag
    assert mgr.last_merge_dropped_globals >= 2                       # Host* + Match
    assert any("brand-new" in " ".join(p) for p in mgr.last_merge_collisions)
    # main gained exactly one Include for the fragment — at the TOP (before any Host/Match).
    main_text = main.read_text()
    assert main_text.count(bm._IMPORT_FRAGMENT_NAME) >= 1
    assert main_text.lstrip().startswith(bm._IMPORT_INCLUDE_MARKER)
    include_lines = [l for l in main_text.splitlines()
                     if l.strip().lower().startswith("include") and bm._IMPORT_FRAGMENT_NAME in l]
    assert len(include_lines) == 1
    first_host = main_text.lower().find("\nhost ")
    first_include = main_text.find(include_lines[0])
    assert first_include != -1 and (first_host == -1 or first_include < first_host)

    # Idempotent: re-importing the same hosts adds nothing (fragment is Include-resolved now).
    before = fragment.read_text()
    mgr._merge_ssh_config_fragment(str(main), [imported])
    assert fragment.read_text() == before
    assert len([l for l in main.read_text().splitlines()
                if l.strip().lower().startswith("include") and bm._IMPORT_FRAGMENT_NAME in l]) == 1


def test_ensure_include_prepends_before_host_star(monkeypatch, tmp_path):
    """Include must be top-level; appended under Host * lets User root win (Oracle bug)."""
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "cfg"))
    root = tmp_path / "dotssh"
    root.mkdir()
    main = root / "config"
    main.write_text(
        "Host USA\n    HostName 1.2.3.4\n    User root\n\n"
        "Host *\n    User root\n    ServerAliveInterval 60\n",
        encoding="utf-8",
    )
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([], ssh_config_path=str(main)))
    mgr._merge_ssh_config_fragment(
        str(main),
        ["Host Oracle\n    HostName 150.230.27.23\n    User ubuntu\n    Port 2222\n"
         "    ProxyJump USA\n"],
    )
    text = main.read_text()
    assert text.startswith(bm._IMPORT_INCLUDE_MARKER + "\n")
    include_line = text.splitlines()[1]
    assert include_line.lower().startswith("include") and bm._IMPORT_FRAGMENT_NAME in include_line
    # Must appear before Host *
    assert text.find(include_line) < text.lower().find("host *")
    # Outside ~/.ssh, Include must be absolute so OpenSSH -F resolves it.
    assert include_line.split(None, 1)[1].startswith(str(root))

    # OpenSSH effective config: User ubuntu, not root from Host *
    import shutil
    import subprocess
    if shutil.which("ssh"):
        out = subprocess.check_output(
            ["ssh", "-F", str(main), "-G", "Oracle"], text=True, stderr=subprocess.DEVNULL)
        opts = {line.split(" ", 1)[0]: line.split(" ", 1)[1]
                for line in out.splitlines() if " " in line}
        assert opts.get("user") == "ubuntu"
        assert opts.get("hostname") == "150.230.27.23"
        assert opts.get("port") == "2222"


def test_repair_relocates_appended_import_include(tmp_path):
    """Existing installs that appended Include under Host * are healed on repair."""
    root = tmp_path / "dotssh"
    root.mkdir()
    main = root / "config"
    frag = root / bm._IMPORT_FRAGMENT_NAME
    frag.write_text(
        "Host Oracle\n    HostName 150.230.27.23\n    User ubuntu\n    Port 2222\n",
        encoding="utf-8",
    )
    # Simulate the old bug: relative Include appended after Host *
    main.write_text(
        "Host *\n    User root\n\n"
        f"{bm._IMPORT_INCLUDE_MARKER}\nInclude {bm._IMPORT_FRAGMENT_NAME}\n",
        encoding="utf-8",
    )
    assert bm._import_include_follows_host_or_match(
        main.read_text(), str(frag), str(root))
    assert bm.repair_misplaced_import_include(str(main)) is True
    text = main.read_text()
    assert text.startswith(bm._IMPORT_INCLUDE_MARKER)
    include_lines = [l for l in text.splitlines()
                     if l.strip().lower().startswith("include") and bm._IMPORT_FRAGMENT_NAME in l]
    assert len(include_lines) == 1
    assert not bm._import_include_follows_host_or_match(text, str(frag), str(root))
    # Second repair is a no-op
    assert bm.repair_misplaced_import_include(str(main)) is False

    import shutil
    import subprocess
    if shutil.which("ssh"):
        out = subprocess.check_output(
            ["ssh", "-F", str(main), "-G", "Oracle"], text=True, stderr=subprocess.DEVNULL)
        user_lines = [l for l in out.splitlines() if l.startswith("user ")]
        assert user_lines == ["user ubuntu"]


def test_merge_no_double_include_when_glob_already_covers(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "cfg"))
    root = tmp_path / "dotssh"
    root.mkdir()
    main = root / "config"
    # A glob that will already cover the sshPilot fragment once it's written.
    main.write_text("Include *.conf\nHost keep\n    HostName k.example\n", encoding="utf-8")
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([], ssh_config_path=str(main)))
    mgr._merge_ssh_config_fragment(str(main), ["Host newone\n    HostName n.example\n"])
    assert (root / bm._IMPORT_FRAGMENT_NAME).exists()
    # No explicit Include added because `Include *.conf` already resolves the fragment.
    assert "sshpilot-imported.conf" not in main.read_text()


# --- SSH config stanza splitter (header-boundary, not indentation) -----------

def test_iter_config_stanzas_boundaries_and_kinds():
    text = (
        "Compression yes\n"                       # leading global
        "Host a b\n    HostName a.ex\n"            # host, multi-pattern
        "Host c\nHostName c.ex\nPort 2222\n"       # host, UNINDENTED options
        "Match host d\n    User x\n"               # match
    )
    stanzas = list(bm._iter_config_stanzas(text))
    kinds = [k for k, _p, _b in stanzas]
    assert kinds == ["global", "host", "host", "match"]
    # multi-pattern captured fully
    assert stanzas[1][1] == ["a", "b"]
    # unindented host keeps ALL its option lines (the old parser dropped these)
    c_block = "\n".join(stanzas[2][2])
    assert "Port 2222" in c_block and "HostName c.ex" in c_block


def test_ssh_keyword_variants():
    assert bm._ssh_keyword("Host x") == "host"
    assert bm._ssh_keyword("Host=x") == "host"
    assert bm._ssh_keyword("  Host = x") == "host"
    assert bm._ssh_keyword("\tHostName y") == "hostname"
    assert bm._ssh_keyword("# comment") == ""
    assert bm._ssh_keyword("") == ""


def test_wildcard_pattern_detection():
    assert bm._is_wildcard_pattern("*")
    assert bm._is_wildcard_pattern("prod-*")
    assert bm._is_wildcard_pattern("!deny")
    assert not bm._is_wildcard_pattern("concrete-host")


# --- backend delegation (export_to_backend / import_from_backend) ------------

class _RecordingBackend:
    name = "fake"
    def __init__(self, manifest_to_return=None):
        self.exported = None
        self._manifest = manifest_to_return
    def export(self, manifest, *, passphrase=None):
        from sshpilot.backup_backends import BackupEntry
        self.exported = (manifest, passphrase)
        return BackupEntry(id="itemid", name="itemname")
    def read(self, entry, *, passphrase=None):
        return self._manifest


def test_export_to_backend_builds_manifest_and_delegates(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    backend = _RecordingBackend()
    entry = mgr.export_to_backend(
        backend, connections=[], passphrase="pw",
        options={"app_settings": False, "ssh_config": False, "known_hosts": False,
                 "secrets": False, "private_keys": False})
    assert entry.name == "itemname"
    manifest, passphrase = backend.exported
    assert passphrase == "pw"
    assert manifest["format"] == "spbk"          # real manifest was built
    assert manifest["credentials"] == [] and manifest["private_keys"] == []


def test_import_from_backend_reads_and_applies(monkeypatch, tmp_path):
    from sshpilot.backup_backends import BackupEntry
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    backend = _RecordingBackend(manifest_to_return={"version": 1, "app_config": {}})
    seen = []
    monkeypatch.setattr(mgr, "apply_imported_manifest",
                        lambda manifest, **kw: (seen.append((manifest, kw)) or (True, None, 3, 1)))
    res = mgr.import_from_backend(backend, BackupEntry(id="x", name="x"),
                                 mode="merge", restore_options={"secrets": True})
    assert res == (True, None, 3, 1)
    assert seen[0][0] == {"version": 1, "app_config": {}}
    assert seen[0][1]["mode"] == "merge"
    assert seen[0][1]["restore_options"] == {"secrets": True}


# --- mirror secrets as Bitwarden login items --------------------------------

class _RecSecretBackend:
    def __init__(self, ok=True):
        self.stored = []
        self._ok = ok
    def store(self, spec, secret):
        self.stored.append((spec.keyring_account, secret))
        return self._ok


def test_mirror_credentials_to_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([]))
    backend = _RecSecretBackend()
    manifest = {"credentials": [
        {"id": "u@h", "type": "password", "host": "h", "username": "u", "secret": "pw"},
        {"id": "sudo:u@h", "type": "sudo", "host": "h", "username": "u", "secret": "spw"},
        {"id": "no-secret", "type": "password", "host": "h", "username": "x", "secret": None},
    ]}
    mirrored, failed = mgr.mirror_credentials_to_backend(manifest, backend)
    assert (mirrored, failed) == (2, 0)                       # the None-secret one is skipped
    assert ("u@h", "pw") in backend.stored
    assert ("sudo:u@h", "spw") in backend.stored


def test_export_to_backend_mirrors_only_when_secrets_on(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path))
    fake = FakeMgr()
    fake.data[password_spec("h", "u").keyring_account] = "pw"
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)
    conn = FakeConn("A", "h", "u")
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([conn]))
    note_backend = _RecordingBackend()
    mirror = _RecSecretBackend()

    base = {"app_settings": False, "ssh_config": False, "known_hosts": False, "private_keys": False}
    mgr.export_to_backend(note_backend, connections=[conn],
                          options={**base, "secrets": True}, mirror_to=mirror)
    assert mgr.last_mirror_counts == {"mirrored": 1, "failed": 0}
    assert "u@h" in [acct for acct, _s in mirror.stored]

    # secrets off -> no mirroring even if mirror_to is passed.
    mirror2 = _RecSecretBackend()
    mgr.export_to_backend(note_backend, connections=[conn],
                          options={**base, "secrets": False}, mirror_to=mirror2)
    assert mgr.last_mirror_counts is None
    assert mirror2.stored == []
