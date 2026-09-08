"""Tests for the pluggable backup destinations (sshpilot/backup_backends.py)."""

import base64
import os
import tempfile

import pytest

from sshpilot.api.models.secrets import SecretTransferMessageCode
from sshpilot.backup_backends import (
    BW_NOTE_MAX_CHARS,
    BackupEntry,
    BackupError,
    BackupTooLargeForNote,
    BitwardenBackupBackend,
    SSHServerBackupBackend,
    SpbkFileBackend,
    backup_entry_for,
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


class FakeStore:
    """In-memory stand-in for a RemoteBackupStore, modelling the remote host as a
    dict of path -> bytes.

    ``free_kb=None`` simulates a transport that cannot answer the free-space
    question (SFTP has no statvfs); ``list_error`` makes listing raise, as a
    broken connection does."""

    def __init__(self, free_kb=99000, ensure_error=False, list_error=None):
        self.files = {}
        self.free_kb = free_kb
        self.ensure_error = ensure_error
        self.list_error = list_error
        self.closed = False

    def ensure_directory(self, path):
        if self.ensure_error:
            raise BackupError(
                SecretTransferMessageCode.SSH_SERVER_DIRECTORY_UNAVAILABLE,
                parameters={"directory": path},
            )

    def free_space_bytes(self, path):
        return None if self.free_kb is None else self.free_kb * 1024

    def list_backups(self, path):
        if self.list_error is not None:
            raise self.list_error
        prefix = path.rstrip("/") + "/"
        return [
            backup_entry_for(key)
            for key in sorted(self.files)
            if key.startswith(prefix) and key.endswith(".spbk")
        ]

    def upload(self, local_path, remote_path):
        with open(local_path, "rb") as handle:
            self.files[remote_path] = handle.read()

    def download(self, remote_path, local_path):
        data = self.files.get(remote_path)
        if data is None:
            raise BackupError(SecretTransferMessageCode.SSH_BACKUP_READ_FAILED)
        with open(local_path, "wb") as handle:
            handle.write(data)

    def close(self):
        self.closed = True


def test_ssh_server_backend_roundtrip():
    store = FakeStore()
    manifest = {"version": 1, "ssh_config": "Host a\n", "credentials": [{"id": "u@h"}]}
    backend = SSHServerBackupBackend(store, item_name="sshpilot_backup_20260711_1830.spbk")
    entry = backend.export(manifest, passphrase="pw")
    assert entry.id == "~/sshpilot-backups/sshpilot_backup_20260711_1830.spbk"
    listed = backend.list_exports()
    assert [e.name for e in listed] == ["sshpilot_backup_20260711_1830.spbk"]
    assert listed[0].date == "2026-07-11"
    assert backend.read(listed[0], passphrase="pw") == manifest


def test_ssh_server_preflight_rejects_unwritable_dir():
    backend = SSHServerBackupBackend(FakeStore(ensure_error=True))
    with pytest.raises(BackupError):
        backend.export({"version": 1, "credentials": []})


def test_ssh_server_preflight_rejects_insufficient_space():
    backend = SSHServerBackupBackend(FakeStore(free_kb=0))
    with pytest.raises(BackupError):
        backend.export({"version": 1, "credentials": []})


def test_ssh_server_export_succeeds_when_free_space_is_unknown():
    # SFTP cannot answer the free-space question; the check is soft, not fatal.
    backend = SSHServerBackupBackend(FakeStore(free_kb=None),
                                     item_name="sshpilot_backup_x.spbk")
    entry = backend.export({"version": 1, "credentials": []})
    assert entry.name == "sshpilot_backup_x.spbk"


def test_ssh_server_list_propagates_connect_failure():
    error = BackupError(SecretTransferMessageCode.SSH_SERVER_CONNECTION_FAILED)
    backend = SSHServerBackupBackend(FakeStore(list_error=error))
    with pytest.raises(BackupError):
        backend.list_exports()


def test_ssh_server_list_empty_when_no_files():
    assert SSHServerBackupBackend(FakeStore()).list_exports() == []


def test_ssh_server_download_raises_on_missing():
    backend = SSHServerBackupBackend(FakeStore())
    with pytest.raises(BackupError):
        backend.read(BackupEntry(id="~/sshpilot-backups/nope.spbk", name="nope.spbk"),
                     passphrase="x")


def test_ssh_server_export_leaves_no_local_temp_behind(tmp_path, monkeypatch):
    """The staging archive an export writes must not survive the call.

    Scoped to its own temp directory: reading the shared one made this observe
    every other test's temp files too, so anything legitimately holding a
    ``.spbk`` in parallel (the preview retains one across its passphrase
    prompt) failed it at random under xdist.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    store = FakeStore()
    backend = SSHServerBackupBackend(store, item_name="b.spbk")
    before = set(os.listdir(tmp_path))
    backend.export({"version": 1, "credentials": []})
    leaked = {n for n in set(os.listdir(tmp_path)) - before
              if n.endswith(".spbk")}
    assert not leaked


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


def test_bitwarden_too_large_names_the_dominant_part():
    """The refusal must say what is big, not guess.

    Which part dominates depends on the vault: an app config with a long
    session-restore blob can outweigh the private keys several times over, so
    a fixed "private keys are the problem" hint sends the user to uncheck the
    wrong box.
    """
    bw = FakeBw()
    backend = BitwardenBackupBackend(bw, item_name="sshPilot Backup big")
    manifest = {
        "version": 1,
        "ssh_config": "Host a\n",
        "private_keys": [{"path": "/k", "content": "short"}],
        # Incompressible, so it cannot be squeezed under the limit.
        "app_config": {"blob": base64.b64encode(os.urandom(30000)).decode("ascii")},
    }
    with pytest.raises(BackupTooLargeForNote) as excinfo:
        backend.export(manifest)
    error = excinfo.value
    assert error.largest_section == "app_settings"
    assert error.largest_section != "private_keys"
    assert error.transfer_message.parameters["length"] > BW_NOTE_MAX_CHARS
    assert bw.items == {}


def test_largest_note_section_measures_encoded_cost_not_raw_size():
    """Compression decides the cost: a large but repetitive section can be
    cheaper than a small high-entropy one."""
    from sshpilot.backup_backends import largest_note_section

    manifest = {
        "app_config": {"repeated": "sshpilot " * 4000},          # ~36k raw, compresses away
        "private_keys": [{"content": base64.b64encode(os.urandom(3000)).decode("ascii")}],
    }
    label, cost = largest_note_section(manifest)
    assert label == "private_keys"
    assert cost > 0


def test_largest_note_section_ignores_absent_parts():
    from sshpilot.backup_backends import largest_note_section

    label, cost = largest_note_section({"version": 1, "credentials": [], "app_config": {}})
    assert (label, cost) == ("", 0)
