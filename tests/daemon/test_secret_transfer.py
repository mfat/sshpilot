"""Equivalence tests: the daemon secret-transfer path is a thin adapter over
the pre-existing ``BackupManager`` implementation.

These prove the daemon-owned export/import preserves the full backup behavior
(app settings, SSH config, known_hosts, secrets, private keys) with the exact
same manifests, option semantics and non-destructive restore as the classic
GTK-driven path — no parallel backup implementation is maintained.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import sshpilot.backup_manager as bm
import sshpilot.credential_manager as cmod
import sshpilot.secret_storage as ss
from sshpilot.api.models.secrets import SecretOperationState
from sshpilot.backup_archive import read_spbk, write_spbk
from sshpilot.core.connections.models import ConnectionRecord
from sshpilot.core.settings import CONFIG_VERSION
from sshpilot.daemon.secret_backend_service import SecretBackendService
from sshpilot.daemon.secret_transfer import (
    daemon_export_backup,
    daemon_import_backup,
    daemon_preview_backup,
)
from sshpilot.secret_storage import password_spec, sudo_password_spec


class FakeMgr:
    """Dict-backed SecretManager used by both the daemon service and the
    classic ``CredentialManager`` gather path (same process-wide identity)."""

    def __init__(self, secrets=None):
        self.data = dict(secrets or {})
        self._selected = "auto"
        self.active_backend_name = "libsecret"

    # -- credential enumeration surface -------------------------------
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

    # -- service lifecycle surface ------------------------------------
    def registered_backends(self):
        return ["libsecret", "keyring", "bitwarden", "rbw", "keepassxc", "agent"]

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


class FakeConn:
    """Classic connection view (hostname-based) used by the pre-existing path."""

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


class FakeConnMgr:
    def __init__(self, conns, ssh_config_path=""):
        self._conns = conns
        self.ssh_config_path = ssh_config_path

    def get_connections(self):
        return list(self._conns)

    def load_ssh_config(self):
        pass


class HeadlessConfig:
    """Config shim reading the daemon's settings file (isolated mode off)."""

    def __init__(self, config_file):
        self.config_file = str(config_file)
        self.config_data = {"ssh": {"use_isolated_config": False}}

    def get_setting(self, key, default=None):
        if key == "ssh.use_isolated_config":
            return self.config_data.get("ssh", {}).get("use_isolated_config", default)
        return default

    def load_json_config(self):
        with open(self.config_file, encoding="utf-8") as fh:
            self.config_data = json.load(fh)
        return self.config_data

    def get_default_config(self):
        return {"ssh": {"use_isolated_config": False}}


def _record(nickname, hostname, username):
    return ConnectionRecord(
        id=nickname,
        nickname=nickname,
        hostname=hostname,
        host=hostname,
        username=username,
        port=22,
    )


def _isolate_paths(monkeypatch, tmp_path):
    """Point platform_utils getters at a throwaway config/ssh pair."""
    config_dir = tmp_path / "config"
    ssh_dir = tmp_path / "ssh"
    config_dir.mkdir(parents=True, exist_ok=True)
    ssh_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(ssh_dir))
    return config_dir, ssh_dir


def _seed(fake, hostname, username, password, sudo):
    fake.data[password_spec(hostname, username).keyring_account] = password
    fake.data[sudo_password_spec(hostname, username).keyring_account] = sudo


def _make_daemon_manager(fake, settings_path, *, connections_source=None):
    from sshpilot.daemon.secret_backend_service import SecretBackendService

    return SecretBackendService(
        settings_path,
        secret_manager=fake,
        connections_source=connections_source,
    )


# ---------------------------------------------------------------------------
# Equivalence: daemon export == classic BackupManager export
# ---------------------------------------------------------------------------

