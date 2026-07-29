"""Strict Protocol v1 envelope and public-DTO JSON codecs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AbstractSet, Any, Dict, Iterable, Union

from .._safe_values import copy_safe_details
from ..capabilities import Capabilities, Capability
from ..errors import ErrorCode, SshPilotError
from ..events import CoreEvent, EventType
from ..models.common import (
    AttachmentId,
    ClientId,
    ClientInfo,
    CompatibilityResult,
    ConnectionId,
    CoreInfo,
    SessionId,
)
from ..models.connections import (
    AuthenticationMethod,
    ConnectionDetails,
    ConnectionHealth,
    ConnectionSummary,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    GroupReference,
    UpdateConnectionRequest,
)
from ..models.sessions import (
    AttachSessionRequest,
    AttachSessionResult,
    AttachmentInfo,
    CloseSessionRequest,
    DetachSessionRequest,
    InputOwner,
    OpenSessionRequest,
    SessionCapabilities,
    SessionExitInfo,
    SessionFailure,
    SessionState,
    SessionSummary,
)
from ..session_identity import session_id_from_uuid, session_uuid_from_id
from .envelopes import (
    ErrorData,
    ErrorResponseEnvelope,
    EventEnvelope,
    HandshakeRequest,
    HandshakeResult,
    RequestEnvelope,
    SuccessResponseEnvelope,
)

Envelope = Union[
    RequestEnvelope,
    SuccessResponseEnvelope,
    ErrorResponseEnvelope,
    EventEnvelope,
]


def _strict_fields(
    value: Any,
    *,
    required: AbstractSet[str],
    optional: AbstractSet[str] = frozenset(),
    context: str,
) -> Dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{context} must be a JSON object")
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise ValueError(f"{context} is missing required fields")
    if unknown:
        raise ValueError(f"{context} contains unsupported fields")
    return value


def _identifier(value: Any, context: str) -> str:
    return _text(value, context)


def _session_id(value: Any, context: str) -> SessionId:
    text = _identifier(value, context)
    try:
        return SessionId(session_id_from_uuid(session_uuid_from_id(text)))
    except (TypeError, ValueError):
        raise ValueError(f"{context} is malformed") from None


def _text(value: Any, context: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()):
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _boolean(value: Any, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{context} must be a boolean")
    return value


def _integer(value: Any, context: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{context} must be an integer")
    return value


def _json_value(value: Any, context: str = "transport value") -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        return copy_safe_details({"value": value})["value"]
    if type(value) is list:
        return [_json_value(item, f"{context}[]") for item in value]
    if type(value) is dict:
        return copy_safe_details(value)
    raise TypeError(f"{context} contains an unsupported value")


def error_to_wire(error: SshPilotError) -> ErrorData:
    return ErrorData(
        code=error.code,
        message=error.message,
        details=error.details,
        retryable=error.retryable,
        request_id=error.request_id,
        connection_id=(
            ConnectionId(error.connection_id) if error.connection_id is not None else None
        ),
        session_id=(SessionId(error.session_id) if error.session_id is not None else None),
    )


def error_from_wire(error: ErrorData) -> SshPilotError:
    return SshPilotError(
        error.code,
        error.message,
        details=error.details,
        retryable=error.retryable,
        request_id=error.request_id,
        connection_id=error.connection_id,
        session_id=error.session_id,
    )


def _error_data_to_dict(error: ErrorData) -> Dict[str, Any]:
    return {
        "code": error.code.value,
        "message": error.message,
        "details": dict(error.details),
        "retryable": error.retryable,
        "request_id": error.request_id,
        "connection_id": error.connection_id,
        "session_id": error.session_id,
    }


def _error_data_from_dict(value: Any) -> ErrorData:
    data = _strict_fields(
        value,
        required={"code", "message", "details", "retryable"},
        optional={"request_id", "connection_id", "session_id"},
        context="error data",
    )
    try:
        code = ErrorCode(data["code"])
    except (TypeError, ValueError):
        raise ValueError("error data contains an unknown error code") from None
    session_id = data.get("session_id")
    if session_id is not None:
        session_id = _session_id(session_id, "error session id")
    return ErrorData(
        code=code,
        message=_identifier(data["message"], "error message"),
        details=data["details"],
        retryable=data["retryable"],
        request_id=data.get("request_id"),
        connection_id=data.get("connection_id"),
        session_id=session_id,
    )


def encode_envelope(envelope: Envelope) -> Dict[str, Any]:
    """Convert a known envelope into its strict JSON object representation."""

    if isinstance(envelope, RequestEnvelope):
        return {
            "type": "request",
            "protocol_version": envelope.protocol_version,
            "request_id": envelope.request_id,
            "method": envelope.method,
            "params": _json_value(dict(envelope.params), "request params"),
            "client_id": envelope.client_id,
        }
    if isinstance(envelope, SuccessResponseEnvelope):
        return {
            "type": "success",
            "protocol_version": envelope.protocol_version,
            "request_id": envelope.request_id,
            "result": _json_value(envelope.result, "response result"),
        }
    if isinstance(envelope, ErrorResponseEnvelope):
        return {
            "type": "error",
            "protocol_version": envelope.protocol_version,
            "request_id": envelope.request_id,
            "error": _error_data_to_dict(envelope.error),
        }
    if isinstance(envelope, EventEnvelope):
        return {
            "type": "event",
            "protocol_version": envelope.protocol_version,
            "event": envelope.event,
            "sequence": envelope.sequence,
            "payload": _json_value(dict(envelope.payload), "event payload"),
        }
    raise TypeError("only known Protocol v1 envelopes can be encoded")


def decode_envelope(value: Any) -> Envelope:
    """Validate and construct exactly one known Protocol v1 envelope."""

    if type(value) is not dict:
        raise ValueError("transport envelope must be a JSON object")
    envelope_type = value.get("type")
    if envelope_type == "request":
        data = _strict_fields(
            value,
            required={
                "type",
                "protocol_version",
                "request_id",
                "method",
                "params",
                "client_id",
            },
            context="request envelope",
        )
        return RequestEnvelope(
            protocol_version=data["protocol_version"],
            request_id=data["request_id"],
            method=data["method"],
            params=data["params"],
            client_id=data["client_id"],
        )
    if envelope_type == "success":
        data = _strict_fields(
            value,
            required={"type", "protocol_version", "request_id", "result"},
            context="success response envelope",
        )
        return SuccessResponseEnvelope(
            protocol_version=data["protocol_version"],
            request_id=data["request_id"],
            result=_json_value(data["result"], "response result"),
        )
    if envelope_type == "error":
        data = _strict_fields(
            value,
            required={"type", "protocol_version", "request_id", "error"},
            context="error response envelope",
        )
        return ErrorResponseEnvelope(
            protocol_version=data["protocol_version"],
            request_id=data["request_id"],
            error=_error_data_from_dict(data["error"]),
        )
    if envelope_type == "event":
        data = _strict_fields(
            value,
            required={"type", "protocol_version", "event", "sequence", "payload"},
            context="event envelope",
        )
        envelope = EventEnvelope(
            protocol_version=data["protocol_version"],
            event=data["event"],
            sequence=data["sequence"],
            payload=data["payload"],
        )
        public_event_from_envelope(envelope)
        return envelope
    raise ValueError("transport envelope type is unknown")


_CONNECTION_EVENT_TYPES = frozenset(
    {
        EventType.CONNECTION_CREATED,
        EventType.CONNECTION_UPDATED,
        EventType.CONNECTION_DELETED,
    }
)
_SESSION_EVENT_TYPES = frozenset(
    {
        EventType.SESSION_CREATED,
        EventType.SESSION_STATE_CHANGED,
        EventType.SESSION_EXITED,
        EventType.SESSION_CLOSED,
    }
)
_FORWARDED_EVENT_TYPES = _CONNECTION_EVENT_TYPES | _SESSION_EVENT_TYPES


def public_event_to_envelope(
    event: CoreEvent,
    *,
    sequence: int,
    protocol_version: str,
) -> EventEnvelope:
    """Encode one approved public lifecycle event for daemon transport."""

    if not isinstance(event, CoreEvent) or event.type not in _FORWARDED_EVENT_TYPES:
        raise TypeError("daemon transport does not support this event type")
    if event.type in _CONNECTION_EVENT_TYPES:
        if type(event.payload) is not ConnectionSummary:
            raise TypeError("connection event payload must be ConnectionSummary")
        payload = connection_summary_to_wire(event.payload)
    elif event.type is EventType.SESSION_EXITED:
        if type(event.payload) is not SessionExitInfo:
            raise TypeError("session exited payload must be SessionExitInfo")
        if event.session_id is None:
            raise TypeError("session exited event requires a session id")
        payload = {
            "session_id": event.session_id,
            "exit_info": session_exit_info_to_wire(event.payload),
        }
    else:
        if type(event.payload) is not SessionSummary:
            raise TypeError("session event payload must be SessionSummary")
        payload = session_summary_to_wire(event.payload)
    return EventEnvelope(
        protocol_version=protocol_version,
        event=event.type.value,
        sequence=sequence,
        payload=payload,
    )


def public_event_from_envelope(envelope: EventEnvelope) -> CoreEvent:
    """Decode one strict daemon lifecycle event into the public event model."""

    if not isinstance(envelope, EventEnvelope):
        raise TypeError("event envelope is required")
    try:
        event_type = EventType(envelope.event)
    except ValueError:
        raise ValueError("daemon event name is unsupported") from None
    if event_type not in _FORWARDED_EVENT_TYPES:
        raise ValueError("daemon event name is unsupported")
    if event_type in _CONNECTION_EVENT_TYPES:
        summary = connection_summary_from_wire(dict(envelope.payload))
        return CoreEvent(
            type=event_type,
            payload=summary,
            sequence=envelope.sequence,
            connection_id=summary.id,
        )
    if event_type is EventType.SESSION_EXITED:
        data = _strict_fields(
            dict(envelope.payload),
            required={"session_id", "exit_info"},
            context="session exited event",
        )
        session_id = _session_id(
            data["session_id"],
            "session exited event session id",
        )
        exit_info = session_exit_info_from_wire(data["exit_info"])
        return CoreEvent(
            type=event_type,
            payload=exit_info,
            sequence=envelope.sequence,
            session_id=session_id,
        )
    summary = session_summary_from_wire(dict(envelope.payload))
    return CoreEvent(
        type=event_type,
        payload=summary,
        sequence=envelope.sequence,
        connection_id=summary.connection_id,
        session_id=summary.id,
    )


def connection_event_to_envelope(
    event: CoreEvent,
    *,
    sequence: int,
    protocol_version: str,
) -> EventEnvelope:
    """Encode one approved public connection event for daemon transport."""

    if not isinstance(event, CoreEvent) or event.type not in _CONNECTION_EVENT_TYPES:
        raise TypeError("daemon transport supports connection events only")
    return public_event_to_envelope(
        event,
        sequence=sequence,
        protocol_version=protocol_version,
    )


def connection_event_from_envelope(envelope: EventEnvelope) -> CoreEvent:
    """Decode one strict daemon connection event into the public event model."""

    try:
        event_type = EventType(envelope.event)
    except (AttributeError, ValueError):
        raise ValueError("daemon event name is unsupported") from None
    if event_type not in _CONNECTION_EVENT_TYPES:
        raise ValueError("daemon event name is unsupported")
    event = public_event_from_envelope(envelope)
    return event


def handshake_request_to_wire(request: HandshakeRequest) -> Dict[str, Any]:
    return {
        "client_name": request.client_name,
        "client_version": request.client_version,
        "supported_protocol_versions": list(request.supported_protocol_versions),
        "client_capabilities": sorted(request.client_capabilities),
        "frontend_type": request.frontend_type,
    }


def handshake_request_from_wire(value: Any) -> HandshakeRequest:
    data = _strict_fields(
        value,
        required={
            "client_name",
            "client_version",
            "supported_protocol_versions",
            "client_capabilities",
            "frontend_type",
        },
        context="handshake request",
    )
    versions = data["supported_protocol_versions"]
    capabilities = data["client_capabilities"]
    if type(versions) is not list or type(capabilities) is not list:
        raise ValueError("handshake version and capability fields must be arrays")
    return HandshakeRequest(
        client_name=data["client_name"],
        client_version=data["client_version"],
        supported_protocol_versions=tuple(versions),
        client_capabilities=frozenset(
            _identifier(item, "client capability") for item in capabilities
        ),
        frontend_type=data["frontend_type"],
    )


def handshake_result_to_wire(result: HandshakeResult) -> Dict[str, Any]:
    return {
        "daemon_version": result.daemon_version,
        "core_version": result.core_version,
        "selected_protocol_version": result.selected_protocol_version,
        "daemon_capabilities": sorted(item.value for item in result.daemon_capabilities),
        "compatibility_status": result.compatibility_status,
        "server_instance_id": result.server_instance_id,
    }


def handshake_result_from_wire(value: Any) -> HandshakeResult:
    data = _strict_fields(
        value,
        required={
            "daemon_version",
            "core_version",
            "selected_protocol_version",
            "daemon_capabilities",
            "compatibility_status",
            "server_instance_id",
        },
        context="handshake result",
    )
    capabilities = data["daemon_capabilities"]
    if type(capabilities) is not list:
        raise ValueError("daemon capabilities must be an array")
    try:
        supported = frozenset(Capability(item) for item in capabilities)
    except (TypeError, ValueError):
        raise ValueError("handshake contains an unknown capability") from None
    return HandshakeResult(
        daemon_version=_identifier(data["daemon_version"], "daemon version"),
        core_version=_identifier(data["core_version"], "core version"),
        selected_protocol_version=_identifier(
            data["selected_protocol_version"],
            "selected protocol version",
        ),
        daemon_capabilities=supported,
        compatibility_status=_identifier(
            data["compatibility_status"],
            "compatibility status",
        ),
        server_instance_id=_identifier(
            data["server_instance_id"],
            "server instance id",
        ),
    )


def _groups_to_wire(groups: Iterable[GroupReference]) -> list:
    return [{"id": item.id, "name": item.name} for item in groups]


def _groups_from_wire(value: Any) -> tuple:
    if type(value) is not list:
        raise ValueError("connection groups must be an array")
    groups = []
    for item in value:
        data = _strict_fields(
            item,
            required={"id", "name"},
            context="connection group",
        )
        groups.append(
            GroupReference(
                id=_identifier(data["id"], "group id"),
                name=_text(data["name"], "group name", allow_empty=True),
            )
        )
    return tuple(groups)


def connection_summary_to_wire(summary: ConnectionSummary) -> Dict[str, Any]:
    return {
        "id": summary.id,
        "nickname": summary.nickname,
        "host": summary.host,
        "hostname": summary.hostname,
        "username": summary.username,
        "port": summary.port,
        "protocol": summary.protocol,
        "health": summary.health.value,
        "groups": _groups_to_wire(summary.groups),
    }


_SUMMARY_FIELDS = {
    "id",
    "nickname",
    "host",
    "hostname",
    "username",
    "port",
    "protocol",
    "health",
    "groups",
}


def connection_summary_from_wire(value: Any) -> ConnectionSummary:
    data = _strict_fields(
        value,
        required=_SUMMARY_FIELDS,
        context="connection summary",
    )
    try:
        health = ConnectionHealth(data["health"])
    except (TypeError, ValueError):
        raise ValueError("connection summary contains unknown health state") from None
    return ConnectionSummary(
        id=ConnectionId(_identifier(data["id"], "connection id")),
        nickname=_identifier(data["nickname"], "connection nickname"),
        host=_text(data["host"], "connection host", allow_empty=True),
        hostname=_text(
            data["hostname"],
            "connection hostname",
            allow_empty=True,
        ),
        username=_text(
            data["username"],
            "connection username",
            allow_empty=True,
        ),
        port=_integer(data["port"], "connection port"),
        protocol=_identifier(data["protocol"], "connection protocol"),
        health=health,
        groups=_groups_from_wire(data["groups"]),
    )


def connection_details_to_wire(details: ConnectionDetails) -> Dict[str, Any]:
    result = connection_summary_to_wire(details)
    result.update(
        {
            "aliases": list(details.aliases),
            "authentication_method": details.authentication_method.value,
            "identity_configured": details.identity_configured,
            "certificate_configured": details.certificate_configured,
            "x11_forwarding": details.x11_forwarding,
            "forwarding_rule_count": details.forwarding_rule_count,
            "proxy_jump": list(details.proxy_jump),
        }
    )
    return result


def connection_details_from_wire(value: Any) -> ConnectionDetails:
    detail_fields = {
        "aliases",
        "authentication_method",
        "identity_configured",
        "certificate_configured",
        "x11_forwarding",
        "forwarding_rule_count",
        "proxy_jump",
    }
    data = _strict_fields(
        value,
        required=_SUMMARY_FIELDS | detail_fields,
        context="connection details",
    )
    summary = connection_summary_from_wire({key: data[key] for key in _SUMMARY_FIELDS})
    if type(data["aliases"]) is not list or type(data["proxy_jump"]) is not list:
        raise ValueError("connection aliases and proxy jump must be arrays")
    aliases = tuple(_identifier(item, "connection alias") for item in data["aliases"])
    proxy_jump = tuple(_identifier(item, "proxy jump host") for item in data["proxy_jump"])
    try:
        authentication_method = AuthenticationMethod(data["authentication_method"])
    except (TypeError, ValueError):
        raise ValueError("connection details contain unknown authentication method") from None
    return ConnectionDetails(
        id=summary.id,
        nickname=summary.nickname,
        host=summary.host,
        hostname=summary.hostname,
        username=summary.username,
        port=summary.port,
        protocol=summary.protocol,
        health=summary.health,
        groups=summary.groups,
        aliases=aliases,
        authentication_method=authentication_method,
        identity_configured=_boolean(
            data["identity_configured"],
            "identity configured",
        ),
        certificate_configured=_boolean(
            data["certificate_configured"],
            "certificate configured",
        ),
        x11_forwarding=_boolean(data["x11_forwarding"], "X11 forwarding"),
        forwarding_rule_count=_integer(
            data["forwarding_rule_count"],
            "forwarding rule count",
        ),
        proxy_jump=proxy_jump,
    )


def create_connection_request_to_wire(
    request: CreateConnectionRequest,
) -> Dict[str, Any]:
    if type(request) is not CreateConnectionRequest:
        raise TypeError("create connection request is required")
    return {
        "nickname": request.nickname,
        "hostname": request.hostname,
        "username": request.username,
        "port": request.port,
        "protocol": request.protocol,
    }


def create_connection_request_from_wire(value: Any) -> CreateConnectionRequest:
    data = _strict_fields(
        value,
        required={"nickname", "hostname", "username", "port", "protocol"},
        context="create connection request",
    )
    return CreateConnectionRequest(
        nickname=_identifier(data["nickname"], "connection nickname"),
        hostname=_identifier(data["hostname"], "connection hostname"),
        username=_text(
            data["username"],
            "connection username",
            allow_empty=True,
        ),
        port=_integer(data["port"], "connection port"),
        protocol=_identifier(data["protocol"], "connection protocol"),
    )


def update_connection_request_to_wire(
    request: UpdateConnectionRequest,
) -> Dict[str, Any]:
    if type(request) is not UpdateConnectionRequest:
        raise TypeError("update connection request is required")
    return {
        "nickname": request.nickname,
        "hostname": request.hostname,
        "username": request.username,
        "port": request.port,
    }


def update_connection_request_from_wire(value: Any) -> UpdateConnectionRequest:
    data = _strict_fields(
        value,
        required={"nickname", "hostname", "username", "port"},
        context="update connection request",
    )
    nickname = data["nickname"]
    hostname = data["hostname"]
    username = data["username"]
    port = data["port"]
    return UpdateConnectionRequest(
        nickname=(_identifier(nickname, "connection nickname") if nickname is not None else None),
        hostname=(_identifier(hostname, "connection hostname") if hostname is not None else None),
        username=(
            _text(username, "connection username", allow_empty=True)
            if username is not None
            else None
        ),
        port=(_integer(port, "connection port") if port is not None else None),
    )


def delete_connection_request_to_wire(
    request: DeleteConnectionRequest,
) -> Dict[str, Any]:
    if type(request) is not DeleteConnectionRequest:
        raise TypeError("delete connection request is required")
    return {"connection_id": request.connection_id}


def delete_connection_request_from_wire(value: Any) -> DeleteConnectionRequest:
    data = _strict_fields(
        value,
        required={"connection_id"},
        context="delete connection request",
    )
    return DeleteConnectionRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id"))
    )


def delete_connection_result_to_wire(
    result: DeleteConnectionResult,
) -> Dict[str, Any]:
    if type(result) is not DeleteConnectionResult:
        raise TypeError("delete connection result is required")
    return {
        "connection_id": result.connection_id,
        "deleted": result.deleted,
    }


def delete_connection_result_from_wire(value: Any) -> DeleteConnectionResult:
    data = _strict_fields(
        value,
        required={"connection_id", "deleted"},
        context="delete connection result",
    )
    return DeleteConnectionResult(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        deleted=_boolean(data["deleted"], "connection deleted result"),
    )


def _datetime_to_wire(value: datetime, context: str) -> str:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(f"{context} must be an aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime_from_wire(value: Any, context: str) -> datetime:
    text = _identifier(value, context)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{context} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{context} must include a timezone")
    return parsed.astimezone(timezone.utc)


def session_exit_info_to_wire(exit_info: SessionExitInfo) -> Dict[str, Any]:
    if type(exit_info) is not SessionExitInfo:
        raise TypeError("session exit information is required")
    return {
        "exit_code": exit_info.exit_code,
        "signal": exit_info.signal,
        "reason": exit_info.reason,
    }


def session_exit_info_from_wire(value: Any) -> SessionExitInfo:
    data = _strict_fields(
        value,
        required={"exit_code", "signal", "reason"},
        context="session exit information",
    )
    exit_code = data["exit_code"]
    signal = data["signal"]
    if exit_code is not None:
        exit_code = _integer(exit_code, "session exit code")
    if signal is not None:
        signal = _integer(signal, "session exit signal")
    return SessionExitInfo(
        exit_code=exit_code,
        signal=signal,
        reason=_text(data["reason"], "session exit reason", allow_empty=True),
    )


def session_summary_to_wire(summary: SessionSummary) -> Dict[str, Any]:
    if type(summary) is not SessionSummary:
        raise TypeError("session summary is required")
    input_owner = summary.input_owner
    failure = summary.failure
    return {
        "id": summary.id,
        "connection_id": summary.connection_id,
        "state": summary.state.value,
        "created_at": _datetime_to_wire(summary.created_at, "session creation time"),
        "input_owner": (
            {
                "client_id": input_owner.client_id,
                "attachment_id": input_owner.attachment_id,
            }
            if input_owner is not None
            else None
        ),
        "capabilities": sorted(summary.capabilities.supported),
        "exit_info": (
            session_exit_info_to_wire(summary.exit_info) if summary.exit_info is not None else None
        ),
        "failure": (
            {"code": failure.code, "message": failure.message} if failure is not None else None
        ),
        "attachment_count": summary.attachment_count,
    }


def session_summary_from_wire(value: Any) -> SessionSummary:
    data = _strict_fields(
        value,
        required={
            "id",
            "connection_id",
            "state",
            "created_at",
            "input_owner",
            "capabilities",
            "exit_info",
            "failure",
            "attachment_count",
        },
        context="session summary",
    )
    try:
        state = SessionState(data["state"])
    except (TypeError, ValueError):
        raise ValueError("session summary contains an unknown state") from None
    owner_data = data["input_owner"]
    owner = None
    if owner_data is not None:
        owner_fields = _strict_fields(
            owner_data,
            required={"client_id", "attachment_id"},
            context="session input owner",
        )
        owner = InputOwner(
            client_id=ClientId(_identifier(owner_fields["client_id"], "input owner client id")),
            attachment_id=AttachmentId(
                _identifier(
                    owner_fields["attachment_id"],
                    "input owner attachment id",
                )
            ),
        )
    supported = data["capabilities"]
    if type(supported) is not list:
        raise ValueError("session capabilities must be an array")
    capabilities = SessionCapabilities(
        supported=frozenset(_identifier(item, "session capability") for item in supported)
    )
    exit_info = (
        session_exit_info_from_wire(data["exit_info"]) if data["exit_info"] is not None else None
    )
    failure_data = data["failure"]
    failure = None
    if failure_data is not None:
        failure_fields = _strict_fields(
            failure_data,
            required={"code", "message"},
            context="session failure",
        )
        failure = SessionFailure(
            code=_identifier(failure_fields["code"], "session failure code"),
            message=_identifier(
                failure_fields["message"],
                "session failure message",
            ),
        )
    return SessionSummary(
        id=_session_id(data["id"], "session id"),
        connection_id=ConnectionId(_identifier(data["connection_id"], "session connection id")),
        state=state,
        created_at=_datetime_from_wire(
            data["created_at"],
            "session creation time",
        ),
        input_owner=owner,
        capabilities=capabilities,
        exit_info=exit_info,
        failure=failure,
        attachment_count=_integer(
            data["attachment_count"],
            "session attachment count",
        ),
    )


def open_session_request_to_wire(request: OpenSessionRequest) -> Dict[str, Any]:
    if type(request) is not OpenSessionRequest:
        raise TypeError("open session request is required")
    return {
        "connection_id": request.connection_id,
    }


def open_session_request_from_wire(value: Any) -> OpenSessionRequest:
    data = _strict_fields(
        value,
        required={"connection_id"},
        context="open session request",
    )
    return OpenSessionRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
    )


def attach_session_request_to_wire(request: AttachSessionRequest) -> Dict[str, Any]:
    if type(request) is not AttachSessionRequest:
        raise TypeError("attach session request is required")
    return {
        "session_id": request.session_id,
        "request_input": request.request_input,
    }


def attach_session_request_from_wire(value: Any) -> AttachSessionRequest:
    data = _strict_fields(
        value,
        required={"session_id", "request_input"},
        context="attach session request",
    )
    return AttachSessionRequest(
        session_id=_session_id(data["session_id"], "session id"),
        request_input=_boolean(data["request_input"], "request input"),
    )


def attachment_info_to_wire(info: AttachmentInfo) -> Dict[str, Any]:
    if type(info) is not AttachmentInfo:
        raise TypeError("attachment information is required")
    return {
        "id": info.id,
        "session_id": info.session_id,
        "client_id": info.client_id,
        "input_owner": info.input_owner,
    }


def attachment_info_from_wire(value: Any) -> AttachmentInfo:
    data = _strict_fields(
        value,
        required={"id", "session_id", "client_id", "input_owner"},
        context="attachment information",
    )
    return AttachmentInfo(
        id=AttachmentId(_identifier(data["id"], "attachment id")),
        session_id=_session_id(data["session_id"], "session id"),
        client_id=ClientId(_identifier(data["client_id"], "client id")),
        input_owner=_boolean(data["input_owner"], "attachment input owner"),
    )


def attach_session_result_to_wire(result: AttachSessionResult) -> Dict[str, Any]:
    if type(result) is not AttachSessionResult:
        raise TypeError("attach session result is required")
    return {
        "session": session_summary_to_wire(result.session),
        "attachment": attachment_info_to_wire(result.attachment),
    }


def attach_session_result_from_wire(value: Any) -> AttachSessionResult:
    data = _strict_fields(
        value,
        required={"session", "attachment"},
        context="attach session result",
    )
    return AttachSessionResult(
        session=session_summary_from_wire(data["session"]),
        attachment=attachment_info_from_wire(data["attachment"]),
    )


def detach_session_request_to_wire(request: DetachSessionRequest) -> Dict[str, Any]:
    if type(request) is not DetachSessionRequest:
        raise TypeError("detach session request is required")
    return {
        "session_id": request.session_id,
        "attachment_id": request.attachment_id,
    }


def detach_session_request_from_wire(value: Any) -> DetachSessionRequest:
    data = _strict_fields(
        value,
        required={"session_id", "attachment_id"},
        context="detach session request",
    )
    return DetachSessionRequest(
        session_id=_session_id(data["session_id"], "session id"),
        attachment_id=AttachmentId(_identifier(data["attachment_id"], "attachment id")),
    )


def close_session_request_to_wire(request: CloseSessionRequest) -> Dict[str, Any]:
    if type(request) is not CloseSessionRequest:
        raise TypeError("close session request is required")
    return {"session_id": request.session_id}


def close_session_request_from_wire(value: Any) -> CloseSessionRequest:
    data = _strict_fields(
        value,
        required={"session_id"},
        context="close session request",
    )
    return CloseSessionRequest(session_id=_session_id(data["session_id"], "session id"))


def capabilities_to_wire(capabilities: Capabilities) -> Dict[str, Any]:
    return {
        "protocol_version": capabilities.protocol_version,
        "api_implementation_version": capabilities.api_implementation_version,
        "client": {
            "name": capabilities.client.name,
            "version": capabilities.client.version,
            "client_id": capabilities.client.client_id,
        },
        "core": {
            "name": capabilities.core.name,
            "version": capabilities.core.version,
            "implementation": capabilities.core.implementation,
        },
        "supported": sorted(item.value for item in capabilities.supported),
        "compatibility": {
            "compatible": capabilities.compatibility.compatible,
            "protocol_version": capabilities.compatibility.protocol_version,
            "message": capabilities.compatibility.message,
        },
    }


def capabilities_from_wire(value: Any) -> Capabilities:
    data = _strict_fields(
        value,
        required={
            "protocol_version",
            "api_implementation_version",
            "client",
            "core",
            "supported",
            "compatibility",
        },
        context="capabilities",
    )
    client = _strict_fields(
        data["client"],
        required={"name", "version", "client_id"},
        context="capabilities client",
    )
    core = _strict_fields(
        data["core"],
        required={"name", "version", "implementation"},
        context="capabilities core",
    )
    compatibility = _strict_fields(
        data["compatibility"],
        required={"compatible", "protocol_version", "message"},
        context="capabilities compatibility",
    )
    if type(data["supported"]) is not list:
        raise ValueError("supported capabilities must be an array")
    try:
        supported = frozenset(Capability(item) for item in data["supported"])
    except (TypeError, ValueError):
        raise ValueError("capabilities contain an unknown capability") from None
    return Capabilities(
        protocol_version=_identifier(data["protocol_version"], "protocol version"),
        api_implementation_version=_identifier(
            data["api_implementation_version"],
            "API implementation version",
        ),
        client=ClientInfo(
            name=_identifier(client["name"], "client name"),
            version=_identifier(client["version"], "client version"),
            client_id=(
                ClientId(_identifier(client["client_id"], "client id"))
                if client["client_id"] is not None
                else None
            ),
        ),
        core=CoreInfo(
            name=_identifier(core["name"], "core name"),
            version=_identifier(core["version"], "core version"),
            implementation=_identifier(
                core["implementation"],
                "core implementation",
            ),
        ),
        supported=supported,
        compatibility=CompatibilityResult(
            compatible=_boolean(
                compatibility["compatible"],
                "compatibility result",
            ),
            protocol_version=_identifier(
                compatibility["protocol_version"],
                "compatibility protocol version",
            ),
            message=_text(
                compatibility["message"],
                "compatibility message",
                allow_empty=True,
            ),
        ),
    )
