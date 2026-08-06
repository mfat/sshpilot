"""Daemon-owned secret transfer (export / import).

Runs entirely inside the daemon process so no decrypted credential value ever
travels to a frontend: the manifest is built, encrypted, decrypted and applied
here, and callers receive only paths, counts and warnings.

Reuses the existing GTK-free building blocks instead of inventing new ones:

* :mod:`sshpilot.backup_archive` — the ``.spbk`` container and its
  scrypt + AES-256-GCM encryption.
* :mod:`sshpilot.credential_manager` — the normalized, eager credential
  enumeration used by the GUI export.
* :mod:`sshpilot.credential_model` — ``Credential`` / ``credential_to_spec``,
  the same save path normal credential writes use.
* :mod:`sshpilot.backup_backends` — the Bitwarden backup-note destination.
* :mod:`sshpilot.secret_storage` — the authoritative ``SecretManager``.

Restore is deliberately **non-destructive** exactly like the GUI path: a secret
already present in the selected backend is left untouched, and an existing
private-key file is never overwritten.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sshpilot.api.models.secrets import (
    SecretOperationState,
    SecretTransferResult,
)
from sshpilot.core.settings import CONFIG_VERSION

logger = logging.getLogger(__name__)

BACKUP_OPTION_KEYS = ("app_settings", "ssh_config", "known_hosts", "private_keys", "secrets")
DEFAULT_BACKUP_OPTIONS = {
    "app_settings": True,
    "ssh_config": True,
    "known_hosts": True,
    "secrets": True,
    "private_keys": False,
}


def _current_home() -> str:
    return str(Path.home())


def _rebase_home_path(path: str, source_home: Optional[str], target_home: str) -> str:
    """Rebase ``path`` written on the source machine's home onto this machine's home."""
    path = os.path.expanduser(str(path or ""))
    if not path:
        return path
    source_home = os.path.expanduser(source_home or "")
    if source_home and path.startswith(source_home + os.sep):
        return os.path.join(target_home, path[len(source_home) + len(os.sep):])
    return path


def normalize_backup_options(options: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    merged = dict(DEFAULT_BACKUP_OPTIONS)
    if options:
        for key in BACKUP_OPTION_KEYS:
            if key in options:
                merged[key] = bool(options[key])
    return merged


def _connection_key_paths(view: Any) -> List[str]:
    paths: List[str] = []
    kf = getattr(view, "keyfile", "") or ""
    if kf:
        paths.append(kf)
    for p in (getattr(view, "identity_files", None) or []):
        if p:
            paths.append(p)
    for p in (getattr(view, "resolved_identity_files", None) or []):
        if p and p not in paths:
            paths.append(p)
    return paths


def _gather_credentials(manager: Any, views: List[Any]) -> List[Dict[str, Any]]:
    """Serialized credentials (password/sudo/key passphrase) for the given views."""
    if not views:
        return []
    try:
        from sshpilot.credential_manager import CredentialManager

        creds = CredentialManager(list(views), secret_manager=manager).list_credentials(
            include_orphans=False
        )
    except Exception:
        logger.warning("Gathering credentials for backup failed", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for c in creds:
        if c.secret is None:
            continue
        out.append(
            {
                "id": c.id,
                "type": c.type,
                "host": c.host,
                "username": c.username,
                "secret": c.secret,
                "metadata": c.metadata,
            }
        )
    return out


def _gather_private_keys(views: List[Any]) -> List[Dict[str, Any]]:
    """Serialize the selected connections' private key files and matching ``.pub`` files."""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for view in views:
        for key_path in _connection_key_paths(view):
            key_path = os.path.expanduser(str(key_path))
            if not key_path or key_path in seen:
                continue
            seen.add(key_path)
            try:
                if not os.path.isfile(key_path):
                    logger.warning("Export: referenced key file not found: %s", key_path)
                    continue
                with open(key_path, "rb") as f:
                    private_raw = f.read()
                stat = os.stat(key_path)
                item: Dict[str, Any] = {
                    "path": key_path,
                    "mode": stat.st_mode & 0o777,
                    "content_b64": base64.b64encode(private_raw).decode("ascii"),
                }
                public_path = f"{key_path}.pub"
                if os.path.isfile(public_path):
                    with open(public_path, "rb") as f:
                        public_raw = f.read()
                    item["public_path"] = public_path
                    item["public_content_b64"] = base64.b64encode(public_raw).decode("ascii")
                    item["public_mode"] = os.stat(public_path).st_mode & 0o777
                out.append(item)
            except Exception:
                logger.warning("Failed to include private key in backup: %s", key_path,
                               exc_info=True)
    return out