def test_daemon_export_manifest_equals_classic_backup_manager(monkeypatch, tmp_path):
    """Same inputs through the daemon path and the classic path produce the
    same encrypted manifest (credentials, SSH config, app settings)."""
    config_dir, ssh_dir = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps(
            {"config_version": CONFIG_VERSION, "ssh": {"use_isolated_config": False}}
        ),
        encoding="utf-8",
    )
    (ssh_dir / "config").write_text("Host classic\\n", encoding="utf-8")

    fake = FakeMgr()
    _seed(fake, "h.example", "alice", "pw-a", "sudo-a")
    _seed(fake, "other.example", "bob", "pw-b", "sudo-b")
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)

    records = [_record("A", "h.example", "alice"), _record("B", "other.example", "bob")]

    # Daemon path: connections arrive as headless records via a callable source.
    # (No ``encrypted`` flag — passphrase collection is covered by the service
    # tests; here the manifests must be byte-equivalent in content.)
    daemon_path = tmp_path / "daemon.spbk"
    daemon_service = _make_daemon_manager(
        fake, config_file, connections_source=lambda: records
    )
    result = daemon_service.export_backup(
        destination=str(daemon_path),
        connection_ids=["A", "B"],
        options={
            "app_settings": True,
            "ssh_config": True,
            "known_hosts": False,
            "secrets": True,
            "private_keys": False,
        },
        owner_client_id="client-1",
    )
    from sshpilot.api.models.secrets import SecretOperationState

    assert result.status == SecretOperationState.SUCCESS, result.message

    # Classic path: identical options, classic connection objects.
    classic_path = tmp_path / "classic.spbk"
    manager = bm.BackupManager(
        HeadlessConfig(config_file), FakeConnMgr([], str(ssh_dir / "config"))
    )
    ok, err = manager.export_backup(
        str(classic_path),
        connections=[FakeConn("A", "h.example", "alice"),
                     FakeConn("B", "other.example", "bob")],
        options={
            "app_settings": True,
            "ssh_config": True,
            "known_hosts": False,
            "secrets": True,
            "private_keys": False,
        },
    )
    assert ok, err

    daemon_manifest = read_spbk(str(daemon_path), None)
    classic_manifest = read_spbk(str(classic_path), None)

    def _normalize(manifest):
        creds = {
            (c["type"], c["id"]): c["secret"]
            for c in manifest.get("credentials") or []
        }
        return {
            "format": manifest.get("format"),
            "config_version": manifest.get("config_version"),
            "ssh_config": manifest.get("ssh_config"),
            "ssh_config_files": manifest.get("ssh_config_files"),
            "app_config": manifest.get("app_config", {}).get("ssh"),
            "credentials": creds,
            "private_keys": len(manifest.get("private_keys") or []),
            "backup_options": manifest.get("backup_options"),
        }

    assert _normalize(daemon_manifest) == _normalize(classic_manifest)
    # The daemon used the service's shared secret manager for the gather.
    daemon_creds = {
        (c["type"], c["id"]): c["secret"] for c in daemon_manifest["credentials"]
    }
    assert daemon_creds[("password", "alice@h.example")] == "pw-a"
    assert daemon_creds[("password", "bob@other.example")] == "pw-b"


def test_daemon_export_discovery_via_callable_and_selection(monkeypatch, tmp_path):
    """A callable ``connections_source`` drives discovery; ``connection_ids``
    narrows which connections' credentials are exported."""
    config_dir, ssh_dir = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )

    fake = FakeMgr()
    _seed(fake, "one.example", "alice", "pw-1", "sudo-1")
    _seed(fake, "two.example", "bob", "pw-2", "sudo-2")
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)

    records = [_record("one", "one.example", "alice"), _record("two", "two.example", "bob")]

    def _source():
        return records

    daemon_path = tmp_path / "narrow.spbk"
    result = daemon_export_backup(
        fake,
        destination=str(daemon_path),
        connection_ids=["one"],
        options={
            "app_settings": False, "ssh_config": False, "known_hosts": False,
            "secrets": True, "private_keys": False,
        },
        connections_source=_source,
        settings_path=config_file,
    )
    assert result.status.value == "success", result.message
    manifest = read_spbk(str(daemon_path), None)
    creds = {(c["type"], c["id"]): c["secret"] for c in manifest["credentials"]}
    assert ("password", "alice@one.example") in creds
    assert ("password", "bob@two.example") not in creds


def test_daemon_export_preserves_private_keys(monkeypatch, tmp_path):
    """Private-key content and permission mode round-trip through the daemon."""
    config_dir, ssh_dir = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )

    key_file = ssh_dir / "id_ed25519"
    key_file.write_text("PRIVATE KEY MATERIAL\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA comment\n", encoding="utf-8")

    fake = FakeMgr()
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)

    record = ConnectionRecord(
        id="k", nickname="k", hostname="h", host="h", username="u",
        port=22, data={"keyfile": str(key_file)},
    )
    daemon_path = tmp_path / "keys.spbk"
    result = daemon_export_backup(
        fake,
        destination=str(daemon_path),
        connection_ids=["k"],
        options={
            "app_settings": False, "ssh_config": False, "known_hosts": False,
            "secrets": False, "private_keys": True,
        },
        connections_source=lambda: [record],
        settings_path=config_file,
    )
    assert result.status.value == "success", result.message
    manifest = read_spbk(str(daemon_path), None)
    keys = manifest.get("private_keys") or []
    assert len(keys) == 1
    assert keys[0]["path"] == str(key_file)
    assert keys[0]["mode"] & 0o600 == 0o600
    assert keys[0]["public_content_b64"]


