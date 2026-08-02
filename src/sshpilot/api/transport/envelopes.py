"""Socket-independent models for Protocol v1 message envelopes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping, Optional, Tuple

from .._safe_values import copy_safe_details, copy_transport_value
from ..capabilities import Capability
from ..errors import ErrorCode
from ..models.common import (
    ClientId,
    ConnectionId,
    RequestId,
    SessionId,
    require_identifier,
)


def _safe_mapping(value: Mapping[str, Any], field_name: str) -> dict:
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be a dictionary")
    try:
        return copy_transport_value(value, field_name)
    except (TypeError, ValueError) as exc:
        raise type(exc)(f"{field_name} is not a safe transport mapping") from None


def _safe_detail_mapping(value: Mapping[str, Any], field_name: str) -> dict:
    if type(value) is not dict:
        raise TypeError(f"{field_name} must be a dictionary")
    return copy_safe_details(value)


def _safe_value(value: Any, field_name: str) -> Any:
    try:
        return copy_transport_value(value, field_name)
    except (TypeError, ValueError) as exc:
        raise type(exc)(f"{field_name} is not a safe transport value") from None


@dataclass(frozen=True)
class RequestEnvelope:
    """One explicitly named daemon method invocation."""

    protocol_version: str
    request_id: RequestId
    method: str
    params: Mapping[str, Any] = field(default_factory=dict, repr=False)
    client_id: ClientId = ClientId("client:unknown")

    def __post_init__(self) -> None:
        require_identifier(self.protocol_version, "protocol version")
        require_identifier(self.request_id, "request id")
        require_identifier(self.method, "method")
        require_identifier(self.client_id, "client id")
        object.__setattr__(self, "params", _safe_mapping(self.params, "request params"))


@dataclass(frozen=True)
class SuccessResponseEnvelope:
    """A successful response correlated to exactly one request."""

    protocol_version: str
    request_id: RequestId
    result: Any = field(repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.protocol_version, "protocol version")
        require_identifier(self.request_id, "request id")
        object.__setattr__(self, "result", _safe_value(self.result, "response result"))


@dataclass(frozen=True)
class ErrorData:
    """Wire-safe representation of :class:`SshPilotError`."""

    code: ErrorCode
    message: str
    details: Mapping[str, Any] = field(default_factory=dict, repr=False)
    retryable: bool = False
    request_id: Optional[RequestId] = None
    connection_id: Optional[ConnectionId] = None
    session_id: Optional[SessionId] = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, ErrorCode):
            raise TypeError("wire error code must be an ErrorCode")
        require_identifier(self.message, "wire error message")
        if type(self.retryable) is not bool:
            raise TypeError("wire error retryable must be a boolean")
        for field_name, value in (
            ("request id", self.request_id),
            ("connection id", self.connection_id),
            ("session id", self.session_id),
        ):
            if value is not None:
                require_identifier(value, field_name)
        object.__setattr__(self, "details", _safe_detail_mapping(self.details, "error details"))


@dataclass(frozen=True)
class ErrorResponseEnvelope:
    """A structured failed response correlated to one request."""

    protocol_version: str
    request_id: RequestId
    error: ErrorData = field(repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.protocol_version, "protocol version")
        require_identifier(self.request_id, "request id")
        if not isinstance(self.error, ErrorData):
            raise TypeError("error response must contain ErrorData")


@dataclass(frozen=True)
class EventEnvelope:
    """An unsolicited typed public event frame.

    Protocol v1 currently carries typed connection and session lifecycle
    events. Terminal bytes, prompts, and replay remain outside this envelope.
    """

    protocol_version: str
    event: str
    sequence: int
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.protocol_version, "protocol version")
        require_identifier(self.event, "event")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("event sequence must be a non-negative integer")
        object.__setattr__(self, "payload", _safe_mapping(self.payload, "event payload"))


@dataclass(frozen=True)
class HandshakeRequest:
    """Validated client metadata supplied before ordinary RPC methods."""

    client_name: str
    client_version: str
    supported_protocol_versions: Tuple[str, ...]
    client_capabilities: FrozenSet[str] = frozenset()
    frontend_type: Optional[str] = None
    supported_frame_types: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        require_identifier(self.client_name, "client name")
        require_identifier(self.client_version, "client version")
        if (
            type(self.supported_protocol_versions) is not tuple
            or not self.supported_protocol_versions
        ):
            raise ValueError("supported protocol versions must be a non-empty tuple")
        for version in self.supported_protocol_versions:
            require_identifier(version, "supported protocol version")
        if type(self.client_capabilities) is not frozenset:
            raise TypeError("client capabilities must be a frozenset")
        for capability in self.client_capabilities:
            require_identifier(capability, "client capability")
        if self.frontend_type is not None:
            require_identifier(self.frontend_type, "frontend type")
        if type(self.supported_frame_types) is not frozenset:
            raise TypeError("supported frame types must be a frozenset")
        for frame_type in self.supported_frame_types:
            require_identifier(frame_type, "supported frame type")


@dataclass(frozen=True)
class HandshakeResult:
    """Daemon identity and the selected Protocol v1 contract."""

    daemon_version: str
    core_version: str
    selected_protocol_version: str
    daemon_capabilities: FrozenSet[Capability]
    compatibility_status: str
    server_instance_id: str
    # Optional development-safe metadata (empty when unset). Never carries
    # repository paths — only an opaque revision token and UTC start time.
    daemon_started_at: str = ""
    development_revision: str = ""
    api_implementation_version: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.daemon_version, "daemon version")
        require_identifier(self.core_version, "core version")
        require_identifier(self.selected_protocol_version, "selected protocol version")
        require_identifier(self.compatibility_status, "compatibility status")
        require_identifier(self.server_instance_id, "server instance id")
        if type(self.daemon_capabilities) is not frozenset or any(
            not isinstance(item, Capability) for item in self.daemon_capabilities
        ):
            raise TypeError("daemon capabilities must contain Capability values")
        if type(self.daemon_started_at) is not str:
            raise TypeError("daemon_started_at must be a string")
        if type(self.development_revision) is not str:
            raise TypeError("development_revision must be a string")
        if type(self.api_implementation_version) is not str:
            raise TypeError("api_implementation_version must be a string")
