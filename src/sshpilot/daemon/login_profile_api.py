"""Daemon adapter between :class:`LoginProfileService` and the public API.

Translates core values to frontend-neutral DTOs, maps ``CoreError`` to
``SshPilotError``, and republishes the service's change notifications as
``login_profiles.changed`` events. No secret value is logged or returned.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Tuple

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import EventPublisher, EventType
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.login_profiles import (
    AssignLoginProfileRequest,
    ConnectionProfileLink,
    CreateLoginProfileRequest,
    DeleteLoginProfileRequest,
    DeleteLoginProfileResult,
    DetachedConnectionInfo,
    GroupProfileLink,
    LoginProfileAssignmentPreview,
    LoginProfileDetachReason,
    LoginProfileFieldChange,
    LoginProfileId,
    LoginProfileLinkMode,
    LoginProfileSecretKind,
    LoginProfileSettings,
    LoginProfileSnapshot,
    LoginProfilesChangedEvent,
    LoginProfileSource,
    LoginProfileSummary,
    PreviewLoginProfileAssignmentRequest,
    SetGroupLoginProfileRequest,
    SetLoginProfileSecretRequest,
    UpdateLoginProfileRequest,
)
from sshpilot.core.errors import CoreError
from sshpilot.core.errors import ErrorCode as CoreErrorCode
from sshpilot.core.login_profiles.models import LINK_EXPLICIT, LoginProfile
from sshpilot.core.login_profiles.resolver import SOURCE_GROUP
from sshpilot.core.login_profiles.service import EVENT_DETACHED, LoginProfileService

logger = logging.getLogger(__name__)

_AUTH_LABELS = {0: "Key", 1: "Password"}
_KEY_MODE_LABELS = {0: "Automatic", 1: "Specific keys only", 2: "Specific keys and agent"}


def map_login_profile_error(error: CoreError) -> SshPilotError:
    code = {
        CoreErrorCode.VALIDATION_ERROR: ErrorCode.VALIDATION_FAILED,
        CoreErrorCode.STALE_CONNECTION_STATE: ErrorCode.STALE_EDITOR,
        CoreErrorCode.CONNECTION_NOT_FOUND: ErrorCode.CONNECTION_NOT_FOUND,
    }.get(error.code, ErrorCode.PERSISTENCE_FAILED)
    message = str(getattr(error, "message", "") or "") or "The login profile operation failed"
    return SshPilotError(code, message)


def _display(field: str, value: Any) -> str:
    if field == "auth_method":
        return _AUTH_LABELS.get(value, str(value))
    if field == "key_select_mode":
        return _KEY_MODE_LABELS.get(value, str(value))
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    return "" if value is None else str(value)


def settings_from_profile(profile: LoginProfile) -> LoginProfileSettings:
    return LoginProfileSettings(
        name=profile.name,
        username=profile.username,
        auth_method=profile.auth_method,
        key_select_mode=profile.key_select_mode,
        identity_files=tuple(profile.identity_files),
        certificate_files=tuple(profile.certificate_files),
        identity_agent=profile.identity_agent,
        add_keys_to_agent=profile.add_keys_to_agent,
        pkcs11_provider=profile.pkcs11_provider,
        security_key_provider=profile.security_key_provider,
        pubkey_auth_no=profile.pubkey_auth_no,
        forward_agent=profile.forward_agent,
        forward_agent_target=profile.forward_agent_target,
        extra_ssh_config=profile.extra_ssh_config,
    )


def _settings_values(settings: LoginProfileSettings) -> dict:
    return {
        "name": settings.name,
        "username": settings.username,
        "auth_method": settings.auth_method,
        "key_select_mode": settings.key_select_mode,
        "identity_files": tuple(settings.identity_files),
        "certificate_files": tuple(settings.certificate_files),
        "identity_agent": settings.identity_agent,
        "add_keys_to_agent": settings.add_keys_to_agent,
        "pkcs11_provider": settings.pkcs11_provider,
        "security_key_provider": settings.security_key_provider,
        "pubkey_auth_no": settings.pubkey_auth_no,
        "forward_agent": settings.forward_agent,
        "forward_agent_target": settings.forward_agent_target,
        "extra_ssh_config": settings.extra_ssh_config,
    }


def _detached_info(item: Any) -> DetachedConnectionInfo:
    return DetachedConnectionInfo(
        connection_id=ConnectionId(item.connection_id),
        profile_name=item.profile_name,
        reason=LoginProfileDetachReason(item.reason),
    )


class DaemonLoginProfileApi:
    """API-facing facade over the daemon's login profile service."""

    def __init__(self, service: LoginProfileService) -> None:
        self._service = service
        self._publisher = EventPublisher()
        service.set_event_observer(self._on_service_event)

    # -- events ------------------------------------------------------------

    def subscribe_events(self, callback) -> Any:
        return self._publisher.subscribe(callback)

    def close(self) -> None:
        self._publisher.close()

    def _on_service_event(self, kind: str, payload: Mapping[str, Any]) -> None:
        detached: Tuple[DetachedConnectionInfo, ...] = ()
        if kind == EVENT_DETACHED:
            try:
                detached = (
                    DetachedConnectionInfo(
                        connection_id=ConnectionId(str(payload["connection_id"])),
                        profile_name=str(payload.get("profile_name") or ""),
                        reason=LoginProfileDetachReason(str(payload["reason"])),
                    ),
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring a malformed login profile detach notice")
                return
        try:
            self._publisher.publish(
                EventType.LOGIN_PROFILES_CHANGED,
                LoginProfilesChangedEvent(detached=detached),
            )
        except RuntimeError:
            pass  # publisher closed during shutdown

    def sudo_password_for_connection(self, connection_id: str) -> Optional[str]:
        return self._service.sudo_password_for_connection(connection_id)

    def reconcile(self) -> None:
        self._service.reconcile()
        self._on_service_event("changed", {})

    # -- helpers -----------------------------------------------------------

    def _call(self, operation):
        try:
            return operation()
        except SshPilotError:
            raise
        except CoreError as error:
            raise map_login_profile_error(error) from error

    def _summary(self, profile: LoginProfile, counts: Mapping[str, Tuple[int, int]]) -> LoginProfileSummary:
        connections, groups = counts.get(profile.id, (0, 0))
        return LoginProfileSummary(
            id=LoginProfileId(profile.id),
            settings=settings_from_profile(profile),
            revision=profile.revision,
            has_password=profile.has_password,
            has_sudo_password=profile.has_sudo_password,
            connection_count=connections,
            group_count=groups,
        )

    # -- reads -------------------------------------------------------------

    def get_snapshot(self) -> LoginProfileSnapshot:
        def _build() -> LoginProfileSnapshot:
            service = self._service
            profiles = service.list_profiles()
            group_links = service.group_links()
            states = service.link_states()
            counts = {p.id: [0, 0] for p in profiles}
            links = []
            for cid, link, eff in states:
                if eff is not None and eff.profile.id in counts:
                    counts[eff.profile.id][0] += 1
                links.append(
                    ConnectionProfileLink(
                        connection_id=ConnectionId(cid),
                        mode=LoginProfileLinkMode(link.mode),
                        profile_id=(
                            LoginProfileId(link.profile_id)
                            if link.mode == LINK_EXPLICIT
                            else None
                        ),
                        effective_profile_id=(
                            LoginProfileId(eff.profile.id) if eff is not None else None
                        ),
                        source=(
                            None
                            if eff is None
                            else LoginProfileSource.GROUP
                            if eff.source == SOURCE_GROUP
                            else LoginProfileSource.CONNECTION
                        ),
                        group_id=eff.group_id if eff is not None else None,
                    )
                )
            for pid in group_links.values():
                if pid in counts:
                    counts[pid][1] += 1
            frozen_counts = {pid: (c[0], c[1]) for pid, c in counts.items()}
            return LoginProfileSnapshot(
                available=service.available,
                profiles=tuple(self._summary(p, frozen_counts) for p in profiles),
                group_links=tuple(
                    GroupProfileLink(group_id=gid, profile_id=LoginProfileId(pid))
                    for gid, pid in sorted(group_links.items())
                    if service.get_profile(pid) is not None
                ),
                connection_links=tuple(links),
            )

        return self._call(_build)

    def preview_assignment(
        self, request: PreviewLoginProfileAssignmentRequest
    ) -> Tuple[LoginProfileAssignmentPreview, ...]:
        def _preview():
            previews = self._service.preview_assignment(
                list(request.connection_ids),
                request.mode.value if request.mode is not None else None,
                request.profile_id,
            )
            return tuple(
                LoginProfileAssignmentPreview(
                    connection_id=ConnectionId(item.connection_id),
                    profile_name=item.profile_name,
                    changes=tuple(
                        LoginProfileFieldChange(
                            field=change.field,
                            label=change.label,
                            before=_display(change.field, change.before),
                            after=_display(change.field, change.after),
                        )
                        for change in item.changes
                    ),
                )
                for item in previews
            )

        return self._call(_preview)

    # -- writes ------------------------------------------------------------

    def create(self, request: CreateLoginProfileRequest) -> LoginProfileSummary:
        def _create():
            profile = self._service.create_profile(_settings_values(request.settings))
            return self._summary(profile, {})

        return self._call(_create)

    def update(self, request: UpdateLoginProfileRequest) -> LoginProfileSummary:
        def _update():
            profile = self._service.update_profile(
                request.profile_id,
                _settings_values(request.settings),
                expected_revision=request.expected_revision,
            )
            return self._summary(profile, {})

        return self._call(_update)

    def delete(self, request: DeleteLoginProfileRequest) -> DeleteLoginProfileResult:
        def _delete():
            detached = self._service.delete_profile(
                request.profile_id,
                replacement_id=request.replacement_profile_id,
                overrides={item.key: item.target_profile_id for item in request.overrides},
            )
            return DeleteLoginProfileResult(
                detached=tuple(_detached_info(item) for item in detached)
            )

        return self._call(_delete)

    def assign(self, request: AssignLoginProfileRequest) -> bool:
        def _assign():
            self._service.assign_connections(
                list(request.connection_ids),
                request.mode.value if request.mode is not None else None,
                request.profile_id,
            )
            self._on_service_event("changed", {})
            return True

        return self._call(_assign)

    def set_group_profile(self, request: SetGroupLoginProfileRequest) -> bool:
        def _set():
            self._service.set_group_profile(
                request.group_id,
                request.profile_id,
                link_members=list(request.link_members),
            )
            return True

        return self._call(_set)

    def set_secret(
        self, request: SetLoginProfileSecretRequest, secret: Optional[str]
    ) -> bool:
        def _set():
            value = None if request.clear else secret
            if not request.clear and not value:
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST, "A non-empty secret is required"
                )
            if request.kind is LoginProfileSecretKind.SUDO:
                ok = self._service.set_sudo_password(request.profile_id, value)
            else:
                ok = self._service.set_password(request.profile_id, value)
            if not ok:
                raise SshPilotError(
                    ErrorCode.SECRET_STORAGE_FAILED,
                    "The secret could not be stored in secure storage",
                )
            self._on_service_event("changed", {})
            return True

        return self._call(_set)
