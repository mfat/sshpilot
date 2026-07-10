"""Pluggable backup/restore *destinations* for the manifest that :mod:`backup_manager` builds.

Two transports, same manifest dict:

- :class:`SpbkFileBackend` — the local ``.spbk`` file (existing behaviour), via
  :func:`backup_archive.write_spbk` / :func:`backup_archive.read_spbk`.
- :class:`BitwardenBackupBackend` — a single Bitwarden **secure note** holding
  ``SSHPILOT-BACKUP-v1\\n<base64(gzip(json(manifest)))>``. No attachments (premium/Flatpak
  issues) and no extra passphrase envelope — the note relies on Bitwarden's own vault encryption.
  If the encoded note would exceed the field limit, :class:`BackupTooLargeForNote` is raised so
  the caller can fall back to a ``.spbk`` file.

GTK-free. The Bitwarden backend depends only on a small duck-typed object exposing
``create_or_update_secure_note`` / ``list_secure_notes`` / ``read_secure_note`` (implemented by
``secret_storage.BitwardenBackend``), so it is trivially fakeable in tests.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
from dataclasses import dataclass
from typing import List, Optional, Protocol

BACKUP_NOTE_MAGIC = "SSHPILOT-BACKUP-v1"
BACKUP_ITEM_PREFIX = "sshPilot Backup"
# Bitwarden's note field caps around 10k characters (Vaultwarden may allow more). We refuse
# rather than silently truncate.
BW_NOTE_MAX_CHARS = 10_000


class BackupError(Exception):
    """A backup destination could not complete the operation."""


class BackupTooLargeForNote(BackupError):
    """The encoded manifest exceeds the Bitwarden note field limit — use a ``.spbk`` file."""


@dataclass
class BackupEntry:
    """A stored backup a destination can list/read (an item id + display name)."""
    id: str
    name: str
    date: str = ""


class BackupBackend(Protocol):
    name: str

    def export(self, manifest: dict, *, passphrase: Optional[str] = None) -> BackupEntry: ...

    def list_exports(self) -> List[BackupEntry]: ...

    def read(self, entry: BackupEntry, *, passphrase: Optional[str] = None) -> dict: ...


# --- manifest <-> note payload -----------------------------------------------

def encode_manifest_note(manifest: dict) -> str:
    """``SSHPILOT-BACKUP-v1`` header + ``base64(gzip(json(manifest)))``."""
    raw = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    packed = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")
    return f"{BACKUP_NOTE_MAGIC}\n{packed}"


def decode_manifest_note(note: str) -> dict:
    parts = (note or "").split("\n", 1)
    if len(parts) != 2 or parts[0].strip() != BACKUP_NOTE_MAGIC:
        raise BackupError("not an sshPilot backup note")
    try:
        raw = gzip.decompress(base64.b64decode(parts[1].strip().encode("ascii")))
        return json.loads(raw.decode("utf-8"))
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"corrupt backup note: {exc}") from exc


# --- backends ----------------------------------------------------------------

class SpbkFileBackend:
    """Local ``.spbk`` file destination (unchanged behaviour)."""
    name = "file"

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)

    def export(self, manifest: dict, *, passphrase: Optional[str] = None) -> BackupEntry:
        from .backup_archive import write_spbk
        write_spbk(self.path, manifest, passphrase or None)
        return BackupEntry(id=self.path, name=os.path.basename(self.path))

    def list_exports(self) -> List[BackupEntry]:
        return []   # file import uses the OS file chooser, not a listing

    def read(self, entry: BackupEntry, *, passphrase: Optional[str] = None) -> dict:
        from .backup_archive import read_spbk
        return read_spbk(entry.id, passphrase or None)


class BitwardenBackupBackend:
    """Bitwarden secure-note destination. ``bw`` is a duck-typed object (the ``bitwarden``
    ``SecretBackend``) exposing the three secure-note methods. ``item_name`` is the name for a new
    export (the caller supplies the timestamp so this class stays clock-free / testable)."""
    name = "bitwarden"

    def __init__(self, bw, *, item_name: str = ""):
        self._bw = bw
        self._item_name = item_name or BACKUP_ITEM_PREFIX

    def export(self, manifest: dict, *, passphrase: Optional[str] = None) -> BackupEntry:
        content = encode_manifest_note(manifest)
        if len(content) > BW_NOTE_MAX_CHARS:
            raise BackupTooLargeForNote(
                f"This backup is {len(content)} characters — larger than the Bitwarden note "
                f"limit ({BW_NOTE_MAX_CHARS}). Export to a .spbk file instead.")
        item_id = self._bw.create_or_update_secure_note(self._item_name, content)
        if not item_id:
            raise BackupError("Bitwarden did not save the backup note (is the vault unlocked?)")
        return BackupEntry(id=item_id, name=self._item_name)

    def list_exports(self) -> List[BackupEntry]:
        entries: List[BackupEntry] = []
        for item in self._bw.list_secure_notes(BACKUP_ITEM_PREFIX):
            entries.append(BackupEntry(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                date=str(item.get("revisionDate", "") or ""),
            ))
        entries.sort(key=lambda e: e.date, reverse=True)
        return entries

    def read(self, entry: BackupEntry, *, passphrase: Optional[str] = None) -> dict:
        note = self._bw.read_secure_note(entry.id)
        if note is None:
            raise BackupError("Could not read the Bitwarden backup note.")
        return decode_manifest_note(note)