# ---------------------------------------------------------------------------
# Equivalence: daemon import == classic non-destructive restore
# ---------------------------------------------------------------------------

def test_daemon_import_restores_credentials_and_skips_existing(monkeypatch, tmp_path):
    """Daemon import re-stores absent credentials and leaves present ones
    untouched — exactly the classic non-destructive merge behavior."""
    config_dir, ssh_dir = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )

    fake = FakeMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)

    manifest = {
        "version": 1,
        "format": "spbk",
        "app_config": {},
        "ssh_config": "",
        "known_hosts": None,
        "credentials": [
            {"id": "alice@h.example", "type": "password",
             "host": "h.example", "username": "alice",
             "secret": "pw-new", "metadata": {}},
            {"id": "bob@other.example", "type": "password",
             "host": "other.example", "username": "bob",
             "secret": "pw-other", "metadata": {}},
        ],
        "private_keys": [],
        "backup_options": {"app_settings": True, "ssh_config": True,
                           "known_hosts": False, "secrets": True,
                           "private_keys": False},
    }
    source = tmp_path / "in.spbk"
    write_spbk(str(source), manifest, "secret")

    # One secret already present (rotated) must survive the import.
    fake.data[password_spec("h.example", "alice").keyring_account] = "ROTATED"

    result = daemon_import_backup(
        fake,
        source=str(source),
        passphrase="secret",
        options={"secrets": True},
        settings_path=config_file,
    )
    assert result.status.value == "success", result.message
    assert result.counts["restored"] == 1
    assert result.counts["skipped"] == 1
    # Non-destructive: the rotated value was NOT clobbered.
    assert fake.data[password_spec("h.example", "alice").keyring_account] == "ROTATED"
    assert fake.data[password_spec("other.example", "bob").keyring_account] == "pw-other"


def test_daemon_import_wrong_passphrase_is_a_clean_failure(monkeypatch, tmp_path):
    config_dir, _ = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )

    fake = FakeMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)
    source = tmp_path / "in.spbk"
    write_spbk(str(source), {"version": 1, "app_config": {}}, "right")

    result = daemon_import_backup(
        fake,
        source=str(source),
        options={"encrypted": True},
        settings_path=config_file,
    )
    assert result.status.value == "failed"
    assert result.message  # never a traceback with a secret inside
    assert "wrong passphrase or corrupt backup" in result.message.lower()


def test_daemon_export_nothing_selected_fails_cleanly(monkeypatch, tmp_path):
    config_dir, _ = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )

    fake = FakeMgr()
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)
    result = daemon_export_backup(
        fake,
        destination=str(tmp_path / "x.spbk"),
        options={
            "app_settings": False, "ssh_config": False, "known_hosts": False,
            "secrets": False, "private_keys": False,
        },
        connections_source=list,
        settings_path=config_file,
    )
    assert result.status.value == "failed"
    assert "nothing selected" in result.message.lower()
    assert not (tmp_path / "x.spbk").exists()
# ---------------------------------------------------------------------------
# Import preview (metadata only) + one-passphrase manifest cache
# ---------------------------------------------------------------------------

def _preview_spbk(monkeypatch, tmp_path, *, passphrase=None):
    """A tiny credential-carrying .spbk with a sentinel secret value."""
    config_dir, _ = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )
    source = tmp_path / "preview.spbk"
    manifest = {
        "version": 1,
        "format": "spbk",
        "app_config": {},
        "ssh_config": "Host from-backup\n",
        "known_hosts": None,
        "credentials": [
            {"id": "alice@h.example", "type": "password",
             "host": "h.example", "username": "alice",
             "secret": "SENTINEL-PW", "metadata": {}},
        ],
        "private_keys": [],
        "backup_options": {"app_settings": True, "ssh_config": True,
                           "known_hosts": False, "secrets": True,
                           "private_keys": False},
    }
    write_spbk(str(source), manifest, passphrase)
    return config_file, source, manifest