def _build_manifest(
    manager: Any,
    views: List[Any],
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The ``.spbk`` manifest: metadata + credentials + private keys. No secret
    value leaves this module except inside the (optionally encrypted) archive."""
    options = normalize_backup_options(options)
    manifest: Dict[str, Any] = {
        "version": 1,
        "format": "spbk",
        "export_date": datetime.now().isoformat(),
        "platform": os.name,
        "config_version": CONFIG_VERSION,
        "backup_options": options,
        "source_home": _current_home(),
        "isolated_mode": False,
    }
    manifest["credentials"] = _gather_credentials(manager, views) if options["secrets"] else []
    manifest["private_keys"] = _gather_private_keys(views) if options["private_keys"] else []
    return manifest


def _mirror_credentials_to_backend(manifest: Dict[str, Any], backend: Any) -> int:
    """Copy the manifest's credentials into ``backend`` as normal login entries
    (updates existing) — the same ``credential_to_spec`` → ``store`` path the
    GUI "mirror secrets" option uses. Returns the number mirrored."""
    from sshpilot.credential_model import Credential, credential_to_spec

    mirrored = 0
    for c in manifest.get("credentials") or []:
        secret = c.get("secret")
        if secret is None:
            continue
        try:
            cred = Credential(
                id=c.get("id", ""),
                type=c.get("type", ""),
                host=c.get("host"),
                username=c.get("username"),
                secret=secret,
                metadata=dict(c.get("metadata") or {}),
            )
            if backend.store(credential_to_spec(cred), secret):
                mirrored += 1
        except Exception:
            logger.warning("Failed to mirror a credential to the target backend", exc_info=True)
    return mirrored


def _selected_views(
    manager: Any,
    connections_source: Optional[Any],
    connection_ids: Optional[List[str]] = None,
) -> List[Any]:
    """Resolve the connections to include in an export as headless views.

    ``connections_source`` is either a callable returning records or an
    iterable of records. When ``connection_ids`` is non-empty, only those
    connections are included (matched by record id or nickname).
    """
    records: Iterable[Any] = ()
    if callable(connections_source):
        try:
            records = list(connections_source())
        except Exception:
            logger.debug("listing connections for backup failed", exc_info=True)
            records = []
    elif connections_source is not None:
        try:
            records = list(connections_source)
        except Exception:
            records = []
    records = list(records)

    from sshpilot.daemon.connection_launch_provider import HeadlessConnectionView

    views = [HeadlessConnectionView(r) for r in records]
    if not connection_ids:
        return views
    wanted = set(str(c) for c in connection_ids)
    return [v for v in views if str(v.id) in wanted or str(v.nickname) in wanted]


def daemon_export_backup(
    manager: Any,
    *,
    destination: str,
    connection_ids: Optional[List[str]] = None,
    options: Optional[Dict[str, Any]] = None,
    mirror_logins: bool = False,
    connections_source: Optional[Any] = None,
    passphrase: Optional[str] = None,
) -> SecretTransferResult:
    """Export secret material to a ``.spbk`` file or a Bitwarden backup note.

    Returns counts (``credentials`` / ``private_keys``) and warnings; no secret
    value is ever returned.
    """
    views = _selected_views(manager, connections_source, connection_ids)
    options = normalize_backup_options(options)
    if not options["secrets"] and not options["private_keys"]:
        return SecretTransferResult(
            operation="export",
            path=destination,
            counts={"credentials": 0, "private_keys": 0},
            warnings=("Choose at least one item to include in the backup.",),
            status=SecretOperationState.FAILED,
            message="Nothing selected to export",
        )
    manifest = _build_manifest(manager, views, options)
    counts = {
        "credentials": len(manifest["credentials"] or []),
        "private_keys": len(manifest["private_keys"] or []),
    }
    warnings: List[str] = []

    if _is_bitwarden_destination(destination):
        backend = manager.get_backend("bitwarden")
        if backend is None or not _safe(lambda: backend.is_available()):
            return SecretTransferResult(
                operation="export",
                path=destination,
                counts=counts,
                warnings=warnings,
                status=SecretOperationState.FAILED,
                message="Bitwarden is unavailable for backup",
            )
        from sshpilot.backup_backends import BackupTooLargeForNote

        try:
            backend.export(manifest, passphrase=None)
        except BackupTooLargeForNote as exc:
            return SecretTransferResult(
                operation="export",
                path=destination,
                counts=counts,
                warnings=("The backup is too large for a Bitwarden note.",),
                status=SecretOperationState.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            logger.error("Bitwarden backup export failed", exc_info=True)
            return SecretTransferResult(
                operation="export",
                path=destination,
                counts=counts,
                warnings=warnings,
                status=SecretOperationState.FAILED,
                message=f"Bitwarden export failed: {exc}",
            )
        if mirror_logins and options["secrets"]:
            mirrored = _mirror_credentials_to_backend(manifest, manager.get_backend("bitwarden"))
            counts["mirrored"] = mirrored
        logger.info(
            "Backup exported to Bitwarden (%d credential(s), %d private key(s))",
            counts["credentials"],
            counts["private_keys"],
        )
        return SecretTransferResult(
            operation="export",
            path=destination,
            counts=counts,
            warnings=warnings,
            status=SecretOperationState.SUCCESS,
            message="",
        )

    try:
        from sshpilot.backup_archive import write_spbk

        write_spbk(os.path.expanduser(destination), manifest, passphrase or None)
    except Exception as exc:
        logger.error("Backup export failed: %s", exc)
        return SecretTransferResult(
            operation="export",
            path=destination,
            counts=counts,
            warnings=warnings,
            status=SecretOperationState.FAILED,
            message=f"Failed to export backup: {exc}",
        )
    logger.info(
        "Backup exported to %s (%d credential(s), %d private key(s), encrypted=%s)",
        destination,
        counts["credentials"],
        counts["private_keys"],
        bool(passphrase),
    )
    return SecretTransferResult(
        operation="export",
        path=destination,
        counts=counts,
        warnings=warnings,
        status=SecretOperationState.SUCCESS,
        message="",
    )


def daemon_import_backup(
    manager: Any,
    *,
    source: str,
    options: Optional[Dict[str, Any]] = None,
    passphrase: Optional[str] = None,
) -> SecretTransferResult:
    """Read a ``.spbk`` archive and restore its secrets into the selected backend.

    Returns counts (``restored`` / ``skipped`` / ``keys_written`` /
    ``keys_skipped``) and warnings. Restore is non-destructive.
    """
    from sshpilot.backup_archive import read_spbk

    source = os.path.expanduser(source)
    if not os.path.isfile(source):
        return SecretTransferResult(
            operation="import",
            path=source,
            counts={},
            warnings=(),
            status=SecretOperationState.FAILED,
            message=f"Backup file not found: {source}",
        )
    try:
        manifest = read_spbk(source, passphrase or None)
    except Exception as exc:
        logger.error("Backup import failed (wrong passphrase or corrupt archive): %s", exc)
        return SecretTransferResult(
            operation="import",
            path=source,
            counts={},
            warnings=("The archive could not be decrypted or read.",),
            status=SecretOperationState.FAILED,
            message="Wrong passphrase or corrupt backup",
        )
    if not isinstance(manifest, dict):
        return SecretTransferResult(
            operation="import",
            path=source,
            counts={},
            warnings=(),
            status=SecretOperationState.FAILED,
            message="The backup file is not a valid sshPilot backup",
        )

    counts: Dict[str, int] = {"restored": 0, "skipped": 0, "keys_written": 0, "keys_skipped": 0}
    warnings: List[str] = []

    if not _persists_secrets(manager):
        warnings.append(
            "The selected secret backend does not persist secrets (agent); "
            "no credentials were restored."
        )
    else:
        restored, skipped = _restore_credentials(manager, manifest)
        counts["restored"] = restored
        counts["skipped"] = skipped

    keys_written, keys_skipped = _restore_private_keys(manifest)
    counts["keys_written"] = keys_written
    counts["keys_skipped"] = keys_skipped

    logger.info(
        "Backup imported from %s (restored=%d skipped=%d keys_written=%d keys_skipped=%d)",
        source,
        restored,
        skipped,
        keys_written,
        keys_skipped,
    )
    return SecretTransferResult(
        operation="import",
        path=source,
        counts=counts,
        warnings=tuple(warnings),
        status=SecretOperationState.SUCCESS,
        message="",
    )


def _persists_secrets(manager: Any) -> bool:
    try:
        return bool(manager.persists_secrets())
    except Exception:
        return True


def _restore_credentials(manager: Any, manifest: Dict[str, Any]) -> tuple[int, int]:
    """Non-destructive re-store of the manifest's credentials. Returns
    ``(restored, skipped)``; an existing secret is never clobbered."""
    from sshpilot.credential_model import Credential, credential_to_spec

    creds = manifest.get("credentials") or []
    if not creds:
        return 0, 0
    restored = 0
    skipped = 0
    source_home = manifest.get("source_home")
    target_home = _current_home()
    for c in creds:
        try:
            secret = c.get("secret")
            if secret is None:
                continue
            cred = Credential(
                id=c.get("id", ""),
                type=c.get("type", ""),
                host=c.get("host"),
                username=c.get("username"),
                secret=secret,
                metadata=dict(c.get("metadata") or {}),
            )
            if cred.type == "key":
                old_path = cred.metadata.get("key_path") or cred.id
                new_path = _rebase_home_path(old_path, source_home, target_home)
                if new_path != old_path:
                    cred.metadata["key_path"] = new_path
                    cred.id = new_path
            spec = credential_to_spec(cred)
            if _secret_already_present(manager, spec):
                skipped += 1
                continue
            if manager.store(spec, secret):
                restored += 1
        except Exception:
            logger.warning("Failed to restore a credential", exc_info=True)
    return restored, skipped


def _secret_already_present(manager: Any, spec: Any) -> bool:
    lookup = getattr(manager, "lookup", None)
    if not callable(lookup):
        return False
    try:
        return lookup(spec) is not None
    except Exception:
        return False


def _restore_private_keys(manifest: Dict[str, Any]) -> tuple[int, int]:
    """Restore private keys to their (re-homed) paths. An existing file is NEVER
    overwritten. Returns ``(written, skipped_existing)``."""
    written = 0
    skipped = 0
    source_home = manifest.get("source_home")
    target_home = _current_home()
    for item in manifest.get("private_keys") or []:
        try:
            path = _rebase_home_path(
                os.path.expanduser(str(item.get("path") or "")), source_home, target_home
            )
            raw = item.get("content_b64")
            if not path or not raw:
                continue
            if os.path.exists(path):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(base64.b64decode(raw.encode("ascii")))
            private_mode = int(item.get("mode") or 0o600) & 0o777
            os.chmod(path, (private_mode & 0o600) or 0o600)

            public_path = _rebase_home_path(
                os.path.expanduser(str(item.get("public_path") or "")),
                source_home,
                target_home,
            )
            public_raw = item.get("public_content_b64")
            if public_path and public_raw and not os.path.exists(public_path):
                os.makedirs(os.path.dirname(public_path) or ".", exist_ok=True)
                with open(public_path, "wb") as f:
                    f.write(base64.b64decode(public_raw.encode("ascii")))
            written += 1
        except Exception:
            logger.warning("Failed to restore a private key", exc_info=True)
    return written, skipped


def _is_bitwarden_destination(destination: str) -> bool:
    return (destination or "").strip().lower() in {"bitwarden", "bw"}


def _safe(fn, default: Any = False):
    try:
        return fn()
    except Exception:
        return default
