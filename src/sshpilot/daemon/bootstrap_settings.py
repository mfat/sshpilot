"""GI-free, read-only settings view for the daemon.

The daemon must not import ``sshpilot.config`` (GI-backed). This module is the
daemon-owned read-only view over ``config.json`` that production composition
and the M4/M7 launch provider need: the isolated-config flag and the launch
settings the SSH command builder reads (``ssh.`` namespace, askpass, agent
preload).

No writes, no GLib, no ``Config``. Missing or malformed values fall back to
the same defaults the application uses so daemon launch behavior matches the
GTK path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SSH_CONFIG: Dict[str, Any] = {
    "auto_add_host_keys": True,
    "batch_mode": False,
    "compression": False,
    "debug_enabled": False,
    "strict_host_key_checking": "accept-new",
    "use_isolated_config": False,
    "verbosity": 0,
    "ssh_overrides": [],
    "apply_default_keepalive": True,
    "default_keepalive_interval": 15,
    "default_keepalive_count": 3,
}


class DaemonBootstrapSettings:
    """Read-only settings view backed by ``config.json``.

    Values are read from the ``ssh.`` namespace (the same keys the GTK
    ``Config.get_ssh_config`` returns) plus the top-level ``use-askpass``
    preference. The isolated-config flag is exposed directly.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        *,
        loader: Any = None,
    ) -> None:
        if config_path is not None:
            self._config_path = Path(config_path)
        else:
            from ..platform.paths import get_config_dir

            self._config_path = get_config_dir() / "config.json"
        self._loader = loader or _read_json

    # -- data loading --------------------------------------------------------

    def _raw(self) -> Dict[str, Any]:
        try:
            data = self._loader(self._config_path)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def get_ssh_config(self) -> Dict[str, Any]:
        """SSH settings with the application defaults applied (read-only)."""
        raw = self._raw()
        ssh = raw.get("ssh")
        if not isinstance(ssh, dict):
            ssh = {}
        merged = dict(DEFAULT_SSH_CONFIG)
        merged.update({key: value for key, value in ssh.items() if value is not None})
        return merged

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Top-level, ``ssh.``-namespaced, and nested dotted lookup (read-only)."""
        if key.startswith("ssh."):
            nested = key[len("ssh."):]
            return self.get_ssh_config().get(nested, default)
        raw = self._raw()
        if key in raw:
            return raw[key]
        # Dotted keys such as "plugins.enabled" are nested in the file, and a
        # flat lookup silently answers `default` for every one of them.
        from sshpilot.core.settings.store import get_nested

        return get_nested(raw, key, default)

    @property
    def use_isolated_config(self) -> bool:
        return bool(self.get_ssh_config().get("use_isolated_config", False))

    @property
    def askpass_enabled(self) -> bool:
        return bool(self.get_setting("use-askpass", True))

    @property
    def agent_preload_keys(self) -> bool:
        return bool(self.get_ssh_config().get("agent_preload_keys", True))

    @property
    def idle_shutdown_seconds(self):
        return self.get_setting("daemon.idle_shutdown_seconds", None)

    @property
    def service_mode(self) -> bool:
        return bool(
            self.get_setting("daemon.service_mode", False)
            or os.environ.get("SSHPILOT_DAEMON_SERVICE_MODE")
        )

    @property
    def config_file(self) -> Optional[str]:
        raw = self._raw()
        value = raw.get("config_file")
        return str(value) if isinstance(value, str) and value else None


def _read_json(path: Path) -> Dict[str, Any]:
    """Strict-ish JSON read; any failure surfaces as an empty view."""
    if not os.path.exists(str(path)):
        return {}
    with open(str(path), encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}
