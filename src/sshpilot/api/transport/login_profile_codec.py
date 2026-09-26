"""Strict wire codecs for ``login_profiles.*`` requests, results, and events.

Kept beside :mod:`codec` (and using its strict field helpers) so the large
shared codec module only gains the event branches. Decoders reject missing and
unknown fields; any ``TypeError``/``ValueError`` is a protocol error.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models.common import ConnectionId
from ..models.login_profiles import (
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
    LoginProfileReassignment,
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
from .codec import _boolean, _identifier, _integer, _strict_fields, _text


def _require(value: Any, expected: type, context: str) -> None:
    if type(value) is not expected:
        raise TypeError(f"{context} is required")


def _array(value: Any, context: str) -> List[Any]:
    if type(value) is not list:
        raise ValueError(f"{context} must be an array")
    return value


def _strings(value: Any, context: str) -> tuple:
    return tuple(_text(item, context) for item in _array(value, context))


def _optional_id(value: Any, context: str) -> Optional[str]:
    return None if value is None else _identifier(value, context)


def _mode(value: Any) -> Optional[LoginProfileLinkMode]:
    return None if value is None else LoginProfileLinkMode(_text(value, "link mode"))


# -- settings / summaries ------------------------------------------------------

_SETTINGS_FIELDS = frozenset(LoginProfileSettings.__dataclass_fields__)  # type: ignore[attr-defined]


def login_profile_settings_to_wire(settings: LoginProfileSettings) -> Dict[str, Any]:
    _require(settings, LoginProfileSettings, "login profile settings")
    return {
        "name": settings.name,
        "username": settings.username,
        "auth_method": settings.auth_method,
        "key_select_mode": settings.key_select_mode,
        "identity_files": list(settings.identity_files),
        "certificate_files": list(settings.certificate_files),
        "identity_agent": settings.identity_agent,
        "add_keys_to_agent": settings.add_keys_to_agent,
        "pkcs11_provider": settings.pkcs11_provider,
        "security_key_provider": settings.security_key_provider,
        "pubkey_auth_no": settings.pubkey_auth_no,
        "forward_agent": settings.forward_agent,
        "forward_agent_target": settings.forward_agent_target,
        "extra_ssh_config": settings.extra_ssh_config,
    }


def login_profile_settings_from_wire(value: Any) -> LoginProfileSettings:
    data = _strict_fields(value, required=_SETTINGS_FIELDS, context="login profile settings")
    text = {
        name: _text(data[name], name, allow_empty=True)
        for name in (
            "username",
            "identity_agent",
            "add_keys_to_agent",
            "pkcs11_provider",
            "security_key_provider",
            "forward_agent_target",
            "extra_ssh_config",
        )
    }
    return LoginProfileSettings(
        name=_text(data["name"], "name"),
        auth_method=_integer(data["auth_method"], "auth_method"),
        key_select_mode=_integer(data["key_select_mode"], "key_select_mode"),
        identity_files=_strings(data["identity_files"], "identity_files"),
        certificate_files=_strings(data["certificate_files"], "certificate_files"),
        pubkey_auth_no=_boolean(data["pubkey_auth_no"], "pubkey_auth_no"),
        forward_agent=_boolean(data["forward_agent"], "forward_agent"),
        **text,
    )


def login_profile_summary_to_wire(summary: LoginProfileSummary) -> Dict[str, Any]:
    _require(summary, LoginProfileSummary, "login profile summary")
    return {
        "id": summary.id,
        "settings": login_profile_settings_to_wire(summary.settings),
        "revision": summary.revision,
        "has_password": summary.has_password,
        "has_sudo_password": summary.has_sudo_password,
        "connection_count": summary.connection_count,
        "group_count": summary.group_count,
    }


def login_profile_summary_from_wire(value: Any) -> LoginProfileSummary:
    data = _strict_fields(
        value,
        required={
            "id", "settings", "revision", "has_password", "has_sudo_password",
            "connection_count", "group_count",
        },
        context="login profile summary",
    )
    return LoginProfileSummary(
        id=LoginProfileId(_identifier(data["id"], "login profile id")),
        settings=login_profile_settings_from_wire(data["settings"]),
        revision=_integer(data["revision"], "revision"),
        has_password=_boolean(data["has_password"], "has_password"),
        has_sudo_password=_boolean(data["has_sudo_password"], "has_sudo_password"),
        connection_count=_integer(data["connection_count"], "connection_count"),
        group_count=_integer(data["group_count"], "group_count"),
    )


# -- snapshot -------------------------------------------------------------------

def connection_profile_link_to_wire(link: ConnectionProfileLink) -> Dict[str, Any]:
    _require(link, ConnectionProfileLink, "connection profile link")
    return {
        "connection_id": link.connection_id,
        "mode": link.mode.value,
        "profile_id": link.profile_id,
        "effective_profile_id": link.effective_profile_id,
        "source": link.source.value if link.source is not None else None,
        "group_id": link.group_id,
    }


def connection_profile_link_from_wire(value: Any) -> ConnectionProfileLink:
    data = _strict_fields(
        value,
        required={
            "connection_id", "mode", "profile_id", "effective_profile_id", "source", "group_id",
        },
        context="connection profile link",
    )
    source = data["source"]
    return ConnectionProfileLink(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        mode=LoginProfileLinkMode(_text(data["mode"], "link mode")),
        profile_id=_optional_id(data["profile_id"], "login profile id"),
        effective_profile_id=_optional_id(data["effective_profile_id"], "effective profile id"),
        source=None if source is None else LoginProfileSource(_text(source, "profile source")),
        group_id=_optional_id(data["group_id"], "group id"),
    )


def login_profile_snapshot_to_wire(snapshot: LoginProfileSnapshot) -> Dict[str, Any]:
    _require(snapshot, LoginProfileSnapshot, "login profile snapshot")
    return {
        "available": snapshot.available,
        "profiles": [login_profile_summary_to_wire(p) for p in snapshot.profiles],
        "group_links": [
            {"group_id": g.group_id, "profile_id": g.profile_id} for g in snapshot.group_links
        ],
        "connection_links": [
            connection_profile_link_to_wire(link) for link in snapshot.connection_links
        ],
    }


def login_profile_snapshot_from_wire(value: Any) -> LoginProfileSnapshot:
    data = _strict_fields(
        value,
        required={"available", "profiles", "group_links", "connection_links"},
        context="login profile snapshot",
    )
    group_links = []
    for item in _array(data["group_links"], "group links"):
        link = _strict_fields(item, required={"group_id", "profile_id"}, context="group profile link")
        group_links.append(
            GroupProfileLink(
                group_id=_identifier(link["group_id"], "group id"),
                profile_id=LoginProfileId(_identifier(link["profile_id"], "login profile id")),
            )
        )
    return LoginProfileSnapshot(
        available=_boolean(data["available"], "available"),
        profiles=tuple(
            login_profile_summary_from_wire(p) for p in _array(data["profiles"], "profiles")
        ),
        group_links=tuple(group_links),
        connection_links=tuple(
            connection_profile_link_from_wire(item)
            for item in _array(data["connection_links"], "connection links")
        ),
    )


# -- requests ---------------------------------------------------------------------

def create_login_profile_request_to_wire(request: CreateLoginProfileRequest) -> Dict[str, Any]:
    _require(request, CreateLoginProfileRequest, "create login profile request")
    return {"settings": login_profile_settings_to_wire(request.settings)}


def create_login_profile_request_from_wire(value: Any) -> CreateLoginProfileRequest:
    data = _strict_fields(value, required={"settings"}, context="create login profile request")
    return CreateLoginProfileRequest(settings=login_profile_settings_from_wire(data["settings"]))


def update_login_profile_request_to_wire(request: UpdateLoginProfileRequest) -> Dict[str, Any]:
    _require(request, UpdateLoginProfileRequest, "update login profile request")
    return {
        "profile_id": request.profile_id,
        "settings": login_profile_settings_to_wire(request.settings),
        "expected_revision": request.expected_revision,
    }


def update_login_profile_request_from_wire(value: Any) -> UpdateLoginProfileRequest:
    data = _strict_fields(
        value,
        required={"profile_id", "settings", "expected_revision"},
        context="update login profile request",
    )
    expected = data["expected_revision"]
    return UpdateLoginProfileRequest(
        profile_id=LoginProfileId(_identifier(data["profile_id"], "login profile id")),
        settings=login_profile_settings_from_wire(data["settings"]),
        expected_revision=None if expected is None else _integer(expected, "expected_revision"),
    )


def delete_login_profile_request_to_wire(request: DeleteLoginProfileRequest) -> Dict[str, Any]:
    _require(request, DeleteLoginProfileRequest, "delete login profile request")
    return {
        "profile_id": request.profile_id,
        "replacement_profile_id": request.replacement_profile_id,
        "overrides": [
            {"key": item.key, "target_profile_id": item.target_profile_id}
            for item in request.overrides
        ],
    }


def delete_login_profile_request_from_wire(value: Any) -> DeleteLoginProfileRequest:
    data = _strict_fields(
        value,
        required={"profile_id", "replacement_profile_id", "overrides"},
        context="delete login profile request",
    )
    overrides = []
    for item in _array(data["overrides"], "overrides"):
        entry = _strict_fields(item, required={"key", "target_profile_id"}, context="reassignment")
        overrides.append(
            LoginProfileReassignment(
                key=_identifier(entry["key"], "reassignment key"),
                target_profile_id=_optional_id(entry["target_profile_id"], "target profile id"),
            )
        )
    return DeleteLoginProfileRequest(
        profile_id=LoginProfileId(_identifier(data["profile_id"], "login profile id")),
        replacement_profile_id=_optional_id(data["replacement_profile_id"], "replacement id"),
        overrides=tuple(overrides),
    )


def detached_connection_info_to_wire(info: DetachedConnectionInfo) -> Dict[str, Any]:
    _require(info, DetachedConnectionInfo, "detached connection")
    return {
        "connection_id": info.connection_id,
        "profile_name": info.profile_name,
        "reason": info.reason.value,
    }


def detached_connection_info_from_wire(value: Any) -> DetachedConnectionInfo:
    data = _strict_fields(
        value, required={"connection_id", "profile_name", "reason"}, context="detached connection"
    )
    return DetachedConnectionInfo(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        profile_name=_text(data["profile_name"], "profile name", allow_empty=True),
        reason=LoginProfileDetachReason(_text(data["reason"], "detach reason")),
    )


def delete_login_profile_result_to_wire(result: DeleteLoginProfileResult) -> Dict[str, Any]:
    _require(result, DeleteLoginProfileResult, "delete login profile result")
    return {"detached": [detached_connection_info_to_wire(d) for d in result.detached]}


def delete_login_profile_result_from_wire(value: Any) -> DeleteLoginProfileResult:
    data = _strict_fields(value, required={"detached"}, context="delete login profile result")
    return DeleteLoginProfileResult(
        detached=tuple(
            detached_connection_info_from_wire(d) for d in _array(data["detached"], "detached")
        )
    )


def _assignment_to_wire(request) -> Dict[str, Any]:
    return {
        "connection_ids": list(request.connection_ids),
        "mode": request.mode.value if request.mode is not None else None,
        "profile_id": request.profile_id,
    }


def _assignment_fields(value: Any, context: str) -> Dict[str, Any]:
    data = _strict_fields(value, required={"connection_ids", "mode", "profile_id"}, context=context)
    return {
        "connection_ids": tuple(
            ConnectionId(_identifier(item, "connection id"))
            for item in _array(data["connection_ids"], "connection ids")
        ),
        "mode": _mode(data["mode"]),
        "profile_id": _optional_id(data["profile_id"], "login profile id"),
    }


def assign_login_profile_request_to_wire(request: AssignLoginProfileRequest) -> Dict[str, Any]:
    _require(request, AssignLoginProfileRequest, "assign login profile request")
    return _assignment_to_wire(request)


def assign_login_profile_request_from_wire(value: Any) -> AssignLoginProfileRequest:
    return AssignLoginProfileRequest(**_assignment_fields(value, "assign login profile request"))


def preview_login_profile_assignment_request_to_wire(
    request: PreviewLoginProfileAssignmentRequest,
) -> Dict[str, Any]:
    _require(request, PreviewLoginProfileAssignmentRequest, "preview assignment request")
    return _assignment_to_wire(request)


def preview_login_profile_assignment_request_from_wire(
    value: Any,
) -> PreviewLoginProfileAssignmentRequest:
    return PreviewLoginProfileAssignmentRequest(
        **_assignment_fields(value, "preview assignment request")
    )


def login_profile_assignment_previews_to_wire(previews) -> Dict[str, Any]:
    items = []
    for preview in previews:
        _require(preview, LoginProfileAssignmentPreview, "assignment preview")
        items.append(
            {
                "connection_id": preview.connection_id,
                "profile_name": preview.profile_name,
                "changes": [
                    {"field": c.field, "label": c.label, "before": c.before, "after": c.after}
                    for c in preview.changes
                ],
            }
        )
    return {"previews": items}


def login_profile_assignment_previews_from_wire(value: Any):
    data = _strict_fields(value, required={"previews"}, context="assignment previews")
    previews = []
    for item in _array(data["previews"], "previews"):
        entry = _strict_fields(
            item, required={"connection_id", "profile_name", "changes"}, context="assignment preview"
        )
        changes = []
        for change in _array(entry["changes"], "changes"):
            c = _strict_fields(
                change, required={"field", "label", "before", "after"}, context="field change"
            )
            changes.append(
                LoginProfileFieldChange(
                    field=_text(c["field"], "field"),
                    label=_text(c["label"], "label"),
                    before=_text(c["before"], "before", allow_empty=True),
                    after=_text(c["after"], "after", allow_empty=True),
                )
            )
        previews.append(
            LoginProfileAssignmentPreview(
                connection_id=ConnectionId(_identifier(entry["connection_id"], "connection id")),
                profile_name=_text(entry["profile_name"], "profile name", allow_empty=True),
                changes=tuple(changes),
            )
        )
    return tuple(previews)


def set_group_login_profile_request_to_wire(
    request: SetGroupLoginProfileRequest,
) -> Dict[str, Any]:
    _require(request, SetGroupLoginProfileRequest, "set group login profile request")
    return {
        "group_id": request.group_id,
        "profile_id": request.profile_id,
        "link_members": list(request.link_members),
    }


def set_group_login_profile_request_from_wire(value: Any) -> SetGroupLoginProfileRequest:
    data = _strict_fields(
        value,
        required={"group_id", "profile_id", "link_members"},
        context="set group login profile request",
    )
    return SetGroupLoginProfileRequest(
        group_id=_identifier(data["group_id"], "group id"),
        profile_id=_optional_id(data["profile_id"], "login profile id"),
        link_members=tuple(
            ConnectionId(_identifier(item, "connection id"))
            for item in _array(data["link_members"], "link members")
        ),
    )


def set_login_profile_secret_request_to_wire(
    request: SetLoginProfileSecretRequest,
) -> Dict[str, Any]:
    _require(request, SetLoginProfileSecretRequest, "set login profile secret request")
    return {"profile_id": request.profile_id, "kind": request.kind.value, "clear": request.clear}


def set_login_profile_secret_request_from_wire(value: Any) -> SetLoginProfileSecretRequest:
    data = _strict_fields(
        value, required={"profile_id", "kind", "clear"}, context="set login profile secret request"
    )
    return SetLoginProfileSecretRequest(
        profile_id=LoginProfileId(_identifier(data["profile_id"], "login profile id")),
        kind=LoginProfileSecretKind(_text(data["kind"], "secret kind")),
        clear=_boolean(data["clear"], "clear"),
    )


# -- event --------------------------------------------------------------------------

def login_profiles_changed_event_to_wire(event: LoginProfilesChangedEvent) -> Dict[str, Any]:
    _require(event, LoginProfilesChangedEvent, "login profiles changed event")
    return {"detached": [detached_connection_info_to_wire(d) for d in event.detached]}


def login_profiles_changed_event_from_wire(value: Any) -> LoginProfilesChangedEvent:
    data = _strict_fields(value, required={"detached"}, context="login profiles changed event")
    return LoginProfilesChangedEvent(
        detached=tuple(
            detached_connection_info_from_wire(d) for d in _array(data["detached"], "detached")
        )
    )
