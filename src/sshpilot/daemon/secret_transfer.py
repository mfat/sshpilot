"""Daemon-owned secret transfer (export / import).

Runs entirely inside the daemon process so no decrypted credential value ever
travels to a frontend: the manifest is built, encrypted, decrypted and applied
here, and callers receive only paths, counts and warnings.

This module is deliberately a **thin execution adapter over the existing
``BackupManager``** — it does not maintain a parallel backup implementation.
Manifest construction (app settings, SSH config tree, known_hosts, credentials,
private keys), backup-option interpretation, credential/private-key restoration,
path rebasing, merge/non-destructive import and destination handling are all
``BackupManager`` (plus ``backup_archive`` / ``backup_backends``) behaviors:

* :mod:`sshpilot.backup_manager` — the authoritative backup/restore engine,
  driven here through a small GTK-free ``Config``-compatible shim;
* :mod:`sshpilot.backup_archive` — the ``.spbk`` container and its
  scrypt + AES-256-GCM encryption;
* :mod:`sshpilot.backup_backends` — the Bitwarden backup-note destination;
* :mod:`sshpilot.credential_manager` / :mod:`sshpilot.credential_model` — the
  normalized, eager credential enumeration and the ``credential_to_spec`` save
  path used by every normal credential write;
* :mod:`sshpilot.secret_storage` — the authoritative ``SecretManager``
  (the daemon shares the process-wide singleton).

Restore is **non-destructive exactly like the GUI path** (``merge``): a secret
already present in the selected backend is left untouched, an existing
private-key file is never overwritten, SSH hosts are merged into an Include
fragment, and app settings keep this machine's local values.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sshpilot.api.models.secrets import (
    SecretOperationState,
    SecretTransferMessage,
    SecretTransferMessageCode,
    SecretTransferPreview,
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


def _message(
    code: SecretTransferMessageCode,
    *,
    parameters: Optional[Dict[str, object]] = None,
    diagnostic: str = "",
) -> SecretTransferMessage:
    return SecretTransferMessage(
        code=code,
        parameters=dict(parameters or {}),
        diagnostic=diagnostic,
    )


def normalize_backup_options(options: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    """Complete boolean option map — same semantics as ``BackupManager``.

    ``encrypted`` is a transfer-only flag (not a stored category): the caller
    uses it to decide whether the daemon collects a passphrase.
    """
    merged = dict(DEFAULT_BACKUP_OPTIONS)
    if options:
        for key in BACKUP_OPTION_KEYS:
            if key in options:
                merged[key] = bool(options[key])
    return merged


class _HeadlessBackupConfig:
    """GTK-free ``Config``-compatible shim over the daemon's settings file.

    ``BackupManager`` reads app settings (``get_setting``), the raw
    ``config_data`` and a few path helpers through its ``config``; this shim
    satisfies that surface from the same ``config.json`` the daemon owns,
    without importing the GObject-facing ``Config`` adapter.
    """

    def __init__(self, settings_path: Path | str) -> None:
        self._path = Path(settings_path)
        self._data: Optional[Dict[str, Any]] = None

    def _load(self) -> Dict[str, Any]:
        if self._data is not None:
            return self._data
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._data = data if isinstance(data, dict) else {}
        except Exception:
            self._data = {}
        return self._data

    def _invalidate(self) -> None:
        """Drop the cached copy so the next read reflects on-disk changes.

        ``BackupManager`` mutates ``config_data`` in place after a restore
    (``self.config.reload_json_cache_strict()``); the
        shim mirrors that contract by re-reading the settings file.
        """
        self._data = None

    def _nested(self, dotted: str, default: Any) -> Any:
        cur: Any = self._load()
        for part in (dotted or "").split("."):
            if not part:
                return default
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def get_setting(self, key: str, default: Any = None) -> Any:
        try:
            return self._nested(key, default)
        except Exception:
            return default

    def load_json_config(self) -> Dict[str, Any]:
        return self._load()

    def get_default_config(self) -> Dict[str, Any]:
        return {"config_version": CONFIG_VERSION}

    @property
    def config_data(self) -> Dict[str, Any]:
        return self._load()

    @config_data.setter
    def config_data(self, value: Dict[str, Any]) -> None:
        self._data = dict(value) if isinstance(value, dict) else {}

    def get_ssh_config(self) -> Optional[str]:
        return None


class _CallableConnectionStore:
    """Adapts two narrow callables to ``BackupManager``'s ``connection_store``
    surface (``snapshot_for_backup``/``restore_connection_store``).

    Keeps this module holding only bound methods off the real
    ``ConnectionRepository`` — never the repository object itself — mirroring
    the existing ``connections_source=repository.list_records`` convention.
    """

    def __init__(self, snapshot_fn: Optional[Any], restore_fn: Optional[Any]) -> None:
        self._snapshot_fn = snapshot_fn
        self._restore_fn = restore_fn

    def snapshot_for_backup(self) -> Dict[str, Any]:
        return self._snapshot_fn() if self._snapshot_fn is not None else {}

    def restore_connection_store(self, section: Dict[str, Any], *, mode: str = "merge"):
        if self._restore_fn is None:
            from sshpilot.core.connections.repository import ConnectionStoreRestoreResult

            return ConnectionStoreRestoreResult()
        return self._restore_fn(section, mode=mode)


def _backup_manager(
    settings_path: Path | str,
    *,
    connection_store_snapshot: Optional[Any] = None,
    connection_store_restore: Optional[Any] = None,
):
    """One ``BackupManager`` driven by a headless config shim."""
    from sshpilot.backup_manager import BackupManager

    connection_store = None
    if connection_store_snapshot is not None or connection_store_restore is not None:
        connection_store = _CallableConnectionStore(
            connection_store_snapshot, connection_store_restore
        )
    return BackupManager(
        _HeadlessBackupConfig(settings_path), connection_store=connection_store
    )


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


def _selected_views(
    manager: Any,
    connections_source: Optional[Any],
    connection_ids: Optional[List[str]] = None,
) -> List[Any]:
    """Resolve the connections to include in an export as headless views.

    ``connections_source`` is either a callable returning records or an
    iterable of records (the production composition passes
    ``repository.list_records``). When ``connection_ids`` is non-empty, only
    those connections are included (matched by record id or nickname).
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
    settings_path: Optional[Path | str] = None,
    connection_store_snapshot: Optional[Any] = None,
) -> SecretTransferResult:
    """Export the full backup (settings, SSH config, known_hosts, secrets,
    private keys) to a ``.spbk`` file or a Bitwarden backup note.

    Returns counts (``credentials`` / ``private_keys``) and warnings; no secret
    value is ever returned.
    """
    views = _selected_views(manager, connections_source, connection_ids)
    options = normalize_backup_options(options)
    if not any(options.values()):
        return SecretTransferResult(
            operation="export",
            path=destination,
            counts={"credentials": 0, "private_keys": 0},
            warnings=(_message(SecretTransferMessageCode.BACKUP_ITEMS_REQUIRED),),
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.NOTHING_SELECTED_TO_EXPORT
            ),
        )

    mgr = _backup_manager(
        settings_path or _settings_path(),
        connection_store_snapshot=connection_store_snapshot,
    )
    warnings: List[SecretTransferMessage] = []

    ssh_dest = _ssh_server_destination(destination)
    if ssh_dest is not None:
        return _daemon_export_to_ssh(
            manager, ssh_dest, views, options, passphrase=passphrase,
            connections_source=connections_source, settings_path=settings_path,
            connection_store_snapshot=connection_store_snapshot,
        )

    if _is_bitwarden_destination(destination):
        backend = manager.get_backend("bitwarden")
        if backend is None or not _safe(lambda: backend.is_available()):
            return SecretTransferResult(
                operation="export",
                path=destination,
                counts={},
                warnings=warnings,
                status=SecretOperationState.FAILED,
                message=_message(
                    SecretTransferMessageCode.BITWARDEN_BACKUP_UNAVAILABLE
                ),
            )
        from sshpilot.backup_backends import (
            BackupError,
            BackupTooLargeForNote,
            BitwardenBackupBackend,
        )

        name = "sshPilot Backup {}".format(datetime.now().strftime("%Y-%m-%d %H:%M"))
        try:
            mgr.export_to_backend(
                BitwardenBackupBackend(backend, item_name=name),
                connections=views,
                options=options,
                mirror_to=(backend if mirror_logins else None),
            )
        except BackupTooLargeForNote as exc:
            counts = dict(getattr(mgr, "last_export_counts", {}) or {})
            # Every other failure here is logged; this one was returned to the
            # UI and logged nowhere, so a user who read "Export failed" and went
            # looking for the reason found no trace of the export at all.
            logger.warning(
                "Bitwarden backup export refused: %s "
                "(%d credential(s), %d private key(s))",
                exc,
                counts.get("credentials", 0),
                counts.get("private_keys", 0),
            )
            # The message names the part that dominates this particular backup
            # (see ``largest_note_section``), so the warning stays generic.
            too_large = []
            section = getattr(exc, "largest_section", "")
            cost = getattr(exc, "largest_section_cost", 0)
            if section and cost:
                too_large.append(
                    _message(
                        SecretTransferMessageCode.BITWARDEN_NOTE_LARGEST_SECTION,
                        parameters={"section": section, "cost": cost},
                    )
                )
            too_large.extend(
                (
                    _message(SecretTransferMessageCode.EXPORT_SPBK_INSTEAD),
                    _message(
                        SecretTransferMessageCode.BITWARDEN_BACKUP_TOO_LARGE
                    ),
                    _message(SecretTransferMessageCode.BITWARDEN_NOTE_REDUCE),
                )
            )
            return SecretTransferResult(
                operation="export",
                path=destination,
                counts=counts,
                warnings=tuple(too_large),
                status=SecretOperationState.FAILED,
                message=exc.transfer_message,
            )
        except BackupError as exc:
            logger.error("Bitwarden backup export failed: %s", exc)
            return SecretTransferResult(
                operation="export",
                path=destination,
                counts={},
                warnings=warnings,
                status=SecretOperationState.FAILED,
                message=exc.transfer_message,
            )
        except Exception as exc:
            logger.error("Bitwarden backup export failed", exc_info=True)
            return SecretTransferResult(
                operation="export",
                path=destination,
                counts={},
                warnings=warnings,
                status=SecretOperationState.FAILED,
                message=_message(
                    SecretTransferMessageCode.BITWARDEN_EXPORT_FAILED,
                    diagnostic=str(exc),
                ),
            )
        counts = dict(getattr(mgr, "last_export_counts", {}) or {})
        mirror = getattr(mgr, "last_mirror_counts", None)
        if mirror:
            counts["mirrored"] = int(mirror.get("mirrored", 0))
        logger.info(
            "Backup exported to Bitwarden (%d credential(s), %d private key(s))",
            counts.get("credentials", 0),
            counts.get("private_keys", 0),
        )
        return SecretTransferResult(
            operation="export",
            path=destination,
            counts=counts,
            warnings=warnings,
            status=SecretOperationState.SUCCESS,
            message=None,
        )

    try:
        ok, error = mgr.export_backup(
            os.path.expanduser(destination),
            connections=views,
            passphrase=passphrase,
            options=options,
        )
    except Exception as exc:
        logger.error("Backup export failed: %s", exc)
        return SecretTransferResult(
            operation="export",
            path=destination,
            counts={},
            warnings=warnings,
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.BACKUP_EXPORT_FAILED,
                diagnostic=str(exc),
            ),
        )
    counts = dict(getattr(mgr, "last_export_counts", {}) or {})
    skipped = getattr(mgr, "last_export_skipped_config_files", None) or []
    if skipped:
        warnings.append(
            _message(
                SecretTransferMessageCode.SSH_CONFIG_FILES_SKIPPED,
                parameters={"count": len(skipped), "paths": ", ".join(skipped)},
            )
        )
    missing_keys = getattr(mgr, "last_export_missing_key_files", None) or []
    if missing_keys:
        warnings.append(
            _message(
                SecretTransferMessageCode.REFERENCED_KEY_FILES_MISSING,
                parameters={
                    "count": len(missing_keys),
                    "paths": ", ".join(missing_keys),
                },
            )
        )
    logger.info(
        "Backup exported to %s (%d credential(s), %d private key(s), encrypted=%s)",
        destination,
        counts.get("credentials", 0),
        counts.get("private_keys", 0),
        bool(passphrase),
    )
    return SecretTransferResult(
        operation="export",
        path=destination,
        counts=counts,
        warnings=tuple(warnings),
        status=(
            SecretOperationState.SUCCESS if ok else SecretOperationState.FAILED
        ),
        message=(
            getattr(mgr, "last_transfer_message", None)
            if not ok
            else None
        ) or (
            _message(
                SecretTransferMessageCode.BACKUP_EXPORT_FAILED,
                diagnostic=error or "",
            )
            if not ok
            else None
        ),
    )


