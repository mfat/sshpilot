"""Headless, GTK-free SSH overrides service.

Manages the daemon-owned global SSH overrides through the existing
headless settings store.  The service is the single authority for
reading and mutating the semantic SSH fields; it regenerates the
derived ``ssh.ssh_overrides`` list on every mutation.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.settings import (
    EDITABLE_FIELDS,
    REVISION_CONFLICT,
    SETTINGS_MALFORMED,
    SETTINGS_PERSISTENCE_FAILED,
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
    _compute_revision,
    _FIELD_TO_CONFIG_KEY,
)

# Expose the field→config-key mapping for callers that need it.
FIELD_TO_CONFIG_KEY = _FIELD_TO_CONFIG_KEY

from .settings import (
    compose_ssh_overrides,
    get_nested,
    load_settings,
    save_settings,
    set_nested,
)

logger = logging.getLogger(__name__)



class SshOverridesService:
    """Thread-safe, headless manager for global SSH overrides.

    Reads and writes the canonical semantic SSH fields in the JSON settings
    file.  After every mutation the derived ``ssh.ssh_overrides`` argv list is
    regenerated via :func:`compose_ssh_overrides` and atomically persisted.

    All public methods are serialized with a lock so concurrent stale writers
    are rejected cleanly.
    """

    def __init__(self, settings_path: Path | str) -> None:
        self._path = Path(settings_path)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self) -> GlobalSshOverrides:
        """Return the authoritative current SSH overrides snapshot."""
        with self._lock:
            config = self._load_strict()
            return self._snapshot(config)

    def update(
        self,
        request: UpdateGlobalSshOverridesRequest,
    ) -> GlobalSshOverrides:
        """Apply a partial patch to the SSH overrides.

        Raises :class:`SshPilotError` with ``REVISION_CONFLICT`` when
        ``expected_revision`` does not match the current revision.
        """
        if type(request) is not UpdateGlobalSshOverridesRequest:
            raise TypeError("an UpdateGlobalSshOverridesRequest is required")
        with self._lock:
            config = self._load_strict()
            current = self._snapshot(config)

            if (
                request.expected_revision is not None
                and request.expected_revision != current.revision
            ):
                raise SshPilotError(
                    ErrorCode.VALIDATION_FAILED,
                    "The SSH overrides have been modified since last read",
                    details={"code": REVISION_CONFLICT},
                )

            if not request.patch:
                return current

            for key, value in request.patch.items():
                config_key = _FIELD_TO_CONFIG_KEY[key]
                set_nested(config, config_key, value)

            self._compose_and_persist(config)
            return self._snapshot(config)

    def reset(
        self,
        expected_revision: Optional[str] = None,
    ) -> GlobalSshOverrides:
        """Reset SSH overrides to application defaults.

        Only the semantic fields listed in ``EDITABLE_FIELDS`` are reset;
        every other configuration key is preserved.
        """
        with self._lock:
            config = self._load_strict()

            if expected_revision is not None:
                current = self._snapshot(config)
                if expected_revision != current.revision:
                    raise SshPilotError(
                        ErrorCode.VALIDATION_FAILED,
                        "The SSH overrides have been modified since last read",
                        details={"code": REVISION_CONFLICT},
                    )

            self._reset_defaults(config)
            self._compose_and_persist(config)
            return self._snapshot(config)

    # ------------------------------------------------------------------
    # Settings view (for DaemonConnectionLaunchProvider)
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Read a dotted setting key from the authoritative state."""
        with self._lock:
            config = self._load_strict()
            return get_nested(config, key, default)

    def get_ssh_config(self) -> Dict[str, Any]:
        """Return the ``ssh`` subtree and the derived override list.

        Compatible with the ``app_config.get_ssh_config()`` interface
        consumed by ``ssh_connection_builder``.
        """
        with self._lock:
            config = self._load_strict()
            ssh = dict(config.get("ssh", {}))
            # Ensure the derived overrides list is always current.
            ssh["ssh_overrides"] = compose_ssh_overrides(ssh)
            return ssh

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_strict(self) -> Dict[str, Any]:
        """Load settings or raise if the file is malformed."""
        try:
            config, _migrated = load_settings(self._path)
        except Exception as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings file could not be read",
                details={"code": SETTINGS_MALFORMED, "error": str(exc)},
            ) from exc
        if not isinstance(config, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        return config

    def _snapshot(self, config: Dict[str, Any]) -> GlobalSshOverrides:
        """Build a ``GlobalSshOverrides`` from the raw config tree."""
        ssh = config.get("ssh", {})

        def _int(key: str) -> int:
            value = ssh.get(key)
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        def _bool(key: str) -> bool:
            return bool(ssh.get(key))

        overrides = GlobalSshOverrides(
            revision=_compute_revision(
                {
                    "connect_timeout": _int("connection_timeout"),
                    "connection_attempts": _int("connection_attempts"),
                    "server_alive_interval": _int("keepalive_interval"),
                    "server_alive_count_max": _int("keepalive_count_max"),
                    "strict_host_key_checking": str(
                        ssh.get("strict_host_key_checking") or ""
                    ).strip(),
                    "batch_mode": _bool("batch_mode"),
                    "compression": _bool("compression"),
                    "verbosity": _int("verbosity"),
                    "debug_enabled": _bool("debug_enabled"),
                }
            ),
            connect_timeout=_int("connection_timeout"),
            connection_attempts=_int("connection_attempts"),
            server_alive_interval=_int("keepalive_interval"),
            server_alive_count_max=_int("keepalive_count_max"),
            strict_host_key_checking=str(
                ssh.get("strict_host_key_checking") or ""
            ).strip()
            or "accept-new",
            batch_mode=_bool("batch_mode"),
            compression=_bool("compression"),
            verbosity=_int("verbosity"),
            debug_enabled=_bool("debug_enabled"),
        )
        return overrides

    def _compose_and_persist(self, config: Dict[str, Any]) -> None:
        """Regenerate ``ssh.ssh_overrides`` and atomically persist."""
        ssh = config.get("ssh", {})
        try:
            overrides = compose_ssh_overrides(ssh)
        except Exception as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH overrides could not be composed",
                details={"code": SETTINGS_PERSISTENCE_FAILED, "error": str(exc)},
            ) from exc
        ssh["ssh_overrides"] = overrides
        try:
            save_settings(self._path, config)
        except Exception as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings could not be saved",
                details={"code": SETTINGS_PERSISTENCE_FAILED, "error": str(exc)},
            ) from exc

    def _reset_defaults(self, config: Dict[str, Any]) -> None:
        """Reset only the SSH override fields to defaults."""
        from .settings.defaults import get_default_config

        defaults = get_default_config()
        default_ssh = defaults.get("ssh", {})
        ssh = config.setdefault("ssh", {})

        for field_name in EDITABLE_FIELDS:
            config_key = _FIELD_TO_CONFIG_KEY[field_name]
            # config_key is like "ssh.connection_timeout"; last part is the key
            _, _, raw_key = config_key.partition(".")
            if raw_key in default_ssh:
                ssh[raw_key] = default_ssh[raw_key]
