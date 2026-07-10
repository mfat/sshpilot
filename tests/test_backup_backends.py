"""Tests for the pluggable backup destinations (sshpilot/backup_backends.py)."""

import base64
import os

import pytest

from sshpilot.backup_backends import (
    BackupEntry,
    BackupError,
    BackupTooLargeForNote,
    BitwardenBackupBackend,
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
