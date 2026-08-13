"""Headless, GTK-free SSH overrides service.

Manages the daemon-owned global SSH overrides through the existing
headless settings store.  The service is the single authority for
reading and mutating the semantic SSH fields; it regenerates the
derived ``ssh.ssh_overrides`` list on every mutation.

All field-model contracts (field→config-key mapping, valid ranges,
canonical defaults, strict normalization, revision) live in
``sshpilot.core.settings.ssh_overrides`` so the API model and the
revision token always agree.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.settings import (
    REVISION_CONFLICT,
    SETTINGS_MALFORMED,
    SETTINGS_PERSISTENCE_FAILED,
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
)

from .settings import (
    DEFAULT_SSH_OVERRIDES,
    FIELD_TO_CONFIG_KEY,
    SettingsFileError,
    compose_ssh_overrides,
    compute_ssh_overrides_revision,
    get_default_config,
    load_settings_strict,
    normalize_ssh_overrides,
    save_settings,
    set_nested,
    settings_transaction_lock,
)

logger = logging.getLogger(__name__)


class SshOverridesService:
    """Thread-safe, headless manager for global SSH overrides.

    Reads and writes the canonical semantic SSH fields in the JSON settings
    file.  After every mutation the derived ``ssh.ssh_overrides`` argv list is
    regenerated via :func:`compose_ssh_overrides` and atomically persisted.

    All public methods are serialized with a lock so concurrent stale writers
    are rejected cleanly.

    ``controlmaster_extra`` supplies the ``-o`` args that enable SSH connection
    multiplexing (``ssh_multiplex.controlmaster_args()``).  The daemon injects
    the args unconditionally; :func:`compose_ssh_overrides` decides whether to
    include the fragment from the *currently loaded* ``ssh.controlmaster`` value,
    so toggling it in Preferences applies without a daemon restart.  Core does
    not import the multiplex helper; the daemon injects the args.
    """

    def __init__(
        self,
        settings_path: Path | str,
        *,
        controlmaster_extra: Optional[Sequence[str]] = None,
        on_mutation: Optional[Callable[[], None]] = None,
    ) -> None:
        self._path = Path(settings_path)
        self._lock = threading.Lock()
        self._controlmaster_extra = (
            tuple(controlmaster_extra) if controlmaster_extra is not None else None
        )
        # The daemon may inject a post-persistence runtime hook.  Core owns the
        # mutation; it never imports or manages the runtime implementation.
        self._on_mutation = on_mutation

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
            # The settings transaction lock serializes this complete
            # load -> validate -> apply -> save cycle against the secret
            # backend service, which mutates the same config.json.
            with settings_transaction_lock(self._path):
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
                    config_key = FIELD_TO_CONFIG_KEY[key]
                    set_nested(config, config_key, value)

                # Validate the complete patched state strictly before persisting,
                # then write back the canonical semantic values so the file stays
                # fully normalized (defaults backfilled, host-key value canonical).
                semantic = self._normalize(config)
                self._write_semantic(config, semantic)
                self._compose_and_persist(config)
                snapshot = self._snapshot(config)
            self._notify_mutation()
            return snapshot

    def reset(
        self,
        expected_revision: Optional[str] = None,
    ) -> GlobalSshOverrides:
        """Reset SSH overrides to application defaults.

        Only the semantic fields listed in ``EDITABLE_FIELDS`` are reset;
        every other configuration key is preserved.  The canonical defaults are
        written explicitly so the persisted state matches a fresh file exactly
        (including the revision token).
        """
        with self._lock:
            with settings_transaction_lock(self._path):
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
                snapshot = self._snapshot(config)
            self._notify_mutation()
            return snapshot

    def _notify_mutation(self) -> None:
        """Notify the daemon runtime after a successful persisted mutation."""
        if self._on_mutation is None:
            return
        try:
            self._on_mutation()
        except Exception:
            logger.debug("SSH override runtime refresh failed", exc_info=True)

    # ------------------------------------------------------------------
    # Settings view (for DaemonConnectionLaunchProvider)
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Read a dotted setting key from the authoritative state.

        Best-effort view for the launch builder: a malformed or unreadable
        file degrades to the canonical defaults (logged) instead of aborting
        a connection launch.  The strict API surface (:meth:`get`, :meth:`update`,
        :meth:`reset`) still raises on malformed input.
        """
        if key.startswith("ssh."):
            nested = key[len("ssh."):]
            return self.get_ssh_config().get(nested, default)
        config = self._load_view()
        return config.get(key, default)

    def get_ssh_config(self) -> Dict[str, Any]:
        """Return the ``ssh`` subtree with defaults merged and the derived list.

        Compatible with the ``app_config.get_ssh_config()`` interface consumed
        by ``ssh_connection_builder``: persisted values win over the canonical
        defaults, and the derived ``ssh_overrides`` argv fragment is always
        regenerated so the launch path and the overrides API agree.
        """
        config = self._load_view()
        ssh = dict(get_default_config()["ssh"])
        persisted = config.get("ssh")
        if isinstance(persisted, dict):
            ssh.update({key: value for key, value in persisted.items() if value is not None})
        ssh["ssh_overrides"] = compose_ssh_overrides(
            ssh, controlmaster_extra=self._controlmaster_extra
        )
        return ssh

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_strict(self) -> Dict[str, Any]:
        """Load settings or raise a stable error if the file is malformed."""
        try:
            config, _migrated = load_settings_strict(self._path)
        except SettingsFileError as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings file could not be read",
                details={"code": SETTINGS_MALFORMED},
            ) from exc
        if not isinstance(config, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        # Validate the SSH subtree shape up-front so every read/mutation fails
        # with the stable malformed error instead of a raw conversion exception.
        self._normalize(config)
        return config

    def _load_view(self) -> Dict[str, Any]:
        """Best-effort load for launch-path views (defaults on malformed input).

        A damaged daemon settings file must not abort an SSH connection: the
        builder falls back to the canonical defaults (logged, never rewritten).
        """
        try:
            return self._load_strict()
        except SshPilotError:
            logger.warning(
                "SSH settings file is malformed; using canonical defaults "
                "for the launch view"
            )
            return get_default_config()

    def _normalize(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Strictly normalize the ``ssh`` subtree (wrapped as a stable error)."""
        ssh = config.get("ssh", {})
        if not isinstance(ssh, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        try:
            return normalize_ssh_overrides(ssh)
        except (TypeError, ValueError) as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            ) from exc

    def _snapshot(self, config: Dict[str, Any]) -> GlobalSshOverrides:
        """Build a ``GlobalSshOverrides`` from the normalized ssh subtree."""
        semantic = self._normalize(config)
        return GlobalSshOverrides(
            revision=compute_ssh_overrides_revision(semantic),
            connect_timeout=semantic["connect_timeout"],
            connection_attempts=semantic["connection_attempts"],
            server_alive_interval=semantic["server_alive_interval"],
            server_alive_count_max=semantic["server_alive_count_max"],
            strict_host_key_checking=semantic["strict_host_key_checking"],
            batch_mode=semantic["batch_mode"],
            compression=semantic["compression"],
            verbosity=semantic["verbosity"],
            debug_enabled=semantic["debug_enabled"],
        )

    def _write_semantic(
        self,
        config: Dict[str, Any],
        semantic: Dict[str, Any],
    ) -> None:
        """Write canonical semantic values back to the raw config keys."""
        ssh = config.setdefault("ssh", {})
        if not isinstance(ssh, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        for field, value in semantic.items():
            raw_key = FIELD_TO_CONFIG_KEY[field].split(".", 1)[1]
            ssh[raw_key] = value

    def _compose_and_persist(self, config: Dict[str, Any]) -> None:
        """Regenerate ``ssh.ssh_overrides`` and atomically persist."""
        ssh = config.get("ssh", {})
        if not isinstance(ssh, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        try:
            overrides = compose_ssh_overrides(
                ssh, controlmaster_extra=self._controlmaster_extra
            )
        except Exception as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH overrides could not be composed",
                details={"code": SETTINGS_PERSISTENCE_FAILED},
            ) from exc
        ssh["ssh_overrides"] = overrides
        try:
            save_settings(self._path, config)
        except Exception as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings could not be saved",
                details={"code": SETTINGS_PERSISTENCE_FAILED},
            ) from exc

    def _reset_defaults(self, config: Dict[str, Any]) -> None:
        """Reset only the SSH override fields to their canonical defaults."""
        ssh = config.setdefault("ssh", {})
        if not isinstance(ssh, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The SSH settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        for field, config_key in FIELD_TO_CONFIG_KEY.items():
            raw_key = config_key.split(".", 1)[1]
            ssh[raw_key] = DEFAULT_SSH_OVERRIDES[field]
