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
import shlex
import tempfile
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


# --- SSH server (the user's own host) ----------------------------------------

DEFAULT_SSH_BACKUP_DIR = "~/sshpilot-backups"
# Timestamp inside app-generated backup filenames (sshpilot_backup_YYYYMMDD_HHMM.spbk).
_BACKUP_NAME_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})")


def _q(path: str) -> str:
    """Shell-quote a remote path while still letting the remote shell expand a leading ``~/``
    (``shlex.quote`` would neutralise the tilde). Everything after the tilde is quoted, so a
    user-typed path with spaces/metacharacters can't break out of the command."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


class SSHServerBackupBackend:
    """Store the ``.spbk`` archive as a file in a directory on one of the user's own SSH servers.

    Transport is the plain ssh exec channel (``cat``), not the SFTP subsystem, so it works
    anywhere ssh does. ``runner`` is a duck-typed object exposing
    ``run_command(cmd, *, input=None, timeout=…) -> (exit_code, stdout_bytes, stderr_text)`` —
    in the app this is an :class:`OpenSSHSFTPManager` (which rides the shared native-auth path);
    in tests it is a fake. Kept GTK-free like the other backends."""
    name = "ssh"

    def __init__(self, runner, remote_dir: str = DEFAULT_SSH_BACKUP_DIR, *, item_name: str = ""):
        self._run = runner
        self._dir = (remote_dir or DEFAULT_SSH_BACKUP_DIR).rstrip("/") or DEFAULT_SSH_BACKUP_DIR
        self._name = item_name or "sshpilot_backup.spbk"

    def _remote_path(self, name: str) -> str:
        """Logical (unquoted) remote path for ``name`` in the backup dir. Quote at command build."""
        return f"{self._dir}/{name}"

    def preflight(self, archive_size: int) -> None:
        """Ensure the remote dir exists and is writable and has room. Raises ``BackupError``.

        One round-trip: create the dir, confirm it's writable, and read free space. A launch
        failure (``exit_code == -1``) means we couldn't even reach the host over ssh."""
        qdir = _q(self._dir)
        # Only the create/write check gates preflight; df is best-effort ("|| true") so a
        # missing/broken df on the remote doesn't masquerade as a permission error.
        rc, out, err = self._run.run_command(
            f"mkdir -p {qdir} && test -w {qdir} && {{ df -Pk {qdir} | tail -1 || true; }}",
            timeout=60)
        if rc == -1:
            raise BackupError(f"Could not connect to the server: {err or 'ssh failed'}")
        if rc != 0:
            raise BackupError(
                f"Cannot create or write to {self._dir} on the server: "
                f"{(err or out.decode('utf-8', 'replace')).strip() or 'permission denied'}")
        avail_kb = _parse_df_avail_kb(out)
        if avail_kb is not None and avail_kb * 1024 < archive_size * 1.1:
            raise BackupError(
                f"Not enough free space on the server: need ~{_human(archive_size)}, "
                f"only {_human(avail_kb * 1024)} available in {self._dir}.")

    def export(self, manifest: dict, *, passphrase: Optional[str] = None) -> BackupEntry:
        from .backup_archive import write_spbk
        with tempfile.NamedTemporaryFile(suffix=".spbk", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            write_spbk(tmp_path, manifest, passphrase or None)
            with open(tmp_path, "rb") as fh:
                data = fh.read()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        self.preflight(len(data))
        remote = self._remote_path(self._name)
        qpart, qfinal = _q(remote + ".part"), _q(remote)
        # ponytail: whole archive is read into memory for the cat stdin upload — fine for typical
        # backups (config + secrets); switch to OpenSSHSFTPManager.upload() if large key sets matter.
        rc, out, err = self._run.run_command(
            f"cat > {qpart} && mv {qpart} {qfinal}", input=data, timeout=300)
        if rc != 0:
            # Best-effort: don't leave a half-written .part behind (it's excluded from listings).
            try:
                self._run.run_command(f"rm -f {qpart}", timeout=30)
            except Exception:
                pass
            raise BackupError(
                f"Failed to write the backup to the server: "
                f"{(err or out.decode('utf-8', 'replace')).strip() or 'unknown error'}")
        return BackupEntry(id=remote, name=self._name)

    def list_exports(self) -> List[BackupEntry]:
        qdir = _q(self._dir)
        rc, out, err = self._run.run_command(f"ls -1 {qdir}/*.spbk 2>/dev/null", timeout=60)
        # Distinguish an ssh-level failure (rc 255) or launch failure (rc -1) from a genuinely
        # empty/missing dir (rc 1/2) — otherwise a connect/auth error looks like "no backups".
        if rc in (-1, 255):
            raise BackupError(
                f"Could not connect to the server: {(err or '').strip() or 'ssh failed'}")
        if rc != 0:
            return []
        entries: List[BackupEntry] = []
        for line in out.decode("utf-8", "replace").splitlines():
            path = line.strip()
            if not path:
                continue
            base = path.rsplit("/", 1)[-1]
            m = _BACKUP_NAME_DATE_RE.search(base)
            date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
            entries.append(BackupEntry(id=path, name=base, date=date))
        entries.sort(key=lambda e: e.name, reverse=True)
        return entries

    def download(self, entry: BackupEntry, local_path: str) -> None:
        """Fetch the raw ``.spbk`` bytes to ``local_path`` (leaves any encryption intact so the
        existing import flow can prompt for the passphrase)."""
        rc, out, err = self._run.run_command(f"cat {_q(entry.id)}", timeout=300)
        if rc != 0:
            raise BackupError(
                f"Could not read the backup from the server: {err.strip() or 'unknown error'}")
        with open(local_path, "wb") as fh:
            fh.write(out)

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


def _parse_df_avail_kb(out: bytes) -> Optional[int]:
    """Available KB from a ``df -Pk … | tail -1`` line (POSIX field 4). None if unparseable."""
    try:
        fields = out.decode("utf-8", "replace").split()
        return int(fields[3])
    except (ValueError, IndexError):
        return None


def _human(num_bytes: float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
