"""Tests for the pluggable backup destinations (sshpilot/backup_backends.py)."""

import base64
import os
import shlex

import pytest

from sshpilot.backup_backends import (
    BackupEntry,
    BackupError,
    BackupTooLargeForNote,
    BitwardenBackupBackend,
    SSHServerBackupBackend,
    SpbkFileBackend,
    decode_manifest_note,
    encode_manifest_note,
)


class FakeBw:
    """Stand-in for secret_storage.BitwardenBackend's secure-note methods."""
    def __init__(self):
        self.items = {}
        self._n = 0

    def create_or_update_secure_note(self, name, content):
        for iid, it in self.items.items():
            if it["name"] == name:
                it["notes"] = content
                return iid
        self._n += 1
        iid = f"id{self._n}"
        self.items[iid] = {"id": iid, "name": name, "notes": content, "type": 2,
                           "revisionDate": name}
        return iid

    def list_secure_notes(self, name_prefix):
        return [it for it in self.items.values()
                if it["type"] == 2 and it["name"].startswith(name_prefix)]

    def read_secure_note(self, item_id):
        it = self.items.get(item_id)
        return it["notes"] if it else None


def test_encode_decode_roundtrip():
    manifest = {"version": 1, "ssh_config": "Host x\n    HostName x\n",
                "credentials": [{"id": "u@h", "secret": "pw"}]}
    note = encode_manifest_note(manifest)
    assert note.splitlines()[0] == "SSHPILOT-BACKUP-v1"
    assert decode_manifest_note(note) == manifest


def test_decode_rejects_foreign_note():
    with pytest.raises(BackupError):
        decode_manifest_note("just a random secure note")


def test_spbk_file_backend_roundtrip(tmp_path):
    path = str(tmp_path / "b.spbk")
    backend = SpbkFileBackend(path)
    manifest = {"version": 1, "x": "y", "credentials": []}
    entry = backend.export(manifest, passphrase="pw")
    assert entry.id == path
    got = backend.read(BackupEntry(id=path, name="b.spbk"), passphrase="pw")
    assert got == manifest


def test_bitwarden_backup_roundtrip():
    bw = FakeBw()
    backend = BitwardenBackupBackend(bw, item_name="sshPilot Backup 2026-01-01 10:00")
    manifest = {"version": 1, "ssh_config": "Host a\n", "credentials": [{"id": "u@h"}]}
    entry = backend.export(manifest)
    assert entry.name == "sshPilot Backup 2026-01-01 10:00"
    listed = backend.list_exports()
    assert [e.name for e in listed] == [entry.name]
    assert backend.read(listed[0]) == manifest


def test_bitwarden_too_large_raises_and_stores_nothing():
    bw = FakeBw()
    backend = BitwardenBackupBackend(bw, item_name="sshPilot Backup big")
    incompressible = base64.b64encode(os.urandom(40000)).decode("ascii")  # won't gzip under 10k
    with pytest.raises(BackupTooLargeForNote):
        backend.export({"blob": incompressible})
    assert bw.items == {}


def test_bitwarden_read_foreign_item_raises():
    bw = FakeBw()
    iid = bw.create_or_update_secure_note("someone's note", "not a backup")
    backend = BitwardenBackupBackend(bw, item_name="x")
    with pytest.raises(BackupError):
        backend.read(BackupEntry(id=iid, name="someone's note"))


class FakeRunner:
    """In-memory stand-in for OpenSSHSFTPManager.run_command, modelling the remote host as a
    dict of path -> bytes and parsing the exact commands SSHServerBackupBackend issues."""
    def __init__(self, avail_kb=99000, rc_preflight=0):
        self.files = {}
        self.avail_kb = avail_kb
        self.rc_preflight = rc_preflight
        self.calls = []

    def run_command(self, cmd, *, input=None, timeout=30):
        self.calls.append(cmd)
        if cmd.startswith("mkdir -p"):   # preflight: create/write check + df line
            if self.rc_preflight != 0:
                return self.rc_preflight, b"", "permission denied"
            return 0, f"/dev/sda1 1000000 1 {self.avail_kb} 1% /home".encode(), ""
        if cmd.startswith("cat > "):      # upload: cat > part && mv part final
            self.files[shlex.split(cmd)[-1]] = input
            return 0, b"", ""
        if cmd.startswith("ls "):         # list: ls -1 <dir>/*.spbk 2>/dev/null
            prefix = shlex.split(cmd)[2][:-len("*.spbk")]
            hits = sorted(k for k in self.files
                          if k.startswith(prefix) and k.endswith(".spbk"))
            return (0, "\n".join(hits).encode(), "") if hits else (1, b"", "")
        if cmd.startswith("cat "):        # download
            data = self.files.get(shlex.split(cmd)[1])
            return (0, data, "") if data is not None else (1, b"", "No such file")
        return 0, b"", ""


def test_ssh_server_backend_roundtrip():
    runner = FakeRunner()
    manifest = {"version": 1, "ssh_config": "Host a\n", "credentials": [{"id": "u@h"}]}
    backend = SSHServerBackupBackend(runner, item_name="sshpilot_backup_20260711_1830.spbk")
    entry = backend.export(manifest, passphrase="pw")
    assert entry.id == "~/sshpilot-backups/sshpilot_backup_20260711_1830.spbk"
    assert any(c.startswith("mkdir -p") for c in runner.calls)   # preflight ran
    listed = backend.list_exports()
    assert [e.name for e in listed] == ["sshpilot_backup_20260711_1830.spbk"]
    assert listed[0].date == "2026-07-11"
    assert backend.read(listed[0], passphrase="pw") == manifest


def test_ssh_server_preflight_rejects_unwritable_dir():
    backend = SSHServerBackupBackend(FakeRunner(rc_preflight=1))
    with pytest.raises(BackupError):
        backend.export({"version": 1, "credentials": []})


def test_ssh_server_preflight_rejects_insufficient_space():
    backend = SSHServerBackupBackend(FakeRunner(avail_kb=0))
    with pytest.raises(BackupError):
        backend.export({"version": 1, "credentials": []})


def test_prompt_unlock_targets_given_backend():
    """prompt_unlock(backend=…) must use the passed backend, not the Preferences selection.
    An already-unlocked target reports success immediately without any dialog."""
    from sshpilot import secret_unlock_dialog as sud

    class FakeBackend:
        name = "bitwarden"
        session_backed = True
        def is_available(self):
            return True
        def is_unlocked(self):
            return True

    calls = []
    owned = sud.prompt_unlock(None, backend=FakeBackend(), on_done=lambda ok: calls.append(ok))
    assert owned is True
    assert calls == [True]