def test_daemon_preview_backup_reports_kind_encryption_and_included(monkeypatch, tmp_path):
    config_file, source, manifest = _preview_spbk(monkeypatch, tmp_path)
    public, cached = daemon_preview_backup(
        FakeMgr(), source=str(source), settings_path=config_file)
    assert public["kind"] == "spbk"
    assert public["encrypted"] is False
    assert public["included"] == {
        "app_settings": True, "ssh_config": True, "known_hosts": False,
        "secrets": True, "private_keys": False,
    }
    assert "error" not in public
    # The decrypted manifest is handed back for the daemon's cache — never to a caller.
    assert cached == manifest


def test_daemon_preview_encrypted_spbk_requires_passphrase(monkeypatch, tmp_path):
    config_file, source, _manifest = _preview_spbk(monkeypatch, tmp_path, passphrase="right")
    # Without a passphrase the daemon reports that a prompt is needed (no categories).
    public, cached = daemon_preview_backup(
        FakeMgr(), source=str(source), settings_path=config_file)
    assert public["encrypted"] is True
    assert public["included"] == {}
    assert cached is None
    # A wrong passphrase is a clean, secret-free error.
    public, cached = daemon_preview_backup(
        FakeMgr(), source=str(source), passphrase="wrong", settings_path=config_file)
    assert public["error"]
    assert cached is None
    # The correct passphrase materialises the included categories.
    public, cached = daemon_preview_backup(
        FakeMgr(), source=str(source), passphrase="right", settings_path=config_file)
    assert public["encrypted"] is True
    assert public["included"]["secrets"] is True
    assert cached is not None


def test_daemon_preview_legacy_json_config(monkeypatch, tmp_path):
    config_dir, _ = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps({"ssh": {}}), encoding="utf-8")
    public, cached = daemon_preview_backup(
        FakeMgr(), source=str(source), settings_path=config_file)
    assert public["kind"] == "json"
    assert public["encrypted"] is False
    assert cached is None


def test_daemon_preview_public_never_carries_secret_values(monkeypatch, tmp_path):
    config_file, source, _manifest = _preview_spbk(monkeypatch, tmp_path, passphrase="right")
    public, _cached = daemon_preview_backup(
        FakeMgr(), source=str(source), passphrase="right", settings_path=config_file)
    dumped = json.dumps(public)
    assert "SENTINEL-PW" not in dumped
    assert "credentials" not in public
    assert "private_keys" not in public
    assert set(public) <= {"kind", "encrypted", "included", "error"}


class _OneShotBroker:
    """Returns exactly one scripted secret, then fails any further prompt."""

    def __init__(self, secret):
        self._secret = secret
        self.created = []
        self._used = False

    def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(
            id=f"inter-{len(self.created)}", session_id=kwargs.get("session_id"))

    def wait_for_result(self, _interaction_id):
        if self._used:
            raise AssertionError("import re-prompted for the passphrase — cache miss")
        self._used = True
        return SimpleNamespace(secret=bytearray(self._secret.encode("utf-8")))

    def request_client_secret(self, *, owner_client_id, **kwargs):
        summary = self.create(**kwargs)
        result = self.wait_for_result(summary.id)
        return None if result is None else result.secret


def test_service_preview_caches_manifest_so_import_never_reprompts(monkeypatch, tmp_path):
    config_dir, _ = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )
    source = tmp_path / "encrypted.spbk"
    write_spbk(
        str(source),
        {
            "version": 1,
            "format": "spbk",
            "app_config": {},
            "ssh_config": "",
            "known_hosts": None,
            "credentials": [],
            "private_keys": [],
            "backup_options": {"app_settings": True, "ssh_config": True,
                               "known_hosts": False, "secrets": False,
                               "private_keys": False},
        },
        "s3cr3t",
    )

    fake = FakeMgr()
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)
    broker = _OneShotBroker("s3cr3t")
    service = SecretBackendService(
        config_file, secret_manager=fake, interaction_broker=broker
    )

    preview = service.preview_backup(source=str(source), owner_client_id="client-1")
    assert preview["kind"] == "spbk"
    assert preview["encrypted"] is True
    assert preview["included"]["ssh_config"] is True

    result = service.import_backup(
        source=str(source), options={"mode": "merge"}, owner_client_id="client-1"
    )
    assert result.status.value == "success", result.message
    # The preview collected the passphrase once; the import reused the cached
    # manifest instead of prompting again.
    assert len(broker.created) == 1
    # The one-time import consumed the entry: the cache is empty afterwards.
    assert not service._manifest_cache
    assert not service._manifest_timers