def _daemon_export_to_ssh(
    manager: Any,
    ssh_dest: Tuple[str, str],
    views: List[Any],
    options: Dict[str, bool],
    *,
    passphrase: Optional[str] = None,
    connections_source: Any = None,
    settings_path: Optional[Path | str] = None,
    connection_store_snapshot: Optional[Any] = None,
) -> SecretTransferResult:
    """Export the backup to a ``.spbk`` file on one of the user's SSH servers.

    The manifest is built and encrypted inside the daemon; only the archive is
    uploaded over ssh. The frontend never receives the manifest.
    """
    connection_id, remote_dir = ssh_dest
    from sshpilot.backup_backends import BackupError, SSHServerBackupBackend

    runner = _DaemonSshRunner(
        manager, connections_source, connection_id, settings_path or _settings_path()
    )
    name = "sshpilot_backup_{}.spbk".format(
        datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    try:
        mgr = _backup_manager(
            settings_path or _settings_path(),
            connection_store_snapshot=connection_store_snapshot,
        )
        mgr.export_to_backend(
            SSHServerBackupBackend(runner, remote_dir, item_name=name),
            connections=views,
            passphrase=passphrase,
            options=options,
        )
    except BackupError as exc:
        logger.error("SSH server backup export failed: %s", exc)
        return SecretTransferResult(
            operation="export", path="ssh", counts={}, warnings=(),
            status=SecretOperationState.FAILED, message=exc.transfer_message,
        )
    except Exception as exc:
        logger.error("SSH server backup export failed: %s", exc)
        return SecretTransferResult(
            operation="export", path="ssh", counts={}, warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.SSH_BACKUP_EXPORT_FAILED,
                diagnostic=str(exc),
            ),
        )
    counts = dict(getattr(mgr, "last_export_counts", {}) or {})
    logger.info(
        "Backup exported to SSH server %s (%d credential(s), %d private key(s))",
        connection_id, counts.get("credentials", 0), counts.get("private_keys", 0),
    )
    return SecretTransferResult(
        operation="export", path="ssh", counts=counts, warnings=(),
        status=SecretOperationState.SUCCESS, message=None,
    )


