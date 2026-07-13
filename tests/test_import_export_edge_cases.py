"""Regression tests for import/export edge cases (from an adversarial bug hunt).

Some tests pin fixed behavior (vault-gate abort, partial-collision split); others document
deliberate design choices found during the hunt (auto-backup is config-only because credential
restore is non-destructive; legacy JSON never restores secrets; the vault gate lives in the UI).
"""
import json
import os

import pytest

import sshpilot.backup_manager as bm
import sshpilot.credential_manager as cmod
import sshpilot.secret_storage as ss
from sshpilot.backup_archive import read_spbk
from sshpilot.secret_storage import password_spec
from sshpilot.window_dialogs import WindowConfigDialogsMixin


class FakeMgr:
    def __init__(self):
        self.data = {}

    def store(self, spec, secret):
        self.data[spec.keyring_account] = secret
        return True

    def lookup_everywhere(self, spec):
        v = self.data.get(spec.keyring_account)
        return (v, "libsecret") if v else None

    def lookup(self, spec):
        return self.data.get(spec.keyring_account)

    def all_available_backends(self):
        return []


class FakeConn:
    def __init__(self, nickname, hostname, username, host=""):
        self.nickname = nickname
        self.hostname = hostname
        self.host = host
        self.username = username
        self.port = 22
        self.keyfile = ""
        self.identity_files = []
        self.resolved_identity_files = []

    def get_effective_host(self):
        return self.hostname or self.host or self.nickname


class FakeConfig:
    config_data = {"secrets": {}}

    def get_setting(self, key, default=None):
        return default

    def get_default_config(self):
        return {}

    def load_json_config(self):
        return self.config_data


class FakeConnMgr:
    def __init__(self, conns, ssh_config_path="", isolated_mode=False):
        self._conns = conns
        self.ssh_config_path = ssh_config_path
        self.isolated_mode = isolated_mode

    def get_connections(self):
        return list(self._conns)

    def load_ssh_config(self):
        pass


def _mgr(tmp_path, monkeypatch, conns=None, ssh_config_path=""):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "cfg"))
    return bm.BackupManager(
        FakeConfig(),
        FakeConnMgr(conns or [], ssh_config_path=ssh_config_path),
    )


# ---------------------------------------------------------------------------
# BUG 8 (data loss, HIGH): password stored under a legacy host alias is NOT
# exported after the connection nickname/hostname is renamed — include_orphans
# is False and password_host_candidates no longer probes the old key.
# ---------------------------------------------------------------------------
def test_bug8_renamed_connection_loses_legacy_password_on_export(monkeypatch, tmp_path):
    fake = FakeMgr()
    # Password was saved when the connection was still nicknamed "OldBox".
    fake.data[password_spec("OldBox", "alice").keyring_account] = "secret-pw"

    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)

    # User renamed the connection; nickname and Host now use the FQDN only.
    conn = FakeConn("prod.example.com", "prod.example.com", "alice")
    mgr = _mgr(tmp_path, monkeypatch, conns=[conn])

    path = str(tmp_path / "renamed.spbk")
    ok, err = mgr.export_backup(
        path,
        connections=[conn],
        passphrase=None,
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": True,
            "private_keys": False,
        },
    )
    assert ok, err
    manifest = read_spbk(path)
    # The password still exists in the vault but export silently omits it.
    assert manifest["credentials"] == [], (
        "expected data-loss gap: legacy alias password not exported after rename"
    )


# ---------------------------------------------------------------------------
# Legacy JSON import restores config only (never secrets — .spbk is the secret
# format). The count of ignored credentials is now recorded so the UI can warn.
# ---------------------------------------------------------------------------
def test_bug9_json_import_silently_ignores_embedded_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "get_secret_manager", lambda: FakeMgr())
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(cfg_dir))

    mgr = _mgr(tmp_path, monkeypatch)
    import_path = tmp_path / "handcrafted.json"
    import_path.write_text(
        json.dumps({
            "version": 1,
            "app_config": {"ui": {"theme": "dark"}},
            "ssh_config": "",
            "credentials": [
                {
                    "id": "alice@prod.example.com",
                    "type": "password",
                    "host": "prod.example.com",
                    "username": "alice",
                    "secret": "from-json",
                },
            ],
        }),
        encoding="utf-8",
    )

    success, error = mgr.import_configuration(str(import_path), mode="replace", create_backup=False)
    assert success, error
    # Credentials were present in the file but never written to the vault (legacy JSON is
    # config-only); the count is now recorded so the UI can warn instead of silently dropping.
    assert ss.get_secret_manager().data == {}
    assert mgr.last_import_ignored_secrets == 1