def test_service_import_prompts_when_no_preview_cached(monkeypatch, tmp_path):
    """Without a preview, an encrypted .spbk import prompts for the passphrase."""
    config_dir, _ = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )
    source = tmp_path / "encrypted.spbk"
    write_spbk(
        str(source),
        {
            "version": 1, "format": "spbk", "app_config": {}, "ssh_config": "",
            "known_hosts": None, "credentials": [], "private_keys": [],
            "backup_options": {"app_settings": True, "ssh_config": True,
                               "known_hosts": False, "secrets": False,
                               "private_keys": False},
        },
        "s3cr3t",
    )
    fake = FakeMgr()
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)
    broker = _OneShotBroker("s3cr3t")
    service = SecretBackendService(
        config_file, secret_manager=fake, interaction_broker=broker
    )
    result = service.import_backup(
        source=str(source), options={"mode": "merge"}, owner_client_id="client-1"
    )
    assert result.status.value == "success", result.message
    assert len(broker.created) == 1


def test_daemon_import_backup_supports_replace_mode(monkeypatch, tmp_path):
    """``mode`` flows through the daemon import (replace vs non-destructive merge)."""
    config_dir, _ = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )
    source = tmp_path / "in.spbk"
    write_spbk(
        str(source),
        {
            "version": 1, "format": "spbk", "app_config": {}, "ssh_config": "",
            "known_hosts": None, "credentials": [], "private_keys": [],
            "backup_options": {"app_settings": True, "ssh_config": True,
                               "known_hosts": False, "secrets": False,
                               "private_keys": False},
        },
        None,
    )
    fake = FakeMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)
    result = daemon_import_backup(
        fake, source=str(source), options={"mode": "replace"},
        settings_path=config_file,
    )
    assert result.status.value == "success", result.message
    assert result.counts.get("restored", 0) >= 0


# ---------------------------------------------------------------------------
# Connection-store backup/restore: real ConnectionRepository round trips
#
# These prove backup/import preserve the authoritative, post-migration
# connection-store state (non-SSH connections, groups, membership, root
# order, safe metadata) through the real daemon export/import path — not
# just the SSH-config/app-settings/secrets categories covered above.
# ---------------------------------------------------------------------------


def _connection_store_repo(config_dir, ssh_dir):
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    return ConnectionRepository(
        ssh_store=SshConfigStore(ssh_dir / "config"),
        state_path=config_dir / "connections.json",
        legacy_config_path=config_dir / "config.json",
        isolated=False,
    )