def daemon_import_backup(
    manager: Any,
    *,
    source: str,
    options: Optional[Dict[str, Any]] = None,
    passphrase: Optional[str] = None,
    settings_path: Optional[Path | str] = None,
    manifest: Optional[Dict[str, Any]] = None,
    connection_store_restore: Optional[Any] = None,
) -> SecretTransferResult:
    """Import a ``.spbk`` archive or a legacy JSON config, and restore it.

    The daemon owns the manifest: a ``.spbk`` is decrypted here (and the caller
    supplies the passphrase, which the service collects through a protected
    interaction), while a legacy JSON config is imported without secrets. The
    ``mode`` option (``"replace"`` / ``"merge"``) selects the config-apply
    behaviour; merge is the non-destructive default.

    Returns counts (``restored`` / ``skipped`` / ``keys_written`` /
    ``keys_skipped``) and warnings. No decrypted value is ever returned.
    """
    source = os.path.expanduser(source)
    if not os.path.isfile(source):
        return SecretTransferResult(
            operation="import",
            path=source,
            counts={},
            warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.BACKUP_FILE_NOT_FOUND,
                parameters={"source": source},
            ),
        )
    mode = str((options or {}).get("mode") or "merge")
    if mode not in ("replace", "merge"):
        mode = "merge"

    from sshpilot.backup_archive import is_spbk

    if not is_spbk(source):
        # Legacy JSON config import — no secrets involved.
        mgr = _backup_manager(
            settings_path or _settings_path(),
            connection_store_restore=connection_store_restore,
        )
        try:
            success, error = mgr.import_configuration(
                source, mode=mode, create_backup=True
            )
        except Exception as exc:
            logger.error("Config import failed: %s", exc)
            return SecretTransferResult(
                operation="import", path=source, counts={}, warnings=(),
                status=SecretOperationState.FAILED,
                message=_message(
                    SecretTransferMessageCode.CONFIGURATION_IMPORT_FAILED,
                    diagnostic=str(exc),
                ),
            )
        if not success:
            return SecretTransferResult(
                operation="import", path=source, counts={}, warnings=(),
                status=SecretOperationState.FAILED,
                message=(
                    getattr(mgr, "last_transfer_message", None)
                    or _message(
                        SecretTransferMessageCode.CONFIGURATION_IMPORT_FAILED_GENERIC,
                        diagnostic=error or "",
                    )
                ),
            )
        ignored = int(getattr(mgr, "last_import_ignored_secrets", 0) or 0)
        counts = {"restored": 1, "ignored_secrets": ignored}
        cs_warnings = _connection_store_warnings(mgr)
        return SecretTransferResult(
            operation="import", path=source, counts=counts, warnings=cs_warnings,
            status=SecretOperationState.SUCCESS, message=None,
        )

    if manifest is None:
        manifest = _read_manifest(source, passphrase)
        if manifest is None:
            return SecretTransferResult(
                operation="import",
                path=source,
                counts={},
                warnings=(
                    _message(
                        SecretTransferMessageCode.ARCHIVE_DECRYPT_OR_READ_FAILED
                    ),
                ),
                status=SecretOperationState.FAILED,
                message=_message(
                    SecretTransferMessageCode.WRONG_PASSPHRASE_OR_CORRUPT_BACKUP
                ),
            )

    mgr = _backup_manager(
        settings_path or _settings_path(),
        connection_store_restore=connection_store_restore,
    )
    try:
        success, error, restored, keys_written = mgr.apply_imported_manifest(
            manifest,
            mode=mode,
            create_backup=True,
            restore_options=options,
        )
    except Exception as exc:
        logger.error("Backup import failed: %s", exc)
        return SecretTransferResult(
            operation="import",
            path=source,
            counts={},
            warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.BACKUP_IMPORT_FAILED,
                diagnostic=str(exc),
            ),
        )
    counts = {
        "restored": restored,
        "skipped": int(getattr(mgr, "last_import_skipped_credentials", 0) or 0),
        "keys_written": keys_written,
        "keys_skipped": int(getattr(mgr, "last_import_skipped_keys", 0) or 0),
    }
    warnings = list(_connection_store_warnings(mgr))
    if not success:
        return SecretTransferResult(
            operation="import",
            path=source,
            counts=counts,
            warnings=(),
            status=SecretOperationState.FAILED,
            message=(
                getattr(mgr, "last_transfer_message", None)
                or _message(
                    SecretTransferMessageCode.BACKUP_IMPORT_FAILED_GENERIC,
                    diagnostic=error or "",
                )
            ),
        )
    if not _persists_secrets(manager):
        warnings.append(_message(SecretTransferMessageCode.SECRETS_NOT_PERSISTED))
    logger.info(
        "Backup imported from %s (restored=%d skipped=%d keys_written=%d keys_skipped=%d)",
        source,
        restored,
        counts["skipped"],
        keys_written,
        counts["keys_skipped"],
    )
    return SecretTransferResult(
        operation="import",
        path=source,
        counts=counts,
        warnings=tuple(warnings),
        status=SecretOperationState.SUCCESS,
        message=None,
    )


