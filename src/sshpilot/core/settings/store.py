"""Plain JSON settings load/save (no GObject / Gio)."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

from ...platform.locking import settings_transaction_lock
from .defaults import CONFIG_VERSION, get_default_config
from .migration import ensure_config_defaults

logger = logging.getLogger(__name__)

class SettingsFileError(RuntimeError):
    """Raised when a settings file cannot be read as a valid current-version tree.

    This is the core-level signal for authoritative readers; the daemon maps it
    to a stable sanitized API error (no paths, no raw JSON, no exception text).
    """


def load_settings(path: Path | str) -> Tuple[Dict[str, Any], bool]:
    """Load settings from *path*.

    Returns ``(config, migrated)`` where *migrated* is True when defaults were
    backfilled or an obsolete file was replaced with defaults.
    """
    path = Path(path)
    if not path.exists():
        return get_default_config(), True

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse settings %s: %s", path, exc)
        return get_default_config(), True

    if not isinstance(data, dict):
        return get_default_config(), True

    stored_version = data.get("config_version")
    try:
        stored_version = int(stored_version) if stored_version is not None else 0
    except (TypeError, ValueError):
        stored_version = 0

    if stored_version < CONFIG_VERSION:
        # Match Config.load_json_config: obsolete trees are replaced, not partially merged.
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            if path.exists():
                path.replace(backup)
        except OSError:
            logger.debug("Could not back up obsolete config", exc_info=True)
        return get_default_config(), True

    data, updated = ensure_config_defaults(data)
    return data, updated


def load_settings_strict(path: Path | str) -> Tuple[Dict[str, Any], bool]:
    """Load settings for an authoritative reader, raising on malformed input.

    Returns ``(config, migrated)`` where *migrated* is True when defaults were
    returned (missing file or an obsolete version tree was replaced).

    Unlike :func:`load_settings`, this never silently swallows damage:

    * missing file -> canonical defaults (migrated=True)
    * unreadable file -> :class:`SettingsFileError`
    * blank / whitespace-only file -> :class:`SettingsFileError`
    * invalid JSON -> :class:`SettingsFileError`
    * non-object root -> :class:`SettingsFileError`
    * invalid ``config_version`` type/value -> :class:`SettingsFileError`
    * obsolete ``config_version`` (a valid integer < CONFIG_VERSION) -> replaced
      with a ``.bak`` backup and defaults returned (preserves the documented
      version migration)

    A valid current-version tree is returned untouched: no rename, replacement,
    or truncation, and no default backfill beyond the explicit version migration.
    Malformed input is never modified — every error path returns without touching
    the file, so the damaged bytes stay byte-for-byte unchanged.
    """
    path = Path(path)
    if not path.exists():
        return get_default_config(), True

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsFileError(
            "The settings file could not be read"
        ) from exc

    if not raw.strip():
        raise SettingsFileError("The settings file is empty")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsFileError(
            "The settings file does not contain valid JSON"
        ) from exc

    if not isinstance(data, dict):
        raise SettingsFileError("The settings file is not a settings object")

    stored_version = _parse_stored_version(data)

    if stored_version < CONFIG_VERSION:
        # Match load_settings: obsolete trees are replaced, not partially merged.
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            if path.exists():
                path.replace(backup)
        except OSError:
            logger.debug("Could not back up obsolete config", exc_info=True)
        return get_default_config(), True

    return data, False


def _parse_stored_version(data: Dict[str, Any]) -> int:
    """Return the stored ``config_version`` as an integer.

    A missing ``config_version`` is treated as version 0 (obsolete) so legacy
    trees still migrate safely.  Any other non-integer type or value — bools,
    floats, non-numeric strings, lists, dicts — is malformed and raises.
    """
    value = data.get("config_version")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise SettingsFileError(
            "config_version must be an integer"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    raise SettingsFileError(
        "config_version must be an integer"
    )


def save_settings(path: Path | str, config: Dict[str, Any]) -> None:
    """Atomically write *config* as JSON to *path*."""
    path = Path(path)
    with settings_transaction_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(config)
        payload.setdefault("config_version", CONFIG_VERSION)
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