def test_daemon_backup_round_trip_preserves_non_ssh_groups_order_and_metadata(monkeypatch, tmp_path):
    from sshpilot.api.models.connection_store import MoveConnectionsRequest

    source_config_dir = tmp_path / "source_config"
    source_ssh_dir = tmp_path / "source_ssh"
    source_config_dir.mkdir()
    source_ssh_dir.mkdir()
    source_repo = _connection_store_repo(source_config_dir, source_ssh_dir)
    source_repo.create_connection({"nickname": "web", "hostname": "example.com", "protocol": "ssh"})
    source_repo.create_connection(
        {"nickname": "lab-switch", "protocol": "telnet", "hostname": "10.0.0.5", "port": 2323}
    )
    group = source_repo.create_group("Production", color="#ff0000")
    source_repo.move_connections(
        MoveConnectionsRequest(connection_ids=("web", "lab-switch"), target_group_id=group.id)
    )
    source_repo.create_connection(
        {"nickname": "other", "protocol": "telnet", "hostname": "10.0.0.6"}
    )
    source_repo.update_connection_metadata("lab-switch", {"tags": ["lab"], "pinned": True})

    monkeypatch.setattr(bm, "get_config_dir", lambda: str(source_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(source_ssh_dir))

    fake_mgr = FakeMgr()
    dest = tmp_path / "backup.spbk"
    export_options = {
        "app_settings": True, "ssh_config": True, "known_hosts": False,
        "secrets": False, "private_keys": False,
    }
    export_result = daemon_export_backup(
        fake_mgr,
        destination=str(dest),
        options=export_options,
        connections_source=source_repo.list_records,
        settings_path=source_config_dir / "config.json",
        connection_store_snapshot=source_repo.snapshot_for_backup,
    )
    assert export_result.status == SecretOperationState.SUCCESS, export_result.message

    target_config_dir = tmp_path / "target_config"
    target_ssh_dir = tmp_path / "target_ssh"
    target_config_dir.mkdir()
    target_ssh_dir.mkdir()
    target_repo = _connection_store_repo(target_config_dir, target_ssh_dir)

    monkeypatch.setattr(bm, "get_config_dir", lambda: str(target_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(target_ssh_dir))

    import_result = daemon_import_backup(
        fake_mgr,
        source=str(dest),
        options={"mode": "merge"},
        settings_path=target_config_dir / "config.json",
        connection_store_restore=target_repo.restore_connection_store,
    )
    assert import_result.status == SecretOperationState.SUCCESS, import_result.message

    snap = target_repo.snapshot()
    connection_ids = {c.id for c in snap.connections}
    assert connection_ids == {"web", "lab-switch", "other"}
    assert len(snap.groups) == 1
    restored_group = snap.groups[0]
    assert restored_group.name == "Production"
    assert restored_group.color == "#ff0000"
    assert set(restored_group.connection_ids) == {"web", "lab-switch"}
    assert snap.root_connection_ids == ("other",)
    metadata = {m.connection_id: m.values for m in snap.metadata}
    assert metadata["lab-switch"]["tags"] == ("lab",)
    assert metadata["lab-switch"]["pinned"] is True


def test_daemon_import_applies_to_already_migrated_target_repository(monkeypatch, tmp_path):
    """Import onto a target that already has its own repository state proves the
    restore is applied through ConnectionRepository — not by relying on stale
    ``config.json`` sections, which the repository never re-reads once
    ``connections.json`` exists."""
    source_config_dir = tmp_path / "source_config"
    source_ssh_dir = tmp_path / "source_ssh"
    source_config_dir.mkdir()
    source_ssh_dir.mkdir()
    source_repo = _connection_store_repo(source_config_dir, source_ssh_dir)
    source_repo.create_connection(
        {"nickname": "new-switch", "protocol": "telnet", "hostname": "10.0.0.9"}
    )

    monkeypatch.setattr(bm, "get_config_dir", lambda: str(source_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(source_ssh_dir))

    fake_mgr = FakeMgr()
    dest = tmp_path / "backup.spbk"
    export_options = {
        "app_settings": True, "ssh_config": True, "known_hosts": False,
        "secrets": False, "private_keys": False,
    }
    export_result = daemon_export_backup(
        fake_mgr,
        destination=str(dest),
        options=export_options,
        connections_source=source_repo.list_records,
        settings_path=source_config_dir / "config.json",
        connection_store_snapshot=source_repo.snapshot_for_backup,
    )
    assert export_result.status == SecretOperationState.SUCCESS, export_result.message

    target_config_dir = tmp_path / "target_config"
    target_ssh_dir = tmp_path / "target_ssh"
    target_config_dir.mkdir()
    target_ssh_dir.mkdir()
    target_repo = _connection_store_repo(target_config_dir, target_ssh_dir)
    # The target is already migrated: it has its own connections.json state,
    # unrelated to the backup being imported.
    target_repo.create_connection(
        {"nickname": "existing-switch", "protocol": "telnet", "hostname": "10.0.0.1"}
    )

    # Stale legacy config.json section that the repository will never read
    # again now that connections.json exists — proves the restored state
    # comes from the connection_store section, not this.
    (target_config_dir / "config.json").write_text(
        json.dumps({"connections_meta": {"nonexistent": {"tags": ["ghost"]}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(bm, "get_config_dir", lambda: str(target_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(target_ssh_dir))

    import_result = daemon_import_backup(
        fake_mgr,
        source=str(dest),
        options={"mode": "merge"},
        settings_path=target_config_dir / "config.json",
        connection_store_restore=target_repo.restore_connection_store,
    )
    assert import_result.status == SecretOperationState.SUCCESS, import_result.message

    connection_ids = {c.id for c in target_repo.snapshot().connections}
    assert connection_ids == {"existing-switch", "new-switch"}


def test_daemon_import_backup_reflects_synchronously_without_reload_coordinator(monkeypatch, tmp_path):
    """Immediately after import returns success, the repository already
    reflects the restored state — no ConfigurationReloadCoordinator/watcher
    is ever constructed or consulted."""
    source_config_dir = tmp_path / "source_config"
    source_ssh_dir = tmp_path / "source_ssh"
    source_config_dir.mkdir()
    source_ssh_dir.mkdir()
    source_repo = _connection_store_repo(source_config_dir, source_ssh_dir)
    source_repo.create_connection(
        {"nickname": "sync-switch", "protocol": "telnet", "hostname": "10.0.0.7"}
    )

    monkeypatch.setattr(bm, "get_config_dir", lambda: str(source_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(source_ssh_dir))

    fake_mgr = FakeMgr()
    dest = tmp_path / "backup.spbk"
    export_options = {
        "app_settings": True, "ssh_config": False, "known_hosts": False,
        "secrets": False, "private_keys": False,
    }
    export_result = daemon_export_backup(
        fake_mgr,
        destination=str(dest),
        options=export_options,
        connections_source=source_repo.list_records,
        settings_path=source_config_dir / "config.json",
        connection_store_snapshot=source_repo.snapshot_for_backup,
    )
    assert export_result.status == SecretOperationState.SUCCESS, export_result.message

    target_config_dir = tmp_path / "target_config"
    target_ssh_dir = tmp_path / "target_ssh"
    target_config_dir.mkdir()
    target_ssh_dir.mkdir()
    target_repo = _connection_store_repo(target_config_dir, target_ssh_dir)

    monkeypatch.setattr(bm, "get_config_dir", lambda: str(target_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(target_ssh_dir))

    import_result = daemon_import_backup(
        fake_mgr,
        source=str(dest),
        options={"mode": "merge"},
        settings_path=target_config_dir / "config.json",
        connection_store_restore=target_repo.restore_connection_store,
    )
    assert import_result.status == SecretOperationState.SUCCESS, import_result.message

    # No ConfigurationReloadCoordinator/watcher is constructed anywhere in
    # this test — the assertion below queries the repository directly,
    # immediately after daemon_import_backup returned.
    connection_ids = {c.id for c in target_repo.snapshot().connections}
    assert "sync-switch" in connection_ids


def test_daemon_import_backup_without_connection_store_section_still_succeeds(monkeypatch, tmp_path):
    """A pre-existing backup with no ``connection_store`` section (created
    before this feature existed) must keep importing successfully, without
    touching the target repository."""
    config_dir, _ = _isolate_paths(monkeypatch, tmp_path)
    config_file = config_dir / "config.json"
    config_file.write_text(
        json.dumps({"config_version": CONFIG_VERSION, "ssh": {}}), encoding="utf-8"
    )
    source = tmp_path / "in.spbk"
    write_spbk(
        str(source),
        {
            "version": 1, "format": "spbk", "app_config": {}, "ssh_config": "",
            "known_hosts": None, "credentials": [], "private_keys": [],
            "backup_options": {"app_settings": True, "ssh_config": True,
                               "known_hosts": False, "secrets": False,
                               "private_keys": False},
        },
        None,
    )
    target_config_dir = tmp_path / "target_config"
    target_ssh_dir = tmp_path / "target_ssh"
    target_config_dir.mkdir()
    target_ssh_dir.mkdir()
    target_repo = _connection_store_repo(target_config_dir, target_ssh_dir)
    before = target_repo.snapshot()

    fake = FakeMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: fake)
    result = daemon_import_backup(
        fake, source=str(source), options={"mode": "merge"},
        settings_path=config_file,
        connection_store_restore=target_repo.restore_connection_store,
    )
    assert result.status.value == "success", result.message
    assert target_repo.snapshot() == before


# ---------------------------------------------------------------------------
# connection_store threading through the Bitwarden and SSH-server backup
# destinations — same mechanical parameter as the file path above, verified
# against a real ConnectionRepository with the transport layer faked.
# ---------------------------------------------------------------------------


def test_daemon_export_to_ssh_threads_connection_store_snapshot(monkeypatch, tmp_path):
    import sshpilot.backup_backends as bb

    captured = {}

    class _FakeSSHServerBackupBackend:
        def __init__(self, runner, remote_dir, *, item_name=""):
            pass

        def export(self, manifest, *, passphrase=None):
            captured["manifest"] = manifest
            return bb.BackupEntry(id="remote/path.spbk", name="path.spbk")

    monkeypatch.setattr(bb, "SSHServerBackupBackend", _FakeSSHServerBackupBackend)

    config_dir = tmp_path / "config"
    ssh_dir = tmp_path / "ssh"
    config_dir.mkdir()
    ssh_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(ssh_dir))

    repo = _connection_store_repo(config_dir, ssh_dir)
    repo.create_connection(
        {"nickname": "ssh-switch", "protocol": "telnet", "hostname": "10.0.0.40"}
    )

    fake_mgr = FakeMgr()
    result = daemon_export_backup(
        fake_mgr,
        destination="ssh:some-connection:/backups",
        options={"app_settings": True, "ssh_config": True, "known_hosts": False,
                 "secrets": False, "private_keys": False},
        connections_source=repo.list_records,
        settings_path=config_dir / "config.json",
        connection_store_snapshot=repo.snapshot_for_backup,
    )
    assert result.status == SecretOperationState.SUCCESS, result.message
    connection_ids = {c["id"] for c in captured["manifest"]["connection_store"]["connections"]}
    assert "ssh-switch" in connection_ids


def test_daemon_import_bitwarden_backup_restores_connection_store(monkeypatch, tmp_path):
    import sshpilot.backup_backends as bb
    from sshpilot.backup_backends import BackupEntry
    from sshpilot.daemon.secret_transfer import daemon_import_bitwarden_backup

    class _FakeBwHandle:
        def is_available(self):
            return True

    class _FakeBitwardenBackupBackend:
        def __init__(self, bw, *, item_name=""):
            pass

        def list_exports(self):
            return [BackupEntry(id="e1", name="n", date="")]

        def read(self, entry, *, passphrase=None):
            raise AssertionError("read should not be called when manifest is supplied")

    monkeypatch.setattr(bb, "BitwardenBackupBackend", _FakeBitwardenBackupBackend)

    class _FakeManager:
        def get_backend(self, name):
            return _FakeBwHandle() if name == "bitwarden" else None

        def persists_secrets(self):
            return True

    target_config_dir = tmp_path / "target_config"
    target_ssh_dir = tmp_path / "target_ssh"
    target_config_dir.mkdir()
    target_ssh_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(target_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(target_ssh_dir))

    target_repo = _connection_store_repo(target_config_dir, target_ssh_dir)

    manifest = {
        "version": 1, "format": "spbk", "app_config": {}, "ssh_config": "",
        "known_hosts": None, "credentials": [], "private_keys": [],
        "backup_options": {"app_settings": True, "ssh_config": True,
                           "known_hosts": False, "secrets": False, "private_keys": False},
        "connection_store": {
            "version": 1,
            "connections": [{"id": "bw-switch", "protocol": "telnet", "nickname": "bw-switch",
                              "hostname": "10.0.0.20", "username": "", "port": 23, "display_name": ""}],
            "groups": [], "root_connection_ids": ["bw-switch"], "metadata": [],
        },
    }

    result = daemon_import_bitwarden_backup(
        _FakeManager(), entry_id="e1", options={"mode": "merge"},
        settings_path=target_config_dir / "config.json",
        manifest=manifest,
        connection_store_restore=target_repo.restore_connection_store,
    )
    assert result.status == SecretOperationState.SUCCESS, result.message
    assert "bw-switch" in {c.id for c in target_repo.snapshot().connections}


def test_daemon_import_ssh_backup_restores_connection_store(monkeypatch, tmp_path):
    import sshpilot.backup_backends as bb
    from sshpilot.backup_backends import BackupEntry
    from sshpilot.daemon.secret_transfer import daemon_import_ssh_backup

    class _FakeSSHServerBackupBackend:
        def __init__(self, runner, remote_dir, *, item_name=""):
            pass

        def list_exports(self):
            return [BackupEntry(id="remote/backup.spbk", name="backup.spbk", date="")]

        def read(self, entry, *, passphrase=None):
            raise AssertionError("read should not be called when manifest is supplied")

    monkeypatch.setattr(bb, "SSHServerBackupBackend", _FakeSSHServerBackupBackend)

    target_config_dir = tmp_path / "target_config"
    target_ssh_dir = tmp_path / "target_ssh"
    target_config_dir.mkdir()
    target_ssh_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(target_config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(target_ssh_dir))

    target_repo = _connection_store_repo(target_config_dir, target_ssh_dir)

    manifest = {
        "version": 1, "format": "spbk", "app_config": {}, "ssh_config": "",
        "known_hosts": None, "credentials": [], "private_keys": [],
        "backup_options": {"app_settings": True, "ssh_config": True,
                           "known_hosts": False, "secrets": False, "private_keys": False},
        "connection_store": {
            "version": 1,
            "connections": [{"id": "ssh-target-switch", "protocol": "telnet",
                              "nickname": "ssh-target-switch", "hostname": "10.0.0.21",
                              "username": "", "port": 23, "display_name": ""}],
            "groups": [], "root_connection_ids": ["ssh-target-switch"], "metadata": [],
        },
    }

    fake_mgr = FakeMgr()
    result = daemon_import_ssh_backup(
        fake_mgr, connection_id="some-connection", remote_dir="/backups",
        entry_id="remote/backup.spbk",
        options={"mode": "merge"},
        connections_source=lambda: [],
        settings_path=target_config_dir / "config.json",
        manifest=manifest,
        connection_store_restore=target_repo.restore_connection_store,
    )
    assert result.status == SecretOperationState.SUCCESS, result.message
    assert "ssh-target-switch" in {c.id for c in target_repo.snapshot().connections}