# ---------------------------------------------------------------------------
# BUG 10 (FIXED): vault-unlock gate aborts on exception instead of proceeding,
# so a failed vault probe cannot silently produce a credential-less backup.
# ---------------------------------------------------------------------------
class _Win(WindowConfigDialogsMixin):
    def __init__(self):
        self.dialogs = []

    def _simple_dialog(self, heading, body):
        self.dialogs.append((heading, body))


def test_bug10_vault_unlock_exception_aborts(monkeypatch):
    win = _Win()
    calls = []

    def boom():
        raise RuntimeError("vault probe failed")

    monkeypatch.setattr(
        "sshpilot.secret_storage.get_secret_manager", boom)
    win._run_after_vault_unlock_for_secrets(
        lambda: calls.append(1),
        needed=True,
        cancelled_heading="Export Cancelled",
    )
    # Fixed: the operation is aborted and the user is told, not silently continued.
    assert calls == []
    assert win.dialogs and win.dialogs[0][0] == "Export Cancelled"


# ---------------------------------------------------------------------------
# BUG 11 (FIXED): merge splits a multi-name Host stanza on partial collision —
# the non-colliding names are imported; the colliding one is left as-is + reported.
# ---------------------------------------------------------------------------
def test_bug11_merge_partial_host_collision_imports_new_name(tmp_path, monkeypatch):
    root = tmp_path / ".ssh"
    root.mkdir()
    main = root / "config"
    main.write_text(
        "Host prod\n    HostName prod.example\n",
        encoding="utf-8",
    )
    mgr = _mgr(tmp_path, monkeypatch, ssh_config_path=str(main))

    import_data = {
        "source_home": "/home/src",
        "ssh_config": (
            "Host prod staging\n"
            "    HostName prod.example\n"
            "    User deploy\n"
        ),
    }
    ok, err = mgr._import_merge(import_data, {
        "app_settings": False,
        "ssh_config": True,
        "known_hosts": False,
        "secrets": False,
        "private_keys": False,
    })
    assert ok, err
    assert mgr.last_merge_collisions == [["prod", "staging"]]
    fragment = root / bm._IMPORT_FRAGMENT_NAME
    text = fragment.read_text(encoding="utf-8")
    # "staging" (new) is imported via a split header; "prod" (existing) is not re-added.
    host_lines = [l for l in text.splitlines() if l.strip().lower().startswith("host ")]
    assert host_lines == ["Host staging"]
    assert "User deploy" in text          # the stanza body is preserved


# ---------------------------------------------------------------------------
# Merge deliberately drops Match/global blocks (they affect every host); the
# count is recorded (last_merge_dropped_globals) and now shown in the UI.
# ---------------------------------------------------------------------------
def test_bug12_merge_drops_match_blocks_without_collision_warning(tmp_path, monkeypatch):
    root = tmp_path / ".ssh"
    root.mkdir()
    main = root / "config"
    main.write_text("Host local-only\n", encoding="utf-8")
    mgr = _mgr(tmp_path, monkeypatch, ssh_config_path=str(main))

    import_data = {
        "source_home": "/home/src",
        "ssh_config": (
            "Match User alice\n"
            "    ForceCommand /usr/local/bin/restricted-shell\n"
            "\n"
            "Host new-remote\n"
            "    HostName new.example\n"
        ),
    }
    ok, err = mgr._import_merge(import_data, {
        "app_settings": False,
        "ssh_config": True,
        "known_hosts": False,
        "secrets": False,
        "private_keys": False,
    })
    assert ok, err
    assert mgr.last_merge_dropped_globals >= 1
    assert mgr.last_merge_collisions == []
    fragment = root / bm._IMPORT_FRAGMENT_NAME
    text = fragment.read_text(encoding="utf-8") if fragment.exists() else ""
    assert "ForceCommand" not in text
    assert "new-remote" in text


# ---------------------------------------------------------------------------
# BUG 13 (BY DESIGN): the pre-import auto-backup is config-only (no secrets).
# That is intentional: credential restore is non-destructive (skip-existing), so
# import never overwrites a vault secret and there is nothing to roll back. This
# test pins that the auto-backup deliberately omits credentials.
# ---------------------------------------------------------------------------
def test_bug13_auto_backup_excludes_secrets(monkeypatch, tmp_path):
    fake = FakeMgr()
    fake.data[password_spec("h.example", "alice").keyring_account] = "pw"
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    backups = cfg_dir / "backups"
    backups.mkdir()
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(cfg_dir))

    conn = FakeConn("A", "h.example", "alice")
    mgr = _mgr(tmp_path, monkeypatch, conns=[conn])

    # Simulate export_configuration used by _create_auto_backup (legacy JSON path).
    auto_path = backups / "auto_backup_test.json"
    ok, err = mgr.export_configuration(str(auto_path))
    assert ok, err
    data = json.loads(auto_path.read_text(encoding="utf-8"))
    assert "credentials" not in data
    assert fake.data  # password still only in the vault, not in the safety net