def _included_categories(
    settings_path: Path | str,
    manifest: Dict[str, Any],
) -> Dict[str, bool]:
    """Which backup categories a manifest actually contains (metadata only).

    Mirrors the GUI mode-dialog logic (``BackupManager._restore_options_for_manifest``
    with every category requested): absent categories read ``False`` and the mode
    dialog disables them. No credential value is ever returned.
    """
    from sshpilot.backup_manager import BACKUP_OPTION_KEYS

    mgr = _backup_manager(settings_path)
    all_requested = {key: True for key in BACKUP_OPTION_KEYS}
    try:
        return mgr._restore_options_for_manifest(manifest, restore_options=all_requested)
    except Exception:
        logger.debug("restore-option preview failed", exc_info=True)
        return {}


def _connection_store_warnings(manager: Any) -> Tuple[SecretTransferMessage, ...]:
    warnings = tuple(
        getattr(manager, "last_connection_store_warnings", ()) or ()
    )
    if not all(type(warning) is SecretTransferMessage for warning in warnings):
        raise TypeError("connection-store warnings must be structured messages")
    return warnings


def _read_manifest(
    source: str,
    passphrase: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Decrypt/read a ``.spbk`` manifest; ``None`` on wrong passphrase or corrupt data."""
    from sshpilot.backup_archive import read_spbk

    try:
        manifest = read_spbk(source, passphrase or None)
    except Exception as exc:
        logger.debug("backup manifest read failed: %s", exc)
        return None
    return manifest if isinstance(manifest, dict) else None


def daemon_preview_backup(
    manager: Any,
    *,
    source: str,
    passphrase: Optional[str] = None,
    settings_path: Optional[Path | str] = None,
) -> Tuple[SecretTransferPreview, Optional[Dict[str, Any]]]:
    """Inspect a backup file without exposing its contents.

    Returns ``(public, manifest)``: ``public`` carries only ``kind``
    (``"spbk"`` / ``"json"`` / ``"unknown"``), ``encrypted``, and the
    ``included`` category map; ``manifest`` is the decrypted payload handed to
    the daemon's manifest cache so the subsequent import never re-prompts for
    the passphrase. The frontend only ever sees ``public``.
    """
    source = os.path.expanduser(source)
    if not os.path.isfile(source):
        return (
            SecretTransferPreview(
                kind="unknown",
                error=_message(
                    SecretTransferMessageCode.BACKUP_FILE_NOT_FOUND,
                    parameters={"source": source},
                ),
            ),
            None,
        )
    from sshpilot.backup_archive import is_spbk, spbk_is_encrypted

    if not is_spbk(source):
        # Legacy JSON config — no secret-bearing manifest to preview.
        return SecretTransferPreview(kind="json"), None
    try:
        encrypted = bool(spbk_is_encrypted(source))
    except Exception:
        encrypted = False
    if encrypted and not passphrase:
        return SecretTransferPreview(kind="spbk", encrypted=True), None
    manifest = _read_manifest(source, passphrase)
    if manifest is None:
        return (
            SecretTransferPreview(
                kind="spbk",
                encrypted=encrypted,
                error=_message(
                    SecretTransferMessageCode.WRONG_PASSPHRASE_OR_CORRUPT_BACKUP
                ),
            ),
            None,
        )
    settings_path = settings_path or _settings_path()
    return (
        SecretTransferPreview(
            kind="spbk",
            encrypted=encrypted,
            included=_included_categories(settings_path, manifest),
        ),
        manifest,
    )


def daemon_preview_bitwarden_backup(
    manager: Any,
    *,
    entry_id: str,
    settings_path: Optional[Path | str] = None,
) -> Tuple[SecretTransferPreview, Optional[Dict[str, Any]]]:
    """Preview one Bitwarden backup note: included categories (metadata only)."""
    from sshpilot.backup_backends import BitwardenBackupBackend

    backend = manager.get_backend("bitwarden")
    if backend is None or not _safe(lambda: backend.is_available()):
        return (
            SecretTransferPreview(
                kind="bitwarden",
                error=_message(
                    SecretTransferMessageCode.BITWARDEN_BACKUP_UNAVAILABLE
                ),
            ),
            None,
        )
    bw_backend = BitwardenBackupBackend(backend)
    try:
        entries = bw_backend.list_exports()
    except Exception as exc:
        logger.error("Bitwarden backup listing failed: %s", exc)
        return (
            SecretTransferPreview(
                kind="bitwarden",
                error=_message(
                    SecretTransferMessageCode.BITWARDEN_BACKUP_LIST_FAILED
                ),
            ),
            None,
        )
    entry = next((e for e in entries if str(getattr(e, "id", "")) == entry_id), None)
    if entry is None:
        return (
            SecretTransferPreview(
                kind="bitwarden",
                error=_message(
                    SecretTransferMessageCode.BITWARDEN_BACKUP_NOT_FOUND
                ),
            ),
            None,
        )
    try:
        manifest = bw_backend.read(entry)
    except Exception as exc:
        logger.error("Bitwarden backup read failed: %s", exc)
        return (
            SecretTransferPreview(
                kind="bitwarden",
                error=_message(
                    SecretTransferMessageCode.BITWARDEN_BACKUP_READ_FAILED
                ),
            ),
            None,
        )
    if not isinstance(manifest, dict):
        return (
            SecretTransferPreview(
                kind="bitwarden",
                error=_message(SecretTransferMessageCode.INVALID_SSHPILOT_BACKUP),
            ),
            None,
        )
    settings_path = settings_path or _settings_path()
    return (
        SecretTransferPreview(
            kind="bitwarden",
            included=_included_categories(settings_path, manifest),
        ),
        manifest,
    )


def daemon_preview_ssh_backup(
    manager: Any,
    *,
    connection_id: str,
    remote_dir: str,
    entry_id: str,
    connections_source: Any = None,
    settings_path: Optional[Path | str] = None,
) -> Tuple[SecretTransferPreview, Optional[Dict[str, Any]]]:
    """Preview one SSH-stored backup: included categories (metadata only)."""
    from sshpilot.backup_backends import SSHServerBackupBackend

    runner = _DaemonSshRunner(
        manager, connections_source, connection_id, settings_path or _settings_path()
    )
    backend = SSHServerBackupBackend(runner, remote_dir)
    try:
        entries = backend.list_exports()
    except Exception as exc:
        logger.error("SSH backup listing failed: %s", exc)
        return (
            SecretTransferPreview(
                kind="ssh",
                error=_message(SecretTransferMessageCode.SSH_BACKUP_LIST_FAILED),
            ),
            None,
        )
    entry = next((e for e in entries if str(getattr(e, "id", "")) == entry_id), None)
    if entry is None:
        return (
            SecretTransferPreview(
                kind="ssh",
                error=_message(SecretTransferMessageCode.SSH_BACKUP_NOT_FOUND),
            ),
            None,
        )
    try:
        manifest = backend.read(entry)
    except Exception as exc:
        logger.error("SSH backup read failed: %s", exc)
        return (
            SecretTransferPreview(
                kind="ssh",
                error=_message(SecretTransferMessageCode.SSH_BACKUP_READ_FAILED),
            ),
            None,
        )
    if not isinstance(manifest, dict):
        return (
            SecretTransferPreview(
                kind="ssh",
                error=_message(SecretTransferMessageCode.INVALID_SSHPILOT_BACKUP),
            ),
            None,
        )
    settings_path = settings_path or _settings_path()
    return (
        SecretTransferPreview(
            kind="ssh", included=_included_categories(settings_path, manifest)
        ),
        manifest,
    )


def daemon_list_bitwarden_backups(manager: Any) -> List[Dict[str, str]]:
    """List the Bitwarden backup-note entries (metadata only: id/name/date).

    Runs inside the daemon so the frontend never touches the Bitwarden backend:
    callers receive only identifiers and display names, never note contents.
    """
    backend = manager.get_backend("bitwarden")
    if backend is None or not _safe(lambda: backend.is_available()):
        return []
    from sshpilot.backup_backends import BitwardenBackupBackend

    try:
        entries = BitwardenBackupBackend(backend).list_exports()
    except Exception:
        logger.debug("Bitwarden backup listing failed", exc_info=True)
        return []
    return [{"id": e.id, "name": e.name, "date": e.date or ""} for e in entries]


def daemon_import_bitwarden_backup(
    manager: Any,
    *,
    entry_id: str,
    options: Optional[Dict[str, Any]] = None,
    settings_path: Optional[Path | str] = None,
    manifest: Optional[Dict[str, Any]] = None,
    connection_store_restore: Optional[Any] = None,
) -> SecretTransferResult:
    """Restore one Bitwarden backup note (merge by default, non-destructive).

    Reads the note inside the daemon and applies it through the same
    ``BackupManager.apply_imported_manifest`` path as every other restore — the
    frontend never receives the decrypted manifest. When ``manifest`` is given
    (from the daemon's preview cache) it is applied directly.
    """
    mode = str((options or {}).get("mode") or "merge")
    if mode not in ("replace", "merge"):
        mode = "merge"
    backend = manager.get_backend("bitwarden")
    if backend is None or not _safe(lambda: backend.is_available()):
        return SecretTransferResult(
            operation="import",
            path="bitwarden",
            counts={},
            warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.BITWARDEN_BACKUP_UNAVAILABLE
            ),
        )
    from sshpilot.backup_backends import BitwardenBackupBackend

    bw_backend = BitwardenBackupBackend(backend)
    try:
        entries = bw_backend.list_exports()
    except Exception as exc:
        logger.error("Bitwarden backup listing failed: %s", exc)
        return SecretTransferResult(
            operation="import",
            path="bitwarden",
            counts={},
            warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.BITWARDEN_BACKUP_LIST_FAILED
            ),
        )
    entry = next((e for e in entries if str(getattr(e, "id", "")) == entry_id), None)
    if entry is None:
        return SecretTransferResult(
            operation="import",
            path="bitwarden",
            counts={},
            warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.BITWARDEN_BACKUP_NOT_FOUND
            ),
        )
    if manifest is None:
        try:
            manifest = bw_backend.read(entry)
        except Exception as exc:
            logger.error("Bitwarden backup read failed: %s", exc)
            return SecretTransferResult(
                operation="import",
                path="bitwarden",
                counts={},
                warnings=(),
                status=SecretOperationState.FAILED,
                message=_message(
                    SecretTransferMessageCode.BITWARDEN_BACKUP_READ_FAILED
                ),
            )
    if not isinstance(manifest, dict):
        return SecretTransferResult(
            operation="import",
            path="bitwarden",
            counts={},
            warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(SecretTransferMessageCode.INVALID_SSHPILOT_BACKUP),
        )

    mgr = _backup_manager(
        settings_path or _settings_path(),
        connection_store_restore=connection_store_restore,
    )
    try:
        success, error, restored, keys_written = mgr.apply_imported_manifest(
            manifest,
            mode=mode,
            create_backup=True,
            restore_options=options,
        )
    except Exception as exc:
        logger.error("Bitwarden backup import failed: %s", exc)
        return SecretTransferResult(
            operation="import",
            path="bitwarden",
            counts={},
            warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.BACKUP_IMPORT_FAILED,
                diagnostic=str(exc),
            ),
        )
    counts = {
        "restored": restored,
        "skipped": int(getattr(mgr, "last_import_skipped_credentials", 0) or 0),
        "keys_written": keys_written,
        "keys_skipped": int(getattr(mgr, "last_import_skipped_keys", 0) or 0),
    }
    warnings = list(_connection_store_warnings(mgr))
    if not success:
        return SecretTransferResult(
            operation="import",
            path="bitwarden",
            counts=counts,
            warnings=(),
            status=SecretOperationState.FAILED,
            message=(
                getattr(mgr, "last_transfer_message", None)
                or _message(
                    SecretTransferMessageCode.BACKUP_IMPORT_FAILED_GENERIC,
                    diagnostic=error or "",
                )
            ),
        )
    if not _persists_secrets(manager):
        warnings.append(_message(SecretTransferMessageCode.SECRETS_NOT_PERSISTED))
    logger.info(
        "Backup imported from Bitwarden (restored=%d skipped=%d keys_written=%d keys_skipped=%d)",
        restored, counts["skipped"], keys_written, counts["keys_skipped"],
    )
    return SecretTransferResult(
        operation="import",
        path="bitwarden",
        counts=counts,
        warnings=tuple(warnings),
        status=SecretOperationState.SUCCESS,
        message=None,
    )


class _DaemonSshRunner:
    """One-shot ``ssh <host> <command>`` runner for the SSH-server backup
    destination, driven entirely inside the daemon.

    Reuses the same native-auth argv composition as the file manager
    (``ssh_connection_builder.build_ssh_connection`` + headless askpass env) so
    connection passwords/passphrases are resolved from the daemon's own secret
    manager and never cross into the frontend. The connection record is resolved
    by id/nickname from ``connections_source`` (the daemon repository).
    """

    def __init__(
        self,
        manager: Any,
        connections_source: Any,
        connection_id: str,
        settings_path: Path | str,
    ) -> None:
        self._manager = manager
        self._connections_source = connections_source
        self._connection_id = connection_id
        self._settings_path = settings_path

    def _resolve_record(self):
        records: Iterable[Any] = ()
        if callable(self._connections_source):
            try:
                records = list(self._connections_source())
            except Exception:
                records = []
        elif self._connections_source is not None:
            try:
                records = list(self._connections_source)
            except Exception:
                records = []
        wanted = str(self._connection_id)
        for record in records:
            rid = str(getattr(record, "id", "") or "")
            nickname = str(getattr(record, "nickname", "") or "")
            if rid == wanted or nickname == wanted:
                return record
        return None

    def _manager_shim(self, view):
        """Duck-typed ``connection_manager`` surface for the SSH builder's
        password/passphrase lookups, backed by the daemon's secret manager."""
        manager = self._manager

        class _Shim:
            # Production daemon launches must never stage secrets into the
            # compatibility askpass environment; the interaction broker owns
            # the child credential channel.
            secret_lookup_authoritative = True

            def get_connection_password(self, _conn):
                try:
                    from sshpilot.credential_model import (
                        canonical_password_host,
                        password_host_candidates,
                    )
                    from sshpilot.secret_storage import password_spec

                    user = (getattr(view, "username", "") or "").strip()
                    if not user:
                        return None
                    candidates = list(password_host_candidates(view) or [])
                    canonical = canonical_password_host(view)
                    if canonical and canonical not in candidates:
                        candidates.append(canonical)
                    for host in candidates:
                        if not host:
                            continue
                        value = manager.lookup(password_spec(host, user))
                        if value:
                            return value
                except Exception:
                    pass
                return None

            def get_key_passphrase(self, key_path):
                try:
                    from sshpilot.secret_storage import key_passphrase_spec

                    value = manager.lookup_in_keyring(key_passphrase_spec(key_path))
                    return value
                except Exception:
                    return None

            def prepare_key_for_connection(self, key_path, *, force=True, lifetime=0):
                try:
                    from sshpilot.askpass_utils import ensure_key_in_agent

                    return bool(ensure_key_in_agent(
                        key_path, force=force, lifetime=lifetime))
                except Exception:
                    return False

        return _Shim()

    def _argv_env(self, command: str):
        from sshpilot.daemon.connection_launch_provider import HeadlessConnectionView
        from sshpilot.ssh_connection_builder import (
            ConnectionContext,
            apply_headless_askpass_env,
            build_ssh_connection,
        )

        record = self._resolve_record()
        if record is None:
            raise LookupError(f"The SSH server connection {self._connection_id!r} was not found")
        view = HeadlessConnectionView(record)
        app_config = _settings_shim(self._settings_path)
        ctx = ConnectionContext(
            connection=view,
            connection_manager=self._manager_shim(view),
            config=app_config,
            command_type="ssh",
            native_mode=True,
            extra_args=[],
            remote_command=command,
        )
        prepared = build_ssh_connection(ctx)
        argv = list(prepared.command or ())
        env = apply_headless_askpass_env(
            prepared.env,
            view,
            session_password=getattr(prepared, "password", None) or None,
        )
        return argv, env

    def run_command(
        self,
        command: str,
        *,
        input: Optional[bytes] = None,
        timeout: float = 60,
    ):
        try:
            argv, env = self._argv_env(command)
        except Exception as exc:
            logger.debug("SSH backup runner argv failed: %s", exc)
            return -1, b"", str(exc)
        try:
            proc = subprocess.run(
                argv, env=env, input=input, capture_output=True, timeout=timeout,
            )
            stderr = (proc.stderr or b"").decode("utf-8", "replace")
            return proc.returncode, (proc.stdout or b""), stderr
        except subprocess.TimeoutExpired:
            return -1, b"", "Command timed out"
        except Exception as exc:
            return -1, b"", str(exc)


def _settings_shim(settings_path: Path | str):
    """A minimal ``get_setting`` surface for the SSH builder (read-only)."""
    return _HeadlessBackupConfig(settings_path)


def _ssh_server_destination(destination: str) -> Optional[Tuple[str, str]]:
    """Parse an ``ssh:<connection-id>`` destination into ``(connection_id, remote_dir)``."""
    text = (destination or "").strip()
    if not text.lower().startswith("ssh:"):
        return None
    parts = text[4:].split(":", 1)
    connection_id = (parts[0] or "").strip()
    remote_dir = (parts[1] if len(parts) > 1 else "").strip() or "~/sshpilot-backups"
    if not connection_id:
        return None
    return connection_id, remote_dir


def daemon_list_ssh_backups(
    manager: Any,
    *,
    connection_id: str,
    remote_dir: str,
    connections_source: Any = None,
    settings_path: Optional[Path | str] = None,
) -> List[Dict[str, str]]:
    """List the sshPilot backups stored in ``remote_dir`` on the given server.

    Metadata only (id/name/date) — the archive bytes never leave the daemon.
    """
    from sshpilot.backup_backends import SSHServerBackupBackend

    runner = _DaemonSshRunner(
        manager, connections_source, connection_id, settings_path or _settings_path()
    )
    try:
        entries = SSHServerBackupBackend(runner, remote_dir).list_exports()
    except Exception:
        logger.debug("SSH backup listing failed", exc_info=True)
        return []
    return [
        {"id": getattr(e, "id", ""), "name": getattr(e, "name", ""),
         "date": getattr(e, "date", "") or ""}
        for e in entries
    ]


def daemon_import_ssh_backup(
    manager: Any,
    *,
    connection_id: str,
    remote_dir: str,
    entry_id: str,
    options: Optional[Dict[str, Any]] = None,
    connections_source: Any = None,
    settings_path: Optional[Path | str] = None,
    manifest: Optional[Dict[str, Any]] = None,
    connection_store_restore: Optional[Any] = None,
) -> SecretTransferResult:
    """Download one SSH-stored backup and restore it (merge by default, non-destructive).

    The archive is read inside the daemon and applied through the same
    ``BackupManager.apply_imported_manifest`` path as every other restore. When
    ``manifest`` is given (from the daemon's preview cache) it is applied directly.
    """
    mode = str((options or {}).get("mode") or "merge")
    if mode not in ("replace", "merge"):
        mode = "merge"
    from sshpilot.backup_backends import SSHServerBackupBackend

    runner = _DaemonSshRunner(
        manager, connections_source, connection_id, settings_path or _settings_path()
    )
    backend = SSHServerBackupBackend(runner, remote_dir)
    try:
        entries = backend.list_exports()
    except Exception as exc:
        logger.error("SSH backup listing failed: %s", exc)
        return SecretTransferResult(
            operation="import", path="ssh", counts={}, warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(SecretTransferMessageCode.SSH_BACKUP_LIST_FAILED),
        )
    entry = next((e for e in entries if str(getattr(e, "id", "")) == entry_id), None)
    if entry is None:
        return SecretTransferResult(
            operation="import", path="ssh", counts={}, warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(SecretTransferMessageCode.SSH_BACKUP_NOT_FOUND),
        )
    if manifest is None:
        try:
            manifest = backend.read(entry)
        except Exception as exc:
            logger.error("SSH backup read failed: %s", exc)
            return SecretTransferResult(
                operation="import", path="ssh", counts={}, warnings=(),
                status=SecretOperationState.FAILED,
                message=_message(SecretTransferMessageCode.SSH_BACKUP_READ_FAILED),
            )
    if not isinstance(manifest, dict):
        return SecretTransferResult(
            operation="import", path="ssh", counts={}, warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(SecretTransferMessageCode.INVALID_SSHPILOT_BACKUP),
        )

    mgr = _backup_manager(
        settings_path or _settings_path(),
        connection_store_restore=connection_store_restore,
    )
    try:
        success, error, restored, keys_written = mgr.apply_imported_manifest(
            manifest,
            mode=mode,
            create_backup=True,
            restore_options=options,
        )
    except Exception as exc:
        logger.error("SSH backup import failed: %s", exc)
        return SecretTransferResult(
            operation="import", path="ssh", counts={}, warnings=(),
            status=SecretOperationState.FAILED,
            message=_message(
                SecretTransferMessageCode.BACKUP_IMPORT_FAILED,
                diagnostic=str(exc),
            ),
        )
    counts = {
        "restored": restored,
        "skipped": int(getattr(mgr, "last_import_skipped_credentials", 0) or 0),
        "keys_written": keys_written,
        "keys_skipped": int(getattr(mgr, "last_import_skipped_keys", 0) or 0),
    }
    warnings = list(_connection_store_warnings(mgr))
    if not success:
        return SecretTransferResult(
            operation="import", path="ssh", counts=counts, warnings=(),
            status=SecretOperationState.FAILED,
            message=(
                getattr(mgr, "last_transfer_message", None)
                or _message(
                    SecretTransferMessageCode.BACKUP_IMPORT_FAILED_GENERIC,
                    diagnostic=error or "",
                )
            ),
        )
    if not _persists_secrets(manager):
        warnings.append(_message(SecretTransferMessageCode.SECRETS_NOT_PERSISTED))
    logger.info(
        "Backup imported from SSH server (restored=%d skipped=%d)",
        restored, counts["skipped"],
    )
    return SecretTransferResult(
        operation="import", path="ssh", counts=counts, warnings=tuple(warnings),
        status=SecretOperationState.SUCCESS, message=None,
    )


def _import_warnings(mgr: Any, persists: bool) -> List[str]:
    """Success-dialog warnings for a daemon import: backend persistence plus
    the merge outcomes the GUI path used to surface (``last_merge_collisions``
    / ``last_merge_dropped_globals``)."""
    warnings: List[str] = []
    if not persists:
        warnings.append(
            "The selected secret backend does not persist secrets (agent); "
            "no credentials were restored."
        )
    collisions = getattr(mgr, "last_merge_collisions", None) or []
    if collisions:
        names = ", ".join(" ".join(p) for p in collisions)
        warnings.append(
            "{} imported host(s) shared a name with an existing host; the "
            "conflicting names were left as-is: {}.".format(
                len(collisions), names
            )
        )
    dropped = int(getattr(mgr, "last_merge_dropped_globals", 0) or 0)
    if dropped:
        warnings.append(
            "{} global rule(s) from the backup (Host * / Match blocks) were not "
            "imported — they affect every connection, so they are not merged "
            "automatically.".format(dropped)
        )
    return warnings


def _settings_path() -> Path:
    from sshpilot.platform.paths import get_config_dir

    return get_config_dir() / "config.json"


def _persists_secrets(manager: Any) -> bool:
    try:
        return bool(manager.persists_secrets())
    except Exception:
        return True


def _is_bitwarden_destination(destination: str) -> bool:
    return (destination or "").strip().lower() in {"bitwarden", "bw"}


def _safe(fn, default: Any = False):
    try:
        return fn()
    except Exception:
        return default
