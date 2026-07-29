"""Plain JSON settings load/save (no GObject / Gio)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

from .defaults import CONFIG_VERSION, get_default_config
from .migration import ensure_config_defaults

logger = logging.getLogger(__name__)


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


def save_settings(path: Path | str, config: Dict[str, Any]) -> None:
    """Atomically write *config* as JSON to *path*."""
    path = Path(path)
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
