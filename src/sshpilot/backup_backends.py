"""Pluggable backup/restore *destinations* for the manifest that :mod:`backup_manager` builds.

Three transports, same manifest dict:

- :class:`SpbkFileBackend` — the local ``.spbk`` file (existing behaviour), via
  :func:`backup_archive.write_spbk` / :func:`backup_archive.read_spbk`.
- :class:`BitwardenBackupBackend` — a single Bitwarden **secure note** holding
  ``SSHPILOT-BACKUP-v1\\n<base64(gzip(json(manifest)))>``. No attachments (premium/Flatpak
  issues) and no extra passphrase envelope — the note relies on Bitwarden's own vault encryption.
  If the encoded note would exceed the field limit, :class:`BackupTooLargeForNote` is raised so
  the caller can fall back to a ``.spbk`` file.
- :class:`SSHServerBackupBackend` — a ``.spbk`` file in a directory on one of the user's own SSH
  servers, transferred over the plain ssh exec channel (``cat``) via a duck-typed ``runner``.

GTK-free. The Bitwarden backend depends only on a small duck-typed object exposing
``create_or_update_secure_note`` / ``list_secure_notes`` / ``read_secure_note`` (implemented by
``secret_storage.BitwardenBackend``), so it is trivially fakeable in tests.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import List, Mapping, Optional, Protocol

from .api.models.secrets import (
    SecretTransferMessage,
    SecretTransferMessageCode,
)

BACKUP_NOTE_MAGIC = "SSHPILOT-BACKUP-v1"
BACKUP_ITEM_PREFIX = "sshPilot Backup"
# Bitwarden's note field caps around 10k characters (Vaultwarden may allow more). We refuse
# rather than silently truncate.
BW_NOTE_MAX_CHARS = 10_000


class BackupError(Exception):
    """A backup destination could not complete the operation."""

    def __init__(
        self,
        code: SecretTransferMessageCode,
        *,
        parameters: Optional[Mapping[str, object]] = None,
        diagnostic: str = "",
    ) -> None:
        self.transfer_message = SecretTransferMessage(
            code=code,
            parameters=dict(parameters or {}),
            diagnostic=diagnostic,
        )
        super().__init__(code.value)

    def __str__(self) -> str:
        code = self.transfer_message.code.value
        diagnostic = self.transfer_message.diagnostic
        return f"{code}: {diagnostic}" if diagnostic else code


class BackupTooLargeForNote(BackupError):
    """The encoded manifest exceeds the Bitwarden note field limit — use a ``.spbk`` file."""

    def __init__(
        self,
        *,
        length: int,
        limit: int,
        largest_section: str = "",
        largest_section_cost: int = 0,
    ) -> None:
        super().__init__(
            SecretTransferMessageCode.BITWARDEN_NOTE_TOO_LARGE,
            parameters={"length": length, "limit": limit},
        )
        self.largest_section = largest_section
        self.largest_section_cost = largest_section_cost


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


# What a reader recognizes as one part of a backup, and the manifest keys it
# spans. Used only to explain a refused note.
_NOTE_SECTIONS = (
    ("app_settings", ("app_config",)),
    ("ssh_config", ("ssh_config", "ssh_config_files")),
    ("known_hosts", ("known_hosts",)),
    ("credentials", ("credentials",)),
    ("private_keys", ("private_keys",)),
)


def largest_note_section(manifest: dict) -> tuple:
    """``(label, characters)`` for the part of *manifest* that costs the encoded
    note the most, or ``("", 0)`` when nothing stands out.

    Measured by re-encoding without each section rather than by raw size: the
    note is gzipped, so repetitive settings text can be a fraction of its raw
    size while high-entropy key material costs close to its own. Only run when
    a note is about to be refused, so the extra passes never matter.
    """
    full = len(encode_manifest_note(manifest))
    label, cost = "", 0
    for name, keys in _NOTE_SECTIONS:
        if not any(manifest.get(key) for key in keys):
            continue
        without = {k: v for k, v in manifest.items() if k not in keys}
        saved = full - len(encode_manifest_note(without))
        if saved > cost:
            label, cost = name, saved
    return label, cost


def decode_manifest_note(note: str) -> dict:
    parts = (note or "").split("\n", 1)
    if len(parts) != 2 or parts[0].strip() != BACKUP_NOTE_MAGIC:
        raise BackupError(
            SecretTransferMessageCode.BITWARDEN_BACKUP_READ_FAILED,
            diagnostic="not an sshPilot backup note",
        )
    try:
        raw = gzip.decompress(base64.b64decode(parts[1].strip().encode("ascii")))
        return json.loads(raw.decode("utf-8"))
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(
            SecretTransferMessageCode.BITWARDEN_BACKUP_READ_FAILED,
            diagnostic=f"corrupt backup note: {exc}",
        ) from exc


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
            # Name what is actually big. Guessing wastes the user's time: which
            # part dominates depends entirely on the vault — app settings can
            # outweigh private keys several times over.
            label, cost = largest_note_section(manifest)
            raise BackupTooLargeForNote(
                length=len(content),
                limit=BW_NOTE_MAX_CHARS,
                largest_section=label,
                largest_section_cost=cost,
            )
        item_id = self._bw.create_or_update_secure_note(self._item_name, content)
        if not item_id:
            raise BackupError(SecretTransferMessageCode.BITWARDEN_NOTE_SAVE_FAILED)
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
            raise BackupError(SecretTransferMessageCode.BITWARDEN_BACKUP_READ_FAILED)
        return decode_manifest_note(note)


# --- SSH server (the user's own host) ----------------------------------------

DEFAULT_SSH_BACKUP_DIR = "~/sshpilot-backups"
# Timestamp inside app-generated backup filenames (sshpilot_backup_YYYYMMDD_HHMM.spbk).
_BACKUP_NAME_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})")


class RemoteBackupStore(Protocol):
    """The remote-filesystem surface a backup destination needs.

    Implementations live in the daemon (:mod:`sshpilot.daemon.backup_transport`):
    one over the file manager's SFTP stack, one over the same one-shot command
    service Host Info uses. Every method raises :class:`BackupError` carrying the
    caller-facing message code, so this module never spawns ``ssh`` itself and
    stays GTK-free and transport-agnostic.
    """

    def ensure_directory(self, path: str) -> None:
        """Create *path* if absent and confirm it is writable."""

    def free_space_bytes(self, path: str) -> Optional[int]:
        """Free bytes on the filesystem holding *path*, or ``None`` if unknown."""

    def list_backups(self, path: str) -> List[BackupEntry]:
        """Every ``*.spbk`` directly in *path* (``.part`` uploads excluded)."""

    def upload(self, local_path: str, remote_path: str) -> None:
        """Copy a local file to *remote_path*, atomically and leaving no partial
        file behind on failure."""

    def download(self, remote_path: str, local_path: str) -> None:
        """Copy *remote_path* to a local file, byte for byte."""


def backup_entry_for(remote_path: str) -> BackupEntry:
    """A listing entry for one remote ``.spbk``, dated from its filename.

    Shared by every store so a backup is labelled identically however it was
    listed; app-generated names carry ``…_YYYYMMDD_HHMM.spbk``.
    """
    base = remote_path.rsplit("/", 1)[-1]
    match = _BACKUP_NAME_DATE_RE.search(base)
    date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else ""
    return BackupEntry(id=remote_path, name=base, date=date)


class SSHServerBackupBackend:
    """Store the ``.spbk`` archive as a file in a directory on one of the user's own SSH servers.

    Transport is delegated entirely to *store* (a :class:`RemoteBackupStore`) —
    in the daemon that is the file manager's SFTP stack, falling back to the
    one-shot command service Host Info uses for hosts with no SFTP subsystem;
    in tests it is a fake. This class owns only the backup *policy*: where the
    archive lives, how much room it needs, and what the listing looks like.
    """
    name = "ssh"

    def __init__(self, store: RemoteBackupStore, remote_dir: str = DEFAULT_SSH_BACKUP_DIR,
                 *, item_name: str = ""):
        self._store = store
        self._dir = (remote_dir or DEFAULT_SSH_BACKUP_DIR).rstrip("/") or DEFAULT_SSH_BACKUP_DIR
        self._name = item_name or "sshpilot_backup.spbk"

    def _remote_path(self, name: str) -> str:
        """Logical remote path for ``name`` in the backup dir."""
        return f"{self._dir}/{name}"

    def preflight(self, archive_size: int) -> None:
        """Ensure the remote dir exists, is writable, and has room. Raises ``BackupError``."""
        self._store.ensure_directory(self._dir)
        available = self._store.free_space_bytes(self._dir)
        if available is not None and available < archive_size * 1.1:
            raise BackupError(
                SecretTransferMessageCode.SSH_SERVER_FREE_SPACE_INSUFFICIENT,
                parameters={
                    "required": _human(archive_size),
                    "available": _human(available),
                    "directory": self._dir,
                },
            )

    def export(self, manifest: dict, *, passphrase: Optional[str] = None) -> BackupEntry:
        from .backup_archive import write_spbk
        with tempfile.NamedTemporaryFile(suffix=".spbk", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            write_spbk(tmp_path, manifest, passphrase or None)
            # The archive is streamed from this file rather than read into
            # memory, so a large key set costs no more than its size on disk.
            self.preflight(os.path.getsize(tmp_path))
            remote = self._remote_path(self._name)
            self._store.upload(tmp_path, remote)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return BackupEntry(id=remote, name=self._name)

    def list_exports(self) -> List[BackupEntry]:
        entries = list(self._store.list_backups(self._dir))
        entries.sort(key=lambda e: e.name, reverse=True)
        return entries

    def download(self, entry: BackupEntry, local_path: str) -> None:
        """Fetch the raw ``.spbk`` bytes to ``local_path`` (leaves any encryption intact so the
        existing import flow can prompt for the passphrase)."""
        self._store.download(entry.id, local_path)

    def read(self, entry: BackupEntry, *, passphrase: Optional[str] = None) -> dict:
        from .backup_archive import read_spbk
        with tempfile.NamedTemporaryFile(suffix=".spbk", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            self.download(entry, tmp_path)
            return read_spbk(tmp_path, passphrase or None)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _human(num_bytes: float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
