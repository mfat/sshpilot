"""Daemon-owned, namespaced persistence for plugin settings."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from sshpilot.core.settings.store import (
    get_nested,
    load_settings,
    save_settings,
    set_nested,
    settings_transaction_lock,
)


def _safe(value: Any, *, path: str = "value") -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must be JSON-compatible")
        return value
    if type(value) is list:
        return [_safe(item, path=f"{path}[]") for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError(f"{path} object keys must be strings")
        return {key: _safe(item, path=f"{path}.{key}") for key, item in value.items()}
    raise TypeError(f"{path} must be JSON-compatible")


def _name(value: str, label: str) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


class PluginSettingsService:
    """Persist only ``plugins.<plugin_id>.<key>`` values in config.json."""

    def __init__(self, settings_path: Path | str) -> None:
        self._path = Path(settings_path)
        self._lock = settings_transaction_lock(self._path)

    @staticmethod
    def _full(plugin_id: str, key: str) -> str:
        return f"plugins.{_name(plugin_id, 'plugin id')}.{_name(key, 'setting key')}"

    def get(self, plugin_id: str, key: str, default: Any = None) -> Any:
        default = _safe(default, path="default")
        with self._lock:
            config, _ = load_settings(self._path)
            value = get_nested(config, self._full(plugin_id, key), default)
            return _safe(value)

    def set(self, plugin_id: str, key: str, value: Any) -> None:
        value = _safe(value)
        # Validate the exact JSON contract before touching the persistent tree.
        json.dumps(value, allow_nan=False)
        with self._lock:
            config, _ = load_settings(self._path)
            set_nested(config, self._full(plugin_id, key), value)
            save_settings(self._path, config)
