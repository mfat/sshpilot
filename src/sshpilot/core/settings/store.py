"""Plain JSON settings load/save (no GObject / Gio)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Tuple

from ...platform.locking import settings_transaction_lock
from .defaults import CONFIG_VERSION, get_default_config
from .migration import ensure_config_defaults
from .versioning import (  # noqa: F401  (errors re-exported for callers)
    SettingsFileError,
    SettingsMigrationError,
    SettingsVersionError,
    upgrade_settings,
)

logger = logging.getLogger(__name__)


def _read_tree(path: Path) -> Tuple[bytes, Dict[str, Any]]:
    """Return ``(raw bytes, parsed object)`` or raise :class:`SettingsFileError`.

    Never modifies the file: damaged bytes stay byte-for-byte unchanged.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SettingsFileError("The settings file could not be read") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsFileError("The settings file is not valid UTF-8") from exc
    if not text.strip():
        raise SettingsFileError("The settings file is empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsFileError(
            "The settings file does not contain valid JSON"
        ) from exc
    if not isinstance(data, dict):
        raise SettingsFileError("The settings file is not a settings object")
    return raw, data


def load_settings(path: Path | str) -> Tuple[Dict[str, Any], bool]:
    """Load settings from *path* for a reader that tolerates damage.

    Returns ``(config, migrated)`` where *migrated* is True when defaults were
    returned or backfilled, or an older tree was upgraded. The upgrade is
    in memory only: this never writes, so a read-only consumer cannot change
    the file. A malformed file, or one this build cannot use (see
    :mod:`.versioning`), yields defaults and is left untouched; a writer must
    use :func:`load_settings_strict` instead so it never saves over it.
    """
    path = Path(path)
    if not path.exists():
        return get_default_config(), True
    try:
        _raw, data = _read_tree(path)
        data, stored = upgrade_settings(data)
    except SettingsFileError as exc:
        logger.warning("Settings file not used (%s); using defaults", exc)
        return get_default_config(), True
    data, updated = ensure_config_defaults(data)
    return data, updated or stored != CONFIG_VERSION


def load_settings_strict(path: Path | str) -> Tuple[Dict[str, Any], bool]:
    """Load settings for an authoritative reader, raising on unusable input.

    Returns ``(config, migrated)`` where *migrated* is True when defaults were
    returned for a missing file or an older tree was upgraded.

    * missing file -> canonical defaults (migrated=True)
    * unreadable, blank, non-JSON or non-object file -> :class:`SettingsFileError`
    * invalid ``config_version`` type/value -> :class:`SettingsFileError`
    * newer or unknown older version -> :class:`SettingsVersionError`
    * known older version -> upgraded on disk by :func:`migrate_settings_file`
      (original bytes kept in a unique backup) and returned; if that cannot
      be done safely, :class:`SettingsMigrationError` and nothing is written

    A valid current-version tree is returned untouched: no rename, replacement,
    or truncation, and no default backfill. Every error path leaves the file
    byte-for-byte unchanged.
    """
    path = Path(path)
    if not path.exists():
        return get_default_config(), True
    migrated = migrate_settings_file(path)
    if not path.exists():
        return get_default_config(), True
    _raw, data = _read_tree(path)
    data, _stored = upgrade_settings(data)
    return data, migrated


def migrate_settings_file(path: Path | str) -> bool:
    """Upgrade an older settings file on disk; return True if it was upgraded.

    Runs under the settings transaction lock and re-reads inside it, so two
    processes migrating at once upgrade the file once. The original bytes are
    first written to a new, uniquely named backup next to the file; only once
    that backup is durable is the upgraded tree written (atomically). A
    current file, or a missing one, is left alone and returns False.

    Raises :class:`SettingsFileError` (or a subclass) without writing anything
    when the file is malformed, newer than this build, has no upgrade path,
    fails to upgrade, or cannot be backed up.
    """
    path = Path(path)
    with settings_transaction_lock(path):
        if not path.exists():
            return False
        raw, data = _read_tree(path)
        upgraded, stored = upgrade_settings(data)
        if stored == CONFIG_VERSION:
            return False
        try:
            backup = _write_backup(path, raw, stored)
        except OSError as exc:
            raise SettingsMigrationError(
                "The settings file could not be backed up before upgrading"
            ) from exc
        _atomic_write(path, upgraded)
        logger.warning(
            "Upgraded settings file from version %s to %s; original kept as %s",
            stored,
            CONFIG_VERSION,
            backup.name,
        )
        return True


def _write_backup(path: Path, raw: bytes, stored_version: int) -> Path:
    """Write *raw* to a new ``<name>.v<version>-<timestamp>[-n].bak`` file.

    Exclusive creation, so an earlier backup is never overwritten.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S")
    base = f"{path.name}.v{stored_version}-{stamp}"
    for attempt in range(1000):
        suffix = "" if attempt == 0 else f"-{attempt}"
        candidate = path.with_name(f"{base}{suffix}.bak")
        try:
            fd = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                candidate.unlink()
            except OSError:
                pass
            raise
        return candidate
    raise FileExistsError(f"no free backup name for {path.name}")


def save_settings(path: Path | str, config: Dict[str, Any]) -> None:
    """Atomically write *config* as JSON to *path*.

    Refuses (raising :class:`SettingsFileError`, file untouched) to replace a
    file this build cannot use: malformed, newer, or an older one that cannot
    first be upgraded with a backup. An older *config* is upgraded before it
    is written, so the file always ends up at ``CONFIG_VERSION``.
    """
    path = Path(path)
    with settings_transaction_lock(path):
        if path.exists():
            migrate_settings_file(path)
        payload = dict(config)
        payload.setdefault("config_version", CONFIG_VERSION)
        payload, _stored = upgrade_settings(payload)
        _atomic_write(path, payload)


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".config-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def get_nested(config: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    """Read a dotted key (``ssh.verbosity``) from a settings dict."""
    cur: Any = config
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_nested(config: Dict[str, Any], dotted_key: str, value: Any) -> None:
    """Write a dotted key into a settings dict (creates intermediate dicts)."""
    parts = dotted_key.split(".")
    cur = config
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