# ---------------------------------------------------------------------------
# Manager layer: secrets=True with zero connections yields no credentials (they
# are per-connection). The export DIALOG now blocks this by requiring >=1
# connection when secrets/keys are enabled; this pins the manager-layer contract.
# ---------------------------------------------------------------------------
def test_bug14_secrets_enabled_zero_connections_exports_empty(monkeypatch, tmp_path):
    fake = FakeMgr()
    fake.data[password_spec("h.example", "alice").keyring_account] = "pw"
    monkeypatch.setattr(cmod, "get_secret_manager", lambda: fake)

    mgr = _mgr(tmp_path, monkeypatch)
    path = str(tmp_path / "empty-conn.spbk")
    ok, err = mgr.export_backup(
        path,
        connections=[],  # secrets on, nobody selected
        passphrase=None,
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": True,
            "private_keys": False,
        },
    )
    assert ok, err
    manifest = read_spbk(path)
    assert manifest["credentials"] == []
    assert mgr.last_export_counts["credentials"] == 0


# ---------------------------------------------------------------------------
# BUG 15 (connectivity gap, MEDIUM): restored password under legacy alias does
# not auto-migrate to canonical host — works only if nickname alias survives.
# ---------------------------------------------------------------------------
def test_bug15_restored_legacy_password_misses_after_nickname_removed(monkeypatch, tmp_path):
    stored = FakeMgr()
    monkeypatch.setattr(ss, "get_secret_manager", lambda: stored)
    mgr = _mgr(tmp_path, monkeypatch)

    # Backup captured password under legacy nickname host key (see bug 8 export path).
    manifest = {
        "credentials": [{
            "id": "alice@OldBox",
            "type": "password",
            "host": "OldBox",
            "username": "alice",
            "secret": "legacy-pw",
        }],
    }
    assert mgr._restore_credentials(manifest) == 1

    # Target machine: connection renamed to FQDN only — no alias probe hits "OldBox".
    from sshpilot.credential_manager import CredentialManager

    conn = FakeConn("prod.example.com", "prod.example.com", "alice")
    creds = CredentialManager([conn], secret_manager=stored).list_credentials(
        include_orphans=False
    )
    passwords = [c for c in creds if c.type == "password"]
    assert passwords == [], "restored legacy password is invisible to renamed connection"


# ---------------------------------------------------------------------------
# A referenced-but-missing key file is skipped on export (can't back up what's
# gone) and recorded in last_export_missing_key_files so the UI can warn.
# ---------------------------------------------------------------------------
def test_bug16_missing_private_key_file_silently_omitted(monkeypatch, tmp_path):
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(tmp_path / "cfg"))
    conn = FakeConn("A", "h.example", "alice")
    conn.keyfile = str(tmp_path / "nonexistent_key")
    mgr = bm.BackupManager(FakeConfig(), FakeConnMgr([conn]))
    path = str(tmp_path / "nokey.spbk")
    ok, err = mgr.export_backup(
        path,
        connections=[conn],
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": False,
            "private_keys": True,
        },
    )
    assert ok, err
    manifest = read_spbk(path)
    assert manifest["private_keys"] == []
    # The missing file is now recorded so export can warn instead of silently omitting it.
    assert mgr.last_export_missing_key_files == [str(tmp_path / "nonexistent_key")]


# ---------------------------------------------------------------------------
# BUG 17 (silent omission, MEDIUM): export with a locked session vault yields
# zero credentials at the manager layer — BackupManager has no unlock gate;
# only the UI prompts. Programmatic/export-after-exception paths can ship empty.
# ---------------------------------------------------------------------------
def test_bug17_locked_vault_exports_zero_credentials(monkeypatch, tmp_path):
    class LockedMgr(FakeMgr):
        def lookup_everywhere(self, spec):
            return None  # locked vault: nothing readable

    monkeypatch.setattr(cmod, "get_secret_manager", lambda: LockedMgr())
    conn = FakeConn("A", "h.example", "alice")
    mgr = _mgr(tmp_path, monkeypatch, conns=[conn])
    path = str(tmp_path / "locked.spbk")
    ok, err = mgr.export_backup(
        path,
        connections=[conn],
        options={
            "app_settings": False,
            "ssh_config": False,
            "known_hosts": False,
            "secrets": True,
            "private_keys": False,
        },
    )
    assert ok, err
    assert read_spbk(path)["credentials"] == []
