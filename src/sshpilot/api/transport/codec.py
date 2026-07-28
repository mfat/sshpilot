"""Strict Protocol v1 envelope and public-DTO JSON codecs."""

from __future__ import annotations

from typing import AbstractSet, Any, Dict, Iterable, Union

from .._safe_values import copy_safe_details
from ..capabilities import Capabilities, Capability
from ..errors import ErrorCode, SshPilotError
from ..models.common import (
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
    GroupReference,
)
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
            ConnectionId(error.connection_id)
            if error.connection_id is not None
            else None
        ),
        session_id=(
            SessionId(error.session_id)
            if error.session_id is not None
            else None
        ),
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
    return ErrorData(
        code=code,
        message=_identifier(data["message"], "error message"),
        details=data["details"],
        retryable=data["retryable"],
        request_id=data.get("request_id"),
        connection_id=data.get("connection_id"),
        session_id=data.get("session_id"),
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
        return EventEnvelope(
            protocol_version=data["protocol_version"],
            event=data["event"],
            sequence=data["sequence"],
            payload=data["payload"],
        )
    raise ValueError("transport envelope type is unknown")


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
    summary = connection_summary_from_wire(
        {key: data[key] for key in _SUMMARY_FIELDS}
    )
    if type(data["aliases"]) is not list or type(data["proxy_jump"]) is not list:
        raise ValueError("connection aliases and proxy jump must be arrays")
    aliases = tuple(_identifier(item, "connection alias") for item in data["aliases"])
    proxy_jump = tuple(
        _identifier(item, "proxy jump host") for item in data["proxy_jump"]
    )
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
