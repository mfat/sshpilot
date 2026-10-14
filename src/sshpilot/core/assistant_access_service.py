"""Headless, GTK-free assistant-access settings service.

Persists the AI MCP allowlist and permission ceiling in ``config.json``.
The runtime MCP server reads this through the public API only.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.settings import (
    REVISION_CONFLICT,
    SETTINGS_MALFORMED,
    SETTINGS_PERSISTENCE_FAILED,
    AssistantAccessSettings,
    UpdateAssistantAccessRequest,
)
from .settings import (
    SettingsFileError,
    load_settings_strict,
    save_settings,
    settings_transaction_lock,
)
from .settings.assistant_access import (
    CONFIG_SECTION,
    assistant_access_from_settings,
    compute_assistant_access_revision,
    generate_http_token,
    mcp_http_url,
    normalize_assistant_access,
)

logger = logging.getLogger(__name__)


class AssistantAccessService:
    """Thread-safe manager for assistant MCP access policy."""

    def __init__(
        self,
        settings_path: Path | str,
        *,
        known_connection_ids: Optional[Callable[[], Iterable[str]]] = None,
        http_status_provider: Optional[Callable[[], dict[str, Any]]] = None,
        on_changed: Optional[Callable[[AssistantAccessSettings], None]] = None,
    ) -> None:
        self._path = Path(settings_path)
        self._lock = threading.Lock()
        self._known_connection_ids = known_connection_ids
        self._http_status_provider = http_status_provider
        self._on_changed = on_changed

    def set_http_status_provider(
        self, provider: Optional[Callable[[], dict[str, Any]]]
    ) -> None:
        with self._lock:
            self._http_status_provider = provider

    def set_on_changed(
        self, callback: Optional[Callable[[AssistantAccessSettings], None]]
    ) -> None:
        with self._lock:
            self._on_changed = callback

    def get(self) -> AssistantAccessSettings:
        with self._lock:
            config = self._ensure_token_locked()
            base = self._snapshot_fields(config)
        return self._with_http_status(base)

    def update(self, request: UpdateAssistantAccessRequest) -> AssistantAccessSettings:
        if type(request) is not UpdateAssistantAccessRequest:
            raise TypeError("an UpdateAssistantAccessRequest is required")
        with self._lock:
            with settings_transaction_lock(self._path):
                config = self._load_strict()
                self._ensure_token_in_config(config, persist=False)
                current = self._snapshot_fields(config)
                if (
                    request.expected_revision is not None
                    and request.expected_revision != current.revision
                ):
                    raise SshPilotError(
                        ErrorCode.VALIDATION_FAILED,
                        "Assistant access settings have been modified since last read",
                        details={"code": REVISION_CONFLICT},
                    )
                known = self._resolve_known_ids()
                token = current.http_token or generate_http_token()
                if request.regenerate_token:
                    token = generate_http_token()
                normalized = normalize_assistant_access(
                    {
                        "enabled": request.enabled,
                        "allowed_connection_ids": list(request.allowed_connection_ids),
                        "operate": request.operate,
                        "mutate": request.mutate,
                        "content": request.content,
                        "http_listen": request.http_listen,
                        "http_port": request.http_port,
                        "http_token": token,
                    },
                    known_connection_ids=known,
                    generate_token_if_missing=True,
                )
                config[CONFIG_SECTION] = normalized
                try:
                    save_settings(self._path, config)
                except SettingsFileError as error:
                    raise SshPilotError(
                        ErrorCode.INTERNAL_ERROR,
                        "Failed to persist assistant access settings",
                        details={"code": SETTINGS_PERSISTENCE_FAILED},
                    ) from error
                base = self._snapshot_fields(config)
        snapshot = self._with_http_status(base)
        self._notify_changed(snapshot)
        return snapshot

    def _notify_changed(self, snapshot: AssistantAccessSettings) -> None:
        callback = self._on_changed
        if callback is None:
            return
        try:
            callback(snapshot)
        except Exception:  # noqa: BLE001 - listeners must not break settings
            logger.exception("assistant access on_changed callback failed")

    def _resolve_known_ids(self) -> Optional[list[str]]:
        if self._known_connection_ids is None:
            return None
        try:
            return [str(item) for item in self._known_connection_ids()]
        except Exception:  # noqa: BLE001 - listing failures must not block reads
            logger.debug("assistant access: known connection ids unavailable", exc_info=True)
            return None

    def _load_strict(self) -> dict:
        try:
            config, _migrated = load_settings_strict(self._path)
            return config
        except SettingsFileError as error:
            raise SshPilotError(
                ErrorCode.INTERNAL_ERROR,
                "Assistant access settings are malformed",
                details={"code": SETTINGS_MALFORMED},
            ) from error

    def _ensure_token_locked(self) -> dict:
        with settings_transaction_lock(self._path):
            config = self._load_strict()
            if self._ensure_token_in_config(config, persist=True):
                return config
            return config

    def _ensure_token_in_config(self, config: dict, *, persist: bool) -> bool:
        section = config.get(CONFIG_SECTION)
        if not isinstance(section, dict):
            section = {}
            config[CONFIG_SECTION] = section
        token = section.get("http_token")
        if isinstance(token, str) and token.strip():
            return False
        section["http_token"] = generate_http_token()
        config[CONFIG_SECTION] = section
        if persist:
            try:
                save_settings(self._path, config)
            except SettingsFileError as error:
                raise SshPilotError(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to persist assistant access settings",
                    details={"code": SETTINGS_PERSISTENCE_FAILED},
                ) from error
        return True

    def _http_status(self) -> dict[str, Any]:
        provider = self._http_status_provider
        if provider is None:
            return {
                "http_listening": False,
                "http_bound_port": None,
                "http_url": "",
            }
        try:
            status = dict(provider() or {})
        except Exception:  # noqa: BLE001
            logger.debug("assistant access: http status unavailable", exc_info=True)
            return {
                "http_listening": False,
                "http_bound_port": None,
                "http_url": "",
            }
        bound = status.get("http_bound_port")
        listening = bool(status.get("http_listening"))
        url = status.get("http_url") or (mcp_http_url(bound) if listening else "")
        return {
            "http_listening": listening,
            "http_bound_port": bound if listening else None,
            "http_url": url if listening else "",
        }

    def _snapshot_fields(self, config: dict) -> AssistantAccessSettings:
        known = self._resolve_known_ids()
        fields, revision = assistant_access_from_settings(
            config,
            known_connection_ids=known,
        )
        # When known ids prune the allowlist, keep the revision of the
        # persisted (pre-prune) shape so writers do not spuriously conflict.
        if known is not None:
            persisted, persisted_revision = assistant_access_from_settings(config)
            if persisted["allowed_connection_ids"] != fields["allowed_connection_ids"]:
                revision = persisted_revision
        return AssistantAccessSettings(
            revision=revision or compute_assistant_access_revision(fields),
            enabled=bool(fields["enabled"]),
            allowed_connection_ids=tuple(fields["allowed_connection_ids"]),
            operate=bool(fields["operate"]),
            mutate=bool(fields["mutate"]),
            content=bool(fields["content"]),
            http_listen=bool(fields.get("http_listen")),
            http_port=int(fields.get("http_port") or 0),
            http_token=str(fields.get("http_token") or ""),
        )

    def _with_http_status(
        self, base: AssistantAccessSettings
    ) -> AssistantAccessSettings:
        status = self._http_status()
        return AssistantAccessSettings(
            revision=base.revision,
            enabled=base.enabled,
            allowed_connection_ids=base.allowed_connection_ids,
            operate=base.operate,
            mutate=base.mutate,
            content=base.content,
            http_listen=base.http_listen,
            http_port=base.http_port,
            http_token=base.http_token,
            http_listening=bool(status["http_listening"]),
            http_bound_port=status["http_bound_port"],
            http_url=str(status["http_url"] or ""),
        )
