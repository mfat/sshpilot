"""Strict Protocol v1 envelope and public-DTO JSON codecs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AbstractSet, Any, Dict, Iterable, Mapping, Optional, Union

from .._safe_values import copy_transport_value
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
    ForwardId,
    InteractionId,
    SessionId,
    SftpServiceId,
    TransferId,
)
from ..models.interactions import (
    ChallengePrompt,
    ConfirmationPrompt,
    HostKeyDecision,
    HostKeyPrompt,
    HostKeyStatus,
    InteractionClaim,
    InteractionDecisionRequest,
    InteractionState,
    InteractionSummary,
    InteractionType,
    PassphrasePrompt,
    PasswordPrompt,
    PresencePrompt,
    RememberPolicy,
    SecretDecision,
)
from ..models.known_hosts import (
    KnownHostEntryId,
    KnownHostEntrySummary,
    KnownHostsMutationResult,
    KnownHostsSnapshot,
    RemoveKnownHostEntriesRequest,
)
from ..models.keys import (
    DeleteKeyRequest,
    DeleteKeyResult,
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    ListKeysRequest,
    PublicKeyResult,
    ReadPublicKeyRequest,
    VerifyKeyPassphraseRequest,
    VerifyKeyPassphraseResult,
)
from ..models.connection_store import (
    AddTagToConnectionsRequest,
    ConnectionMetadataSummary,
    ConnectionPlacementMode,
    ConnectionStoreSnapshot,
    CopyConnectionToGroupRequest,
    MoveConnectionsRequest,
    GroupId,
    GroupSummary,
    PlaceGroupRequest,
    RemoveConnectionFromGroupRequest,
    RenameTagRequest,
    ReorderConnectionRequest,
    SetGroupColorRequest,
    thaw_safe_metadata,
    validate_safe_metadata,
)
from ..models.connections import (
    EDITABLE_CONFIG_FIELDS,
    AuthenticationMethod,
    AssignConnectionToGroupRequest,
    ConnectionDetails,
    ConnectionEditorCapabilities,
    ConnectionEditorDetails,
    EffectiveConfigComparison,
    UnsavedHostCheckRequest,
    UnsavedHostCheckResult,
    ConnectionHealth,
    ConnectionSummary,
    ConnectionValidationError,
    ConnectionValidationResult,
    CreateConnectionRequest,
    CreateGroupRequest,
    DeleteConnectionPasswordRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    DeleteGroupRequest,
    ForwardingRule,
    GroupReference,
    LookupKeyPassphraseRequest,
    RenameGroupRequest,
    SaveSshConfigTextRequest,
    SplitConnectionRequest,
    SshConfigText,
    StoreConnectionPasswordRequest,
    SetSessionConnectionPasswordRequest,
    DeleteKeyPassphraseRequest,
    StoreKeyPassphraseRequest,
    UNSET,
    UpdateConnectionMetadataRequest,
    UpdateConnectionRequest,
)
from ..models.daemon import (
    DaemonDiagnostics,
    DaemonIdleInfo,
    DaemonLifecycleState,
    DaemonLogLevel,
    DaemonResourceCounts,
    DaemonStatus,
    DaemonStopResult,
    RestartDaemonRequest,
    SetDaemonLogLevelRequest,
    OperationMode,
    OperationModeFiles,
    OperationModeResult,
    SetOperationModeRequest,
    StopDaemonRequest,
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
from ..models.terminal import (
    BroadcastTerminalInputRequest,
    ClaimTerminalInputRequest,
    ExternalTerminalLaunchSpec,
    ReleaseTerminalInputRequest,
    ReplayBounds,
    ReplayRequest,
    ReplayResult,
    ResizeTerminalRequest,
    TerminalDimensions,
)
from ..models.operations import (
    AttachSftpRequest,
    ClaimForwardRequest,
    CloseSftpRequest,
    ForwardSummary,
    ForwardType,
    ForwardState,
    CloseForwardRequest,
    ListDirectoryRequest,
    ListDirectoryResult,
    OpenForwardRequest,
    OpenSftpRequest,
    RemoteFileEntry,
    RemoteFileType,
    ServiceFailure,
    SftpChmodRequest,
    SftpCopyRequest,
    SftpCreateFileRequest,
    SftpCreateFileResult,
    SftpDirectorySizeRequest,
    SftpDirectorySizeResult,
    SftpFileAccess,
    SftpFileTarget,
    SftpPathRequest,
    SftpReadFileRequest,
    SftpReadFileResult,
    SftpRenameRequest,
    SftpReplaceFileRequest,
    SftpReplaceFileResult,
    SftpServiceState,
    SftpServiceSummary,
    SftpSymlinkRequest,
)
from ..models.transfers import (
    CancelTransferRequest,
    StartScpTransferRequest,
    StartTransferRequest,
    TransferBackend,
    TransferConflictPolicy,
    TransferDirection,
    TransferLocalMode,
    TransferState,
    TransferSummary,
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


def _interaction_scope_id(value: Any, context: str) -> SessionId:
    """Parse interaction scope ids used for eligibility (session/sftp/forward)."""
    return SessionId(_identifier(value, context))


def _session_id(value: Any, context: str) -> SessionId:
    return SessionId(_identifier(value, context))


def _sftp_service_id(value: Any, context: str) -> SftpServiceId:
    return SftpServiceId(_identifier(value, context))


def _transfer_id(value: Any, context: str) -> TransferId:
    return TransferId(_identifier(value, context))


def _forward_id(value: Any, context: str) -> ForwardId:
    return ForwardId(_identifier(value, context))


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
        return copy_transport_value({"value": value})["value"]
    if type(value) is list:
        return [_json_value(item, f"{context}[]") for item in value]
    if type(value) is dict:
        return copy_transport_value(value)
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
        EventType.CONNECTION_STORE_CHANGED,
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
_INTERACTION_EVENT_TYPES = frozenset(
    {
        EventType.INTERACTION_CREATED,
        EventType.INTERACTION_STATE_CHANGED,
    }
)
_SFTP_EVENT_TYPES = frozenset(
    {
        EventType.SFTP_CREATED,
        EventType.SFTP_STATE_CHANGED,
        EventType.SFTP_CLOSED,
        EventType.SFTP_FAILED,
    }
)
_TRANSFER_EVENT_TYPES = frozenset(
    {
        EventType.TRANSFER_CREATED,
        EventType.TRANSFER_STARTED,
        EventType.TRANSFER_PROGRESS,
        EventType.TRANSFER_ITEM_COMPLETED,
        EventType.TRANSFER_COMPLETED,
        EventType.TRANSFER_CANCELLED,
        EventType.TRANSFER_FAILED,
    }
)
_FORWARD_EVENT_TYPES = frozenset(
    {
        EventType.FORWARD_CREATED,
        EventType.FORWARD_STARTING,
        EventType.FORWARD_ACTIVE,
        EventType.FORWARD_CLOSED,
        EventType.FORWARD_FAILED,
    }
)
_DAEMON_EVENT_TYPES = frozenset(
    {
        EventType.DAEMON_STATE_CHANGED,
    }
)
_OPERATION_EVENT_TYPES = frozenset(
    {
        EventType.OPERATION_CREATED,
        EventType.OPERATION_STATE_CHANGED,
    }
)
_BROADCAST_EVENT_TYPES = frozenset({EventType.BROADCAST_OUTPUT})
_FORWARDED_EVENT_TYPES = (
    _CONNECTION_EVENT_TYPES
    | _SESSION_EVENT_TYPES
    | _INTERACTION_EVENT_TYPES
    | _SFTP_EVENT_TYPES
    | _TRANSFER_EVENT_TYPES
    | _FORWARD_EVENT_TYPES
    | _DAEMON_EVENT_TYPES
    | _OPERATION_EVENT_TYPES
    | _BROADCAST_EVENT_TYPES
)


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
        if event.type is EventType.CONNECTION_STORE_CHANGED:
            if type(event.payload) is not ConnectionStoreSnapshot:
                raise TypeError("connection store event payload must be ConnectionStoreSnapshot")
            payload = connection_store_snapshot_to_wire(event.payload)
        else:
            if type(event.payload) is not ConnectionSummary:
                raise TypeError("connection event payload must be ConnectionSummary")
            payload = connection_summary_to_wire(event.payload)
    elif event.type in _INTERACTION_EVENT_TYPES:
        if type(event.payload) is not InteractionSummary:
            raise TypeError("interaction event payload must be InteractionSummary")
        payload = interaction_summary_to_wire(event.payload)
    elif event.type is EventType.SESSION_EXITED:
        if type(event.payload) is not SessionExitInfo:
            raise TypeError("session exited payload must be SessionExitInfo")
        if event.session_id is None:
            raise TypeError("session exited event requires a session id")
        payload = {
            "session_id": event.session_id,
            "exit_info": session_exit_info_to_wire(event.payload),
        }
    elif event.type in _SFTP_EVENT_TYPES:
        if type(event.payload) is not SftpServiceSummary:
            raise TypeError("SFTP event payload must be SftpServiceSummary")
        payload = sftp_service_summary_to_wire(event.payload)
    elif event.type in _TRANSFER_EVENT_TYPES:
        if type(event.payload) is not TransferSummary:
            raise TypeError("transfer event payload must be TransferSummary")
        payload = transfer_summary_to_wire(event.payload)
    elif event.type in _FORWARD_EVENT_TYPES:
        if type(event.payload) is not ForwardSummary:
            raise TypeError("forward event payload must be ForwardSummary")
        payload = forward_summary_to_wire(event.payload)
    elif event.type in _DAEMON_EVENT_TYPES:
        if type(event.payload) is not DaemonStatus:
            raise TypeError("daemon event payload must be DaemonStatus")
        payload = daemon_status_to_wire(event.payload)
    elif event.type in _OPERATION_EVENT_TYPES:
        from ..models.operations import OperationSummary

        if type(event.payload) is not OperationSummary:
            raise TypeError("operation event payload must be OperationSummary")
        payload = operation_summary_to_wire(event.payload)
    elif event.type in _BROADCAST_EVENT_TYPES:
        from ..models.broadcast import BroadcastCommandOutput

        if type(event.payload) is not BroadcastCommandOutput:
            raise TypeError("broadcast output event payload is invalid")
        payload = {
            "operation_id": event.payload.operation_id,
            "connection_id": event.payload.connection_id,
            "stream": event.payload.stream,
            "text": event.payload.text,
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
        if event_type is EventType.CONNECTION_STORE_CHANGED:
            snapshot = connection_store_snapshot_from_wire(dict(envelope.payload))
            return CoreEvent(
                type=event_type,
                payload=snapshot,
                sequence=envelope.sequence,
            )
        summary = connection_summary_from_wire(dict(envelope.payload))
        return CoreEvent(
            type=event_type,
            payload=summary,
            sequence=envelope.sequence,
            connection_id=summary.id,
        )
    if event_type in _INTERACTION_EVENT_TYPES:
        summary = interaction_summary_from_wire(dict(envelope.payload))
        return CoreEvent(
            type=event_type,
            payload=summary,
            sequence=envelope.sequence,
            connection_id=summary.connection_id,
            session_id=summary.session_id,
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
    if event_type in _SFTP_EVENT_TYPES:
        sftp_summary = sftp_service_summary_from_wire(dict(envelope.payload))
        return CoreEvent(
            type=event_type,
            payload=sftp_summary,
            sequence=envelope.sequence,
            connection_id=sftp_summary.connection_id,
            session_id=SessionId(str(sftp_summary.id)),
        )
    if event_type in _TRANSFER_EVENT_TYPES:
        transfer_summary = transfer_summary_from_wire(dict(envelope.payload))
        return CoreEvent(
            type=event_type,
            payload=transfer_summary,
            sequence=envelope.sequence,
            connection_id=transfer_summary.connection_id,
        )
    if event_type in _FORWARD_EVENT_TYPES:
        forward_summary = forward_summary_from_wire(dict(envelope.payload))
        return CoreEvent(
            type=event_type,
            payload=forward_summary,
            sequence=envelope.sequence,
            connection_id=forward_summary.connection_id,
            session_id=SessionId(str(forward_summary.id)),
        )
    if event_type in _DAEMON_EVENT_TYPES:
        status = daemon_status_from_wire(dict(envelope.payload))
        return CoreEvent(
            type=event_type,
            payload=status,
            sequence=envelope.sequence,
        )
    if event_type in _OPERATION_EVENT_TYPES:
        operation_summary = operation_summary_from_wire(dict(envelope.payload))
        return CoreEvent(
            type=event_type,
            payload=operation_summary,
            sequence=envelope.sequence,
            connection_id=operation_summary.connection_id,
        )
    if event_type in _BROADCAST_EVENT_TYPES:
        from ..models.broadcast import BroadcastCommandOutput

        data = _strict_fields(
            dict(envelope.payload),
            required={"operation_id", "connection_id", "stream", "text"},
            context="broadcast output event",
        )
        payload = BroadcastCommandOutput(
            operation_id=data["operation_id"],
            connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
            stream=data["stream"],
            text=data["text"],
        )
        return CoreEvent(
            type=event_type,
            payload=payload,
            sequence=envelope.sequence,
            connection_id=payload.connection_id,
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


def known_host_entry_summary_to_wire(
    summary: KnownHostEntrySummary,
) -> Dict[str, Any]:
    if type(summary) is not KnownHostEntrySummary:
        raise TypeError("known-hosts entry summary is required")
    return {
        "entry_id": summary.entry_id,
        "hostname": summary.hostname,
        "key_type": summary.key_type,
        "display_line": summary.display_line,
    }


def known_host_entry_summary_from_wire(value: Any) -> KnownHostEntrySummary:
    data = _strict_fields(
        value,
        required={"entry_id", "hostname", "key_type", "display_line"},
        context="known-hosts entry summary",
    )
    return KnownHostEntrySummary(
        entry_id=KnownHostEntryId(_identifier(data["entry_id"], "known-hosts entry id")),
        hostname=_text(data["hostname"], "known-hosts hostname", allow_empty=True),
        key_type=_text(data["key_type"], "known-hosts key type", allow_empty=True),
        display_line=_text(data["display_line"], "known-hosts display line"),
    )


def known_hosts_snapshot_to_wire(
    snapshot: KnownHostsSnapshot,
) -> Dict[str, Any]:
    if type(snapshot) is not KnownHostsSnapshot:
        raise TypeError("known-hosts snapshot is required")
    return {
        "revision": snapshot.revision,
        "entries": [known_host_entry_summary_to_wire(item) for item in snapshot.entries],
    }


def known_hosts_snapshot_from_wire(value: Any) -> KnownHostsSnapshot:
    data = _strict_fields(
        value,
        required={"revision", "entries"},
        context="known-hosts snapshot",
    )
    entries = data["entries"]
    if type(entries) is not list:
        raise ValueError("known-hosts entries must be an array")
    return KnownHostsSnapshot(
        revision=_identifier(data["revision"], "known-hosts revision"),
        entries=tuple(known_host_entry_summary_from_wire(item) for item in entries),
    )


def remove_known_host_entries_request_to_wire(
    request: RemoveKnownHostEntriesRequest,
) -> Dict[str, Any]:
    if type(request) is not RemoveKnownHostEntriesRequest:
        raise TypeError("remove known-hosts entries request is required")
    return {
        "revision": request.revision,
        "entry_ids": list(request.entry_ids),
    }


def remove_known_host_entries_request_from_wire(
    value: Any,
) -> RemoveKnownHostEntriesRequest:
    data = _strict_fields(
        value,
        required={"revision", "entry_ids"},
        context="remove known-hosts entries request",
    )
    entry_ids = data["entry_ids"]
    if type(entry_ids) is not list:
        raise ValueError("known-hosts entry ids must be an array")
    return RemoveKnownHostEntriesRequest(
        revision=_identifier(data["revision"], "known-hosts revision"),
        entry_ids=tuple(
            KnownHostEntryId(_identifier(item, "known-hosts entry id")) for item in entry_ids
        ),
    )


def known_hosts_mutation_result_to_wire(
    result: KnownHostsMutationResult,
) -> Dict[str, Any]:
    if type(result) is not KnownHostsMutationResult:
        raise TypeError("known-hosts mutation result is required")
    return {
        "revision": result.revision,
        "removed_count": result.removed_count,
        "entries": [known_host_entry_summary_to_wire(item) for item in result.entries],
    }


def known_hosts_mutation_result_from_wire(
    value: Any,
) -> KnownHostsMutationResult:
    data = _strict_fields(
        value,
        required={"revision", "removed_count", "entries"},
        context="known-hosts mutation result",
    )
    entries = data["entries"]
    if type(entries) is not list:
        raise ValueError("known-hosts entries must be an array")
    return KnownHostsMutationResult(
        revision=_identifier(data["revision"], "known-hosts revision"),
        removed_count=_integer(data["removed_count"], "known-hosts removed count"),
        entries=tuple(known_host_entry_summary_from_wire(item) for item in entries),
    )


def _key_store_scope(value: Any, context: str) -> KeyStoreScope:
    """Parse a semantic key-store scope; reject malformed or unknown values."""

    if type(value) is not str or value not in {"default", "isolated"}:
        raise ValueError(f"{context} must be a supported key store scope")
    return KeyStoreScope(value)


def key_summary_to_wire(summary: KeySummary) -> Dict[str, Any]:
    if type(summary) is not KeySummary:
        raise TypeError("key summary is required")
    return {
        "key_id": summary.key_id,
        "name": summary.name,
        "private_path": summary.private_path,
        "public_path": summary.public_path,
        "public_key_available": summary.public_key_available,
    }


def key_summary_from_wire(value: Any) -> KeySummary:
    data = _strict_fields(
        value,
        required={
            "key_id",
            "name",
            "private_path",
            "public_path",
            "public_key_available",
        },
        context="key summary",
    )
    return KeySummary(
        key_id=KeyId(_identifier(data["key_id"], "key id")),
        name=_text(data["name"], "key name"),
        private_path=_text(data["private_path"], "key private path"),
        public_path=_text(data["public_path"], "key public path"),
        public_key_available=_boolean(data["public_key_available"], "key public availability"),
    )


def list_keys_request_to_wire(request: ListKeysRequest) -> Dict[str, Any]:
    if type(request) is not ListKeysRequest:
        raise TypeError("list keys request is required")
    return {"scope": request.scope.value}


def list_keys_request_from_wire(value: Any) -> ListKeysRequest:
    data = _strict_fields(value, required={"scope"}, context="list keys request")
    return ListKeysRequest(scope=_key_store_scope(data["scope"], "key store scope"))


def delete_key_request_to_wire(request: DeleteKeyRequest) -> Dict[str, Any]:
    if type(request) is not DeleteKeyRequest:
        raise TypeError("delete key request is required")
    return {"key_id": request.key_id, "scope": request.scope.value}


def delete_key_request_from_wire(value: Any) -> DeleteKeyRequest:
    data = _strict_fields(
        value,
        required={"key_id", "scope"},
        context="delete key request",
    )
    return DeleteKeyRequest(
        key_id=KeyId(_identifier(data["key_id"], "key id")),
        scope=_key_store_scope(data["scope"], "key store scope"),
    )


def delete_key_result_to_wire(result: DeleteKeyResult) -> Dict[str, Any]:
    if type(result) is not DeleteKeyResult:
        raise TypeError("delete key result is required")
    return {"key_id": result.key_id, "deleted": result.deleted}


def delete_key_result_from_wire(value: Any) -> DeleteKeyResult:
    data = _strict_fields(
        value,
        required={"key_id", "deleted"},
        context="delete key result",
    )
    return DeleteKeyResult(
        key_id=KeyId(_identifier(data["key_id"], "key id")),
        deleted=_boolean(data["deleted"], "key deletion result"),
    )


def key_list_to_wire(key_list: KeyList) -> Dict[str, Any]:
    if type(key_list) is not KeyList:
        raise TypeError("key list is required")
    return {"keys": [key_summary_to_wire(item) for item in key_list.keys]}


def key_list_from_wire(value: Any) -> KeyList:
    data = _strict_fields(value, required={"keys"}, context="key list")
    keys = data["keys"]
    if type(keys) is not list:
        raise ValueError("key list entries must be an array")
    return KeyList(keys=tuple(key_summary_from_wire(item) for item in keys))


def read_public_key_request_to_wire(request: ReadPublicKeyRequest) -> Dict[str, Any]:
    if type(request) is not ReadPublicKeyRequest:
        raise TypeError("read public key request is required")
    return {"key_id": request.key_id, "scope": request.scope.value}


def read_public_key_request_from_wire(value: Any) -> ReadPublicKeyRequest:
    data = _strict_fields(
        value,
        required={"key_id", "scope"},
        context="read public key request",
    )
    return ReadPublicKeyRequest(
        key_id=KeyId(_identifier(data["key_id"], "key id")),
        scope=_key_store_scope(data["scope"], "key store scope"),
    )


def public_key_result_to_wire(result: PublicKeyResult) -> Dict[str, Any]:
    if type(result) is not PublicKeyResult:
        raise TypeError("public key result is required")
    return {"key_id": result.key_id, "text": result.text}


def public_key_result_from_wire(value: Any) -> PublicKeyResult:
    data = _strict_fields(
        value,
        required={"key_id", "text"},
        context="public key result",
    )
    return PublicKeyResult(
        key_id=KeyId(_identifier(data["key_id"], "key id")),
        text=_text(data["text"], "public key text"),
    )


def generate_key_request_to_wire(request: GenerateKeyRequest) -> Dict[str, Any]:
    if type(request) is not GenerateKeyRequest:
        raise TypeError("generate key request is required")
    payload: Dict[str, Any] = {
        "name": request.name,
        "key_type": request.key_type,
        "key_size": request.key_size,
        "comment": request.comment,
        "encrypted": request.encrypted,
        "scope": request.scope.value,
    }
    if request.interaction_scope_id is not None:
        payload["interaction_scope_id"] = request.interaction_scope_id
    return payload


def generate_key_request_from_wire(value: Any) -> GenerateKeyRequest:
    data = _strict_fields(
        value,
        required={
            "name",
            "key_type",
            "key_size",
            "comment",
            "encrypted",
            "scope",
        },
        optional={"interaction_scope_id"},
        context="generate key request",
    )
    interaction_scope_id = data.get("interaction_scope_id")
    return GenerateKeyRequest(
        name=_text(data["name"], "key name"),
        key_type=_text(data["key_type"], "key type"),
        key_size=_integer(data["key_size"], "key size"),
        comment=_text(data["comment"], "key comment", allow_empty=True),
        encrypted=_boolean(data["encrypted"], "encrypted key generation"),
        interaction_scope_id=(
            SessionId(_identifier(interaction_scope_id, "key interaction scope id"))
            if interaction_scope_id is not None
            else None
        ),
        scope=_key_store_scope(data["scope"], "key store scope"),
    )


def generate_key_result_to_wire(result: GenerateKeyResult) -> Dict[str, Any]:
    if type(result) is not GenerateKeyResult:
        raise TypeError("generate key result is required")
    return {"key": key_summary_to_wire(result.key)}


def generate_key_result_from_wire(value: Any) -> GenerateKeyResult:
    data = _strict_fields(value, required={"key"}, context="generate key result")
    return GenerateKeyResult(key=key_summary_from_wire(data["key"]))


def verify_key_passphrase_request_to_wire(
    request: VerifyKeyPassphraseRequest,
) -> Dict[str, Any]:
    if type(request) is not VerifyKeyPassphraseRequest:
        raise TypeError("verify key passphrase request is required")
    return {
        "key_path": request.key_path,
        "interaction_scope_id": request.interaction_scope_id,
    }


def verify_key_passphrase_request_from_wire(
    value: Any,
) -> VerifyKeyPassphraseRequest:
    data = _strict_fields(
        value,
        required={"key_path", "interaction_scope_id"},
        context="verify key passphrase request",
    )
    return VerifyKeyPassphraseRequest(
        key_path=_text(data["key_path"], "key path"),
        interaction_scope_id=SessionId(
            _identifier(data["interaction_scope_id"], "key interaction scope id")
        ),
    )


def verify_key_passphrase_result_to_wire(
    result: VerifyKeyPassphraseResult,
) -> Dict[str, Any]:
    if type(result) is not VerifyKeyPassphraseResult:
        raise TypeError("verify key passphrase result is required")
    return {"valid": result.valid}


def verify_key_passphrase_result_from_wire(
    value: Any,
) -> VerifyKeyPassphraseResult:
    data = _strict_fields(
        value,
        required={"valid"},
        context="verify key passphrase result",
    )
    return VerifyKeyPassphraseResult(
        valid=_boolean(data["valid"], "key passphrase verification result")
    )


def group_summary_to_wire(summary: GroupSummary) -> Dict[str, Any]:
    if type(summary) is not GroupSummary:
        raise TypeError("group summary is required")
    payload: Dict[str, Any] = {
        "id": summary.id,
        "name": summary.name,
        "order": summary.order,
        "color": summary.color,
        "connection_ids": list(summary.connection_ids),
    }
    if summary.parent_id is not None:
        payload["parent_id"] = summary.parent_id
    return payload


def group_summary_from_wire(value: Any) -> GroupSummary:
    data = _strict_fields(
        value,
        required={"id", "name", "order", "color", "connection_ids"},
        optional={"parent_id"},
        context="group summary",
    )
    connection_ids = data["connection_ids"]
    if type(connection_ids) is not list:
        raise ValueError("group connection ids must be an array")
    parent_id = data.get("parent_id")
    if parent_id is not None and type(parent_id) is not str:
        raise ValueError("group parent id must be a string or null")
    return GroupSummary(
        id=GroupId(_identifier(data["id"], "group id")),
        name=_text(data["name"], "group name"),
        parent_id=(
            GroupId(_identifier(parent_id, "group parent id")) if parent_id is not None else None
        ),
        order=_integer(data["order"], "group order"),
        color=_text(data["color"], "group color", allow_empty=True),
        connection_ids=tuple(
            ConnectionId(_identifier(item, "group connection id")) for item in connection_ids
        ),
    )


def connection_metadata_summary_to_wire(
    summary: ConnectionMetadataSummary,
) -> Dict[str, Any]:
    if type(summary) is not ConnectionMetadataSummary:
        raise TypeError("connection metadata summary is required")

    def thaw(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [thaw(item) for item in value]
        return value

    return {
        "connection_id": summary.connection_id,
        "values": thaw(summary.values),
    }


def connection_metadata_summary_from_wire(
    value: Any,
) -> ConnectionMetadataSummary:
    data = _strict_fields(
        value,
        required={"connection_id", "values"},
        context="connection metadata summary",
    )
    values = data["values"]
    if type(values) is not dict:
        raise ValueError("connection metadata values must be an object")
    return ConnectionMetadataSummary(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        values=validate_safe_metadata(values),
    )


def connection_store_snapshot_to_wire(
    snapshot: ConnectionStoreSnapshot,
) -> Dict[str, Any]:
    if type(snapshot) is not ConnectionStoreSnapshot:
        raise TypeError("connection store snapshot is required")
    return {
        "generation": snapshot.generation,
        "connections": [connection_summary_to_wire(item) for item in snapshot.connections],
        "groups": [group_summary_to_wire(item) for item in snapshot.groups],
        "root_connection_ids": list(snapshot.root_connection_ids),
        "metadata": [connection_metadata_summary_to_wire(item) for item in snapshot.metadata],
    }


def connection_store_snapshot_from_wire(value: Any) -> ConnectionStoreSnapshot:
    data = _strict_fields(
        value,
        required={"generation", "connections", "groups", "root_connection_ids", "metadata"},
        context="connection store snapshot",
    )
    connections = data["connections"]
    groups = data["groups"]
    root_ids = data["root_connection_ids"]
    metadata = data["metadata"]
    if type(connections) is not list:
        raise ValueError("connection store connections must be an array")
    if type(groups) is not list:
        raise ValueError("connection store groups must be an array")
    if type(root_ids) is not list:
        raise ValueError("connection store root ids must be an array")
    if type(metadata) is not list:
        raise ValueError("connection store metadata must be an array")
    return ConnectionStoreSnapshot(
        generation=_integer(data["generation"], "connection store generation"),
        connections=tuple(connection_summary_from_wire(item) for item in connections),
        groups=tuple(group_summary_from_wire(item) for item in groups),
        root_connection_ids=tuple(
            ConnectionId(_identifier(item, "root connection id")) for item in root_ids
        ),
        metadata=tuple(connection_metadata_summary_from_wire(item) for item in metadata),
    )


def set_group_color_request_to_wire(
    request: SetGroupColorRequest,
) -> Dict[str, Any]:
    if type(request) is not SetGroupColorRequest:
        raise TypeError("set group color request is required")
    return {"group_id": request.group_id, "color": request.color}


def set_group_color_request_from_wire(value: Any) -> SetGroupColorRequest:
    data = _strict_fields(
        value,
        required={"group_id", "color"},
        context="set group color request",
    )
    return SetGroupColorRequest(
        group_id=GroupId(_identifier(data["group_id"], "group id")),
        color=_text(data["color"], "group color", allow_empty=True),
    )


def place_group_request_to_wire(request: PlaceGroupRequest) -> Dict[str, Any]:
    if type(request) is not PlaceGroupRequest:
        raise TypeError("place group request is required")
    payload: Dict[str, Any] = {
        "group_id": request.group_id,
        "index": request.index,
    }
    if request.parent_id is not None:
        payload["parent_id"] = request.parent_id
    if request.expected_generation is not None:
        payload["expected_generation"] = request.expected_generation
    return payload


def place_group_request_from_wire(value: Any) -> PlaceGroupRequest:
    data = _strict_fields(
        value,
        required={"group_id", "index"},
        optional={"parent_id", "expected_generation"},
        context="place group request",
    )
    parent_id = data.get("parent_id")
    if parent_id is not None and type(parent_id) is not str:
        raise ValueError("group parent id must be a string or null")
    generation = data.get("expected_generation")
    if generation is not None:
        generation = _integer(generation, "expected generation")
    return PlaceGroupRequest(
        group_id=GroupId(_identifier(data["group_id"], "group id")),
        parent_id=(
            GroupId(_identifier(parent_id, "group parent id")) if parent_id is not None else None
        ),
        index=_integer(data["index"], "group placement index"),
        expected_generation=generation,
    )


def copy_connection_to_group_request_to_wire(
    request: CopyConnectionToGroupRequest,
) -> Dict[str, Any]:
    if type(request) is not CopyConnectionToGroupRequest:
        raise TypeError("copy connection to group request is required")
    return {
        "connection_id": request.connection_id,
        "group_id": request.group_id,
    }


def copy_connection_to_group_request_from_wire(
    value: Any,
) -> CopyConnectionToGroupRequest:
    data = _strict_fields(
        value,
        required={"connection_id", "group_id"},
        context="copy connection to group request",
    )
    return CopyConnectionToGroupRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        group_id=GroupId(_identifier(data["group_id"], "group id")),
    )


def remove_connection_from_group_request_to_wire(
    request: RemoveConnectionFromGroupRequest,
) -> Dict[str, Any]:
    if type(request) is not RemoveConnectionFromGroupRequest:
        raise TypeError("remove connection from group request is required")
    return {
        "connection_id": request.connection_id,
        "group_id": request.group_id,
    }


def remove_connection_from_group_request_from_wire(
    value: Any,
) -> RemoveConnectionFromGroupRequest:
    data = _strict_fields(
        value,
        required={"connection_id", "group_id"},
        context="remove connection from group request",
    )
    return RemoveConnectionFromGroupRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        group_id=GroupId(_identifier(data["group_id"], "group id")),
    )


def reorder_connection_request_to_wire(
    request: ReorderConnectionRequest,
) -> Dict[str, Any]:
    if type(request) is not ReorderConnectionRequest:
        raise TypeError("reorder connection request is required")
    payload: Dict[str, Any] = {
        "connection_id": request.connection_id,
        "target_connection_id": request.target_connection_id,
        "position": request.position,
    }
    if request.group_id is not None:
        payload["group_id"] = request.group_id
    return payload


def reorder_connection_request_from_wire(
    value: Any,
) -> ReorderConnectionRequest:
    data = _strict_fields(
        value,
        required={"connection_id", "target_connection_id", "position"},
        optional={"group_id"},
        context="reorder connection request",
    )
    group_id = data.get("group_id")
    if group_id is not None and type(group_id) is not str:
        raise ValueError("reorder group id must be a string or null")
    return ReorderConnectionRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        target_connection_id=ConnectionId(
            _identifier(data["target_connection_id"], "target connection id")
        ),
        group_id=(
            GroupId(_identifier(group_id, "reorder group id")) if group_id is not None else None
        ),
        position=_identifier(data["position"], "reorder position"),
    )


def move_connections_request_to_wire(
    request: MoveConnectionsRequest,
) -> Dict[str, Any]:
    if type(request) is not MoveConnectionsRequest:
        raise TypeError("move connections request is required")
    payload: Dict[str, Any] = {
        "connection_ids": list(request.connection_ids),
        "mode": request.mode.value,
    }
    if request.target_group_id is not None:
        payload["target_group_id"] = request.target_group_id
    if request.target_connection_id is not None:
        payload["target_connection_id"] = request.target_connection_id
    if request.position is not None:
        payload["position"] = request.position
    if request.expected_generation is not None:
        payload["expected_generation"] = request.expected_generation
    if request.source_group_id is not None:
        payload["source_group_id"] = request.source_group_id
    return payload


def move_connections_request_from_wire(value: Any) -> MoveConnectionsRequest:
    data = _strict_fields(
        value,
        required={"connection_ids"},
        optional={
            "target_group_id",
            "target_connection_id",
            "position",
            "expected_generation",
            "source_group_id",
            "mode",
        },
        context="move connections request",
    )
    ids = data["connection_ids"]
    if type(ids) is not list:
        raise ValueError("connection ids must be an array")
    target_group_id = data.get("target_group_id")
    target_connection_id = data.get("target_connection_id")
    source_group_id = data.get("source_group_id")
    mode = data.get("mode", ConnectionPlacementMode.EXCLUSIVE.value)
    if target_group_id is not None and type(target_group_id) is not str:
        raise ValueError("target group id must be a string or null")
    if target_connection_id is not None and type(target_connection_id) is not str:
        raise ValueError("target connection id must be a string or null")
    if source_group_id is not None and type(source_group_id) is not str:
        raise ValueError("source group id must be a string or null")
    if type(mode) is not str:
        raise ValueError("move mode must be a string")
    position = data.get("position")
    if position is not None and type(position) is not str:
        raise ValueError("move position must be a string or null")
    generation = data.get("expected_generation")
    if generation is not None:
        generation = _integer(generation, "expected generation")
    return MoveConnectionsRequest(
        connection_ids=tuple(ConnectionId(_identifier(item, "connection id")) for item in ids),
        target_group_id=(
            GroupId(_identifier(target_group_id, "target group id"))
            if target_group_id is not None
            else None
        ),
        target_connection_id=(
            ConnectionId(_identifier(target_connection_id, "target connection id"))
            if target_connection_id is not None
            else None
        ),
        position=position,
        expected_generation=generation,
        source_group_id=(
            GroupId(_identifier(source_group_id, "source group id"))
            if source_group_id is not None
            else None
        ),
        mode=ConnectionPlacementMode(mode),
    )


def add_tag_to_connections_request_to_wire(
    request: AddTagToConnectionsRequest,
) -> Dict[str, Any]:
    if type(request) is not AddTagToConnectionsRequest:
        raise TypeError("add tag to connections request is required")
    payload = {
        "connection_ids": list(request.connection_ids),
        "tag": request.tag,
    }
    if request.expected_generation is not None:
        payload["expected_generation"] = request.expected_generation
    return payload


def add_tag_to_connections_request_from_wire(
    value: Any,
) -> AddTagToConnectionsRequest:
    data = _strict_fields(
        value,
        required={"connection_ids", "tag"},
        optional={"expected_generation"},
        context="add tag to connections request",
    )
    ids = data["connection_ids"]
    if type(ids) is not list:
        raise ValueError("connection ids must be an array")
    generation = data.get("expected_generation")
    if generation is not None:
        generation = _integer(generation, "expected generation")
    return AddTagToConnectionsRequest(
        connection_ids=tuple(ConnectionId(_identifier(item, "connection id")) for item in ids),
        tag=_text(data["tag"], "tag"),
        expected_generation=generation,
    )


def rename_tag_request_to_wire(request: RenameTagRequest) -> Dict[str, Any]:
    if type(request) is not RenameTagRequest:
        raise TypeError("rename tag request is required")
    return {"old_tag": request.old_tag, "new_tag": request.new_tag}


def rename_tag_request_from_wire(value: Any) -> RenameTagRequest:
    data = _strict_fields(
        value,
        required={"old_tag", "new_tag"},
        context="rename tag request",
    )
    return RenameTagRequest(
        old_tag=_text(data["old_tag"], "old tag"),
        new_tag=_text(data["new_tag"], "new tag"),
    )


def handshake_request_to_wire(request: HandshakeRequest) -> Dict[str, Any]:
    return {
        "client_name": request.client_name,
        "client_version": request.client_version,
        "supported_protocol_versions": list(request.supported_protocol_versions),
        "client_capabilities": sorted(request.client_capabilities),
        "frontend_type": request.frontend_type,
        "supported_frame_types": sorted(request.supported_frame_types),
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
        optional={"supported_frame_types"},
        context="handshake request",
    )
    versions = data["supported_protocol_versions"]
    capabilities = data["client_capabilities"]
    frame_types = data.get("supported_frame_types", [])
    if (
        type(versions) is not list
        or type(capabilities) is not list
        or type(frame_types) is not list
    ):
        raise ValueError("handshake version and capability fields must be arrays")
    return HandshakeRequest(
        client_name=data["client_name"],
        client_version=data["client_version"],
        supported_protocol_versions=tuple(versions),
        client_capabilities=frozenset(
            _identifier(item, "client capability") for item in capabilities
        ),
        frontend_type=data["frontend_type"],
        supported_frame_types=frozenset(
            _identifier(item, "supported frame type") for item in frame_types
        ),
    )


def handshake_result_to_wire(result: HandshakeResult) -> Dict[str, Any]:
    payload = {
        "daemon_version": result.daemon_version,
        "core_version": result.core_version,
        "selected_protocol_version": result.selected_protocol_version,
        "daemon_capabilities": sorted(item.value for item in result.daemon_capabilities),
        "compatibility_status": result.compatibility_status,
        "server_instance_id": result.server_instance_id,
    }
    if result.daemon_started_at:
        payload["daemon_started_at"] = result.daemon_started_at
    if result.development_revision:
        payload["development_revision"] = result.development_revision
    if result.api_implementation_version:
        payload["api_implementation_version"] = result.api_implementation_version
    return payload


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
        optional={
            "daemon_started_at",
            "development_revision",
            "api_implementation_version",
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
    started_at = data.get("daemon_started_at", "")
    if started_at is None:
        started_at = ""
    if type(started_at) is not str:
        raise ValueError("daemon_started_at must be a string")
    revision = data.get("development_revision", "")
    if revision is None:
        revision = ""
    if type(revision) is not str:
        raise ValueError("development_revision must be a string")
    api_impl = data.get("api_implementation_version", "")
    if api_impl is None:
        api_impl = ""
    if type(api_impl) is not str:
        raise ValueError("api_implementation_version must be a string")
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
        daemon_started_at=started_at.strip(),
        development_revision=revision.strip(),
        api_implementation_version=api_impl.strip(),
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
        "display_name": summary.display_name,
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
        optional={"display_name"},
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
        display_name=_text(data.get("display_name", ""), "connection display name", allow_empty=True),
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


def connection_mutation_result_to_wire(result: Any) -> Dict[str, Any]:
    return {
        "connection_id": result.connection_id,
        "nickname": result.nickname,
        "generation": result.generation,
        "changed": result.changed,
        "changed_fields": list(result.changed_fields),
        "display_name": result.display_name,
    }


def connection_mutation_result_from_wire(value: Any) -> Any:
    from ..models.connections import ConnectionMutationResult

    data = _strict_fields(
        value,
        context="ConnectionMutationResult",
        required={"connection_id", "nickname", "generation"},
        optional={"changed", "changed_fields", "display_name"},
    )
    changed = data.get("changed", True)
    fields = data.get("changed_fields", [])
    if type(changed) is not bool or type(fields) is not list:
        raise ValueError("invalid connection mutation change metadata")
    return ConnectionMutationResult(
        connection_id=_text(data["connection_id"], "connection_id"),
        nickname=_text(data["nickname"], "nickname"),
        generation=_integer(data["generation"], "generation"),
        changed=changed,
        changed_fields=tuple(_text(item, "changed field") for item in fields),
        display_name=_text(data.get("display_name", ""), "connection display name", allow_empty=True),
    )


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
        optional={"display_name"},
        context="connection details",
    )
    summary_payload = {key: data[key] for key in _SUMMARY_FIELDS}
    if "display_name" in data:
        summary_payload["display_name"] = data["display_name"]
    summary = connection_summary_from_wire(summary_payload)
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


def forwarding_rule_to_wire(rule: ForwardingRule) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "type": rule.type,
        "listen_port": rule.listen_port,
        "listen_addr": rule.listen_addr,
        "enabled": rule.enabled,
    }
    if rule.type == "local":
        d["remote_host"] = rule.remote_host
        d["remote_port"] = rule.remote_port
    elif rule.type == "remote":
        if rule.socks:
            d["socks"] = True
        else:
            d["local_host"] = rule.local_host
            d["local_port"] = rule.local_port
    return d


def forwarding_rule_from_wire(value: Any) -> ForwardingRule:
    data = _strict_fields(
        value,
        required={"type", "listen_port"},
        optional={
            "listen_addr",
            "enabled",
            "remote_host",
            "remote_port",
            "local_host",
            "local_port",
            "socks",
        },
        context="forwarding rule",
    )
    rule_type = _identifier(data["type"], "forwarding rule type")
    if rule_type not in ("local", "remote", "dynamic"):
        raise ValueError(f"forwarding rule type must be local/remote/dynamic, got {rule_type!r}")
    listen_port = _integer(data["listen_port"], "forwarding rule listen_port")
    if not 1 <= listen_port <= 65535:
        raise ValueError("forwarding rule listen_port must be 1-65535")
    return ForwardingRule(
        type=rule_type,
        listen_port=listen_port,
        listen_addr=_text(
            data.get("listen_addr", ""), "forwarding rule listen_addr", allow_empty=True
        ),
        remote_host=_text(
            data.get("remote_host", ""), "forwarding rule remote_host", allow_empty=True
        ),
        remote_port=_integer(data.get("remote_port", 0), "forwarding rule remote_port"),
        local_host=_text(
            data.get("local_host", ""), "forwarding rule local_host", allow_empty=True
        ),
        local_port=_integer(data.get("local_port", 0), "forwarding rule local_port"),
        enabled=_boolean(data.get("enabled", True), "forwarding rule enabled"),
        socks=_boolean(data.get("socks", False), "forwarding rule socks"),
    )


def _forwarding_rules_to_wire(rules: Any) -> Any:
    if rules is None:
        return None
    return [forwarding_rule_to_wire(r) for r in rules]


def _forwarding_rules_from_wire(value: Any) -> Any:
    if value is None:
        return None
    if type(value) is not list:
        raise ValueError("forwarding rules must be an array")
    return tuple(forwarding_rule_from_wire(item) for item in value)


_EDITOR_DETAIL_FIELDS = frozenset(
    {
        "aliases",
        "authentication_method",
        "identity_configured",
        "certificate_configured",
        "x11_forwarding",
        "forwarding_rule_count",
        "proxy_jump",
        # auth
        "key_select_mode",
        "identity_files",
        "certificate_files",
        "identity_agent",
        "add_keys_to_agent",
        "pkcs11_provider",
        "security_key_provider",
        "pubkey_auth_no",
        # routing
        "forward_agent",
        "forward_agent_target",
        "forward_agent_explicit_no",
        "proxy_command",
        # forwarding
        "forwarding_rules",
        "x11_forwarding_explicit_no",
        "identities_only_explicit_no",
        # commands
        "pre_command",
        "local_command",
        "remote_command",
        "request_tty",
        # advanced
        "extra_ssh_config",
        "identity_file_none",
        "preferred_authentications",
        # context
        "source",
        "generation",
    }
)


def effective_config_comparison_to_wire(
    value: EffectiveConfigComparison,
) -> Dict[str, Any]:
    if not isinstance(value, EffectiveConfigComparison):
        raise TypeError("value must be an EffectiveConfigComparison")
    return {
        "connection_id": value.connection_id,
        "host": value.host,
        "available": value.available,
        "has_diff": value.has_diff,
        "changes": [dict(item) for item in value.changes],
        "own": list(value.own),
        "full": list(value.full),
        "generation": value.generation,
    }


def effective_config_comparison_from_wire(
    value: Any,
) -> EffectiveConfigComparison:
    data = _strict_fields(
        value,
        required={
            "connection_id", "host", "available", "has_diff", "changes",
            "own", "full", "generation",
        },
        context="effective config comparison",
    )
    if type(data["changes"]) is not list:
        raise ValueError("effective config changes must be a list")
    if any(type(item) is not dict for item in data["changes"]):
        raise ValueError("effective config changes must contain objects")
    if type(data["own"]) is not list or type(data["full"]) is not list:
        raise ValueError("effective config lines must be lists")
    return EffectiveConfigComparison(
        connection_id=data["connection_id"],
        host=data["host"],
        available=data["available"],
        has_diff=data["has_diff"],
        changes=tuple(dict(item) for item in data["changes"]),
        own=tuple(data["own"]),
        full=tuple(data["full"]),
        generation=data["generation"],
    )


def unsaved_host_check_request_to_wire(
    value: UnsavedHostCheckRequest,
) -> Dict[str, Any]:
    if not isinstance(value, UnsavedHostCheckRequest):
        raise TypeError("value must be an UnsavedHostCheckRequest")
    result = {
        "hostname": value.hostname,
        "username": value.username,
        "port": value.port,
        "protocol": value.protocol,
        "proxy_jump": list(value.proxy_jump),
    }
    if value.connection_id is not None:
        result["connection_id"] = value.connection_id
    return result


def unsaved_host_check_request_from_wire(value: Any) -> UnsavedHostCheckRequest:
    data = _strict_fields(
        value,
        required={"hostname", "username"},
        optional={"connection_id", "port", "protocol", "proxy_jump"},
        context="unsaved host check request",
    )
    return UnsavedHostCheckRequest(
        hostname=data["hostname"],
        username=data["username"],
        connection_id=data.get("connection_id"),
        port=data.get("port"),
        protocol=data.get("protocol", "ssh"),
        proxy_jump=tuple(data.get("proxy_jump", ())),
    )


def unsaved_host_check_result_to_wire(
    value: UnsavedHostCheckResult,
) -> Dict[str, Any]:
    if not isinstance(value, UnsavedHostCheckResult):
        raise TypeError("value must be an UnsavedHostCheckResult")
    return {
        "saved": value.saved,
        "hostname": value.hostname,
        "username": value.username,
        "generation": value.generation,
    }


def unsaved_host_check_result_from_wire(value: Any) -> UnsavedHostCheckResult:
    data = _strict_fields(
        value,
        required={"saved", "hostname", "username", "generation"},
        context="unsaved host check result",
    )
    return UnsavedHostCheckResult(
        saved=data["saved"],
        hostname=data["hostname"],
        username=data["username"],
        generation=data["generation"],
    )


def connection_editor_details_to_wire(
    details: ConnectionEditorDetails,
) -> Dict[str, Any]:
    result = connection_details_to_wire(details)
    result.update(
        {
            "key_select_mode": details.key_select_mode,
            "identity_files": list(details.identity_files),
            "certificate_files": list(details.certificate_files),
            "identity_agent": details.identity_agent,
            "add_keys_to_agent": details.add_keys_to_agent,
            "pkcs11_provider": details.pkcs11_provider,
            "security_key_provider": details.security_key_provider,
            "pubkey_auth_no": details.pubkey_auth_no,
            "forward_agent": details.forward_agent,
            "forward_agent_target": details.forward_agent_target,
            "forward_agent_explicit_no": details.forward_agent_explicit_no,
            "proxy_command": details.proxy_command,
            "forwarding_rules": [forwarding_rule_to_wire(r) for r in details.forwarding_rules],
            "x11_forwarding_explicit_no": details.x11_forwarding_explicit_no,
            "identities_only_explicit_no": details.identities_only_explicit_no,
            "pre_command": details.pre_command,
            "local_command": details.local_command,
            "remote_command": details.remote_command,
            "request_tty": details.request_tty,
            "extra_ssh_config": details.extra_ssh_config,
            "identity_file_none": details.identity_file_none,
            "preferred_authentications": details.preferred_authentications,
            "source": details.source,
            "generation": details.generation,
            "authored_directives": list(details.authored_directives),
        }
    )
    return result


def connection_editor_details_from_wire(
    value: Any,
) -> ConnectionEditorDetails:
    summary_fields = _SUMMARY_FIELDS
    data = _strict_fields(
        value,
        required=summary_fields | _EDITOR_DETAIL_FIELDS,
        # Additive: a daemon predating authorship evidence simply omits it, and
        # an empty tuple already means "no evidence".
        optional={"display_name", "authored_directives"},
        context="connection editor details",
    )
    summary_payload = {key: data[key] for key in summary_fields}
    if "display_name" in data:
        summary_payload["display_name"] = data["display_name"]
    summary = connection_summary_from_wire(summary_payload)
    if type(data["aliases"]) is not list or type(data["proxy_jump"]) is not list:
        raise ValueError("connection aliases and proxy jump must be arrays")
    aliases = tuple(_identifier(item, "connection alias") for item in data["aliases"])
    proxy_jump = tuple(_identifier(item, "proxy jump host") for item in data["proxy_jump"])
    try:
        authentication_method = AuthenticationMethod(data["authentication_method"])
    except (TypeError, ValueError):
        raise ValueError(
            "connection editor details contain unknown authentication method"
        ) from None
    identity_files = data["identity_files"]
    if type(identity_files) is not list:
        raise ValueError("identity_files must be an array")
    certificate_files = data["certificate_files"]
    if type(certificate_files) is not list:
        raise ValueError("certificate_files must be an array")
    forwarding_rules = data["forwarding_rules"]
    if type(forwarding_rules) is not list:
        raise ValueError("forwarding_rules must be an array")
    raw_authored = data.get("authored_directives", [])
    if type(raw_authored) is not list:
        raise ValueError("authored_directives must be an array")
    authored_directives = tuple(
        _text(item, "authored directive", allow_empty=False) for item in raw_authored
    )
    return ConnectionEditorDetails(
        id=summary.id,
        nickname=summary.nickname,
        host=summary.host,
        hostname=summary.hostname,
        username=summary.username,
        port=summary.port,
        protocol=summary.protocol,
        health=summary.health,
        groups=summary.groups,
        display_name=summary.display_name,
        aliases=aliases,
        authentication_method=authentication_method,
        identity_configured=_boolean(data["identity_configured"], "identity configured"),
        certificate_configured=_boolean(data["certificate_configured"], "certificate configured"),
        x11_forwarding=_boolean(data["x11_forwarding"], "X11 forwarding"),
        forwarding_rule_count=_integer(data["forwarding_rule_count"], "forwarding rule count"),
        proxy_jump=proxy_jump,
        key_select_mode=_integer(data["key_select_mode"], "key_select_mode"),
        identity_files=tuple(_identifier(f, "identity file") for f in identity_files),
        certificate_files=tuple(_identifier(f, "certificate file") for f in certificate_files),
        identity_agent=_text(data["identity_agent"], "identity_agent", allow_empty=True),
        add_keys_to_agent=_text(data["add_keys_to_agent"], "add_keys_to_agent", allow_empty=True),
        pkcs11_provider=_text(data["pkcs11_provider"], "pkcs11_provider", allow_empty=True),
        security_key_provider=_text(
            data["security_key_provider"], "security_key_provider", allow_empty=True
        ),
        pubkey_auth_no=_boolean(data["pubkey_auth_no"], "pubkey_auth_no"),
        forward_agent=_boolean(data["forward_agent"], "forward_agent"),
        forward_agent_explicit_no=_boolean(
            data["forward_agent_explicit_no"], "forward_agent_explicit_no"
        ),
        forward_agent_target=_text(
            data["forward_agent_target"],
            "forward_agent_target",
            allow_empty=True,
        ),
        proxy_command=_text(data["proxy_command"], "proxy_command", allow_empty=True),
        forwarding_rules=tuple(forwarding_rule_from_wire(r) for r in forwarding_rules),
        x11_forwarding_explicit_no=_boolean(
            data["x11_forwarding_explicit_no"], "x11_forwarding_explicit_no"
        ),
        identities_only_explicit_no=_boolean(
            data["identities_only_explicit_no"], "identities_only_explicit_no"
        ),
        pre_command=_text(data["pre_command"], "pre_command", allow_empty=True),
        local_command=_text(data["local_command"], "local_command", allow_empty=True),
        remote_command=_text(data["remote_command"], "remote_command", allow_empty=True),
        request_tty=_text(data["request_tty"], "request_tty", allow_empty=True),
        extra_ssh_config=_text(data["extra_ssh_config"], "extra_ssh_config", allow_empty=True),
        identity_file_none=_boolean(data["identity_file_none"], "identity_file_none"),
        preferred_authentications=_text(
            data["preferred_authentications"],
            "preferred_authentications",
            allow_empty=True,
        ),
        source=_text(data["source"], "source", allow_empty=True),
        generation=_integer(data["generation"], "generation"),
        authored_directives=authored_directives,
    )


def connection_editor_capabilities_to_wire(
    caps: ConnectionEditorCapabilities,
) -> Dict[str, Any]:
    return {
        "writable_fields": sorted(caps.writable_fields),
        "supports_secrets": caps.supports_secrets,
        "supports_metadata": caps.supports_metadata,
        "supports_groups": caps.supports_groups,
        "supports_split": caps.supports_split,
    }


def connection_editor_capabilities_from_wire(
    value: Any,
) -> ConnectionEditorCapabilities:
    data = _strict_fields(
        value,
        required={
            "writable_fields",
            "supports_secrets",
            "supports_metadata",
            "supports_groups",
            "supports_split",
        },
        context="connection editor capabilities",
    )
    fields_raw = data["writable_fields"]
    if type(fields_raw) is not list:
        raise ValueError("writable_fields must be an array")
    for item in fields_raw:
        if type(item) is not str or not item.strip():
            raise ValueError("each writable_fields entry must be a non-empty string")
    return ConnectionEditorCapabilities(
        writable_fields=frozenset(str(f) for f in fields_raw),
        supports_secrets=_boolean(data["supports_secrets"], "supports_secrets"),
        supports_metadata=_boolean(data["supports_metadata"], "supports_metadata"),
        supports_groups=_boolean(data["supports_groups"], "supports_groups"),
        supports_split=_boolean(data["supports_split"], "supports_split"),
    )


def connection_validation_error_to_wire(error: ConnectionValidationError) -> Dict[str, Any]:
    return {
        "field": error.field,
        "code": error.code,
        "message": error.message,
    }


def connection_validation_error_from_wire(value: Any) -> ConnectionValidationError:
    data = _strict_fields(
        value,
        required={"field", "code", "message"},
        context="connection validation error",
    )
    return ConnectionValidationError(
        field=_identifier(data["field"], "validation field"),
        code=_identifier(data["code"], "validation code"),
        message=_identifier(data["message"], "validation message"),
    )


def connection_validation_result_to_wire(
    result: ConnectionValidationResult,
) -> Dict[str, Any]:
    return {
        "valid": result.valid,
        "errors": [connection_validation_error_to_wire(e) for e in result.errors],
    }


def connection_validation_result_from_wire(
    value: Any,
) -> ConnectionValidationResult:
    data = _strict_fields(
        value,
        required={"valid", "errors"},
        context="connection validation result",
    )
    errors_raw = data["errors"]
    if type(errors_raw) is not list:
        raise ValueError("validation errors must be an array")
    return ConnectionValidationResult(
        valid=_boolean(data["valid"], "validation valid"),
        errors=tuple(connection_validation_error_from_wire(e) for e in errors_raw),
    )


def create_connection_request_to_wire(
    request: CreateConnectionRequest,
) -> Dict[str, Any]:
    if type(request) is not CreateConnectionRequest:
        raise TypeError("create connection request is required")
    result: Dict[str, Any] = {
        "nickname": request.nickname,
        "hostname": request.hostname,
        "username": request.username,
        "port": request.port,
        "protocol": request.protocol,
        "display_name": request.display_name,
    }
    if request.config_patch:
        result["config_patch"] = dict(request.config_patch)
    if request.plugin_data:
        result["plugin_data"] = dict(request.plugin_data)
    return result


def create_connection_request_from_wire(value: Any) -> CreateConnectionRequest:
    data = _strict_fields(
        value,
        required={"nickname", "hostname", "username", "port", "protocol"},
        optional={"config_patch", "plugin_data", "display_name"},
        context="create connection request",
    )
    raw_patch = data.get("config_patch")
    config_patch: Dict[str, Any] = {}
    if raw_patch is not None:
        if type(raw_patch) is not dict:
            raise ValueError("config_patch must be an object")
        config_patch = {k: v for k, v in raw_patch.items() if k in EDITABLE_CONFIG_FIELDS}
    raw_plugin_data = data.get("plugin_data", {})
    if type(raw_plugin_data) is not dict:
        raise ValueError("plugin_data must be an object")
    return CreateConnectionRequest(
        nickname=_identifier(data["nickname"], "connection nickname"),
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
        display_name=_text(data.get("display_name", ""), "connection display name", allow_empty=True),
        config_patch=config_patch,
        plugin_data=dict(raw_plugin_data),
    )


def update_connection_request_to_wire(
    request: UpdateConnectionRequest,
) -> Dict[str, Any]:
    if type(request) is not UpdateConnectionRequest:
        raise TypeError("update connection request is required")
    result: Dict[str, Any] = {}
    if request.nickname is not UNSET:
        result["nickname"] = request.nickname
    if request.hostname is not UNSET:
        result["hostname"] = request.hostname
    if request.username is not UNSET:
        result["username"] = request.username
    if request.port is not UNSET:
        result["port"] = request.port
    if request.display_name is not UNSET:
        result["display_name"] = request.display_name
    if request.config_patch:
        result["config_patch"] = dict(request.config_patch)
    if request.plugin_data:
        result["plugin_data"] = dict(request.plugin_data)
    if request.expected_generation is not None:
        result["expected_generation"] = request.expected_generation
    return result


def update_connection_request_from_wire(value: Any) -> UpdateConnectionRequest:
    data = _strict_fields(
        value,
        required=set(),
        optional={
            "nickname",
            "hostname",
            "username",
            "port",
            "display_name",
            "config_patch",
            "plugin_data",
            "expected_generation",
        },
        context="update connection request",
    )
    if not data:
        raise ValueError("connection update must contain at least one field")

    nickname = data.get("nickname", UNSET)
    hostname = data.get("hostname", UNSET)
    username = data.get("username", UNSET)
    port = data.get("port", UNSET)
    display_name = data.get("display_name", UNSET)

    if nickname is not UNSET and nickname is not None:
        nickname = _identifier(nickname, "connection nickname")
    if hostname is not UNSET and hostname is not None:
        hostname = _text(hostname, "connection hostname", allow_empty=True)
    if username is not UNSET and username is not None:
        username = _text(username, "connection username", allow_empty=True)
    if port is not UNSET and port is not None:
        # An empty string is the explicit "clear the authored Port" signal.
        port = "" if (type(port) is str and not port.strip()) else _integer(
            port, "connection port"
        )

    raw_patch = data.get("config_patch")
    config_patch: Dict[str, Any] = {}
    if raw_patch is not None:
        if type(raw_patch) is not dict:
            raise ValueError("config_patch must be an object")
        config_patch = {k: v for k, v in raw_patch.items() if k in EDITABLE_CONFIG_FIELDS}

    expected_generation = data.get("expected_generation")
    if expected_generation is not None:
        expected_generation = _integer(expected_generation, "expected generation")

    raw_plugin_data = data.get("plugin_data", {})
    if type(raw_plugin_data) is not dict:
        raise ValueError("plugin_data must be an object")
    return UpdateConnectionRequest(
        nickname=nickname,
        hostname=hostname,
        username=username,
        port=port,
        display_name=display_name,
        config_patch=config_patch,
        plugin_data=dict(raw_plugin_data),
        expected_generation=expected_generation,
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


def store_connection_password_request_to_wire(
    request: StoreConnectionPasswordRequest,
) -> Dict[str, Any]:
    if type(request) is not StoreConnectionPasswordRequest:
        raise TypeError("store connection password request is required")
    payload: Dict[str, Any] = {
        "connection_id": request.connection_id,
        "password": request.password,
    }
    if request.previous_hostname:
        payload["previous_hostname"] = request.previous_hostname
    if request.previous_host:
        payload["previous_host"] = request.previous_host
    if request.previous_username:
        payload["previous_username"] = request.previous_username
    return payload


def store_connection_password_request_from_wire(
    value: Any,
) -> StoreConnectionPasswordRequest:
    data = _strict_fields(
        value,
        required={"connection_id", "password"},
        optional={"previous_hostname", "previous_host", "previous_username"},
        context="store connection password request",
    )
    return StoreConnectionPasswordRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        password=_text(data["password"], "connection password"),
        previous_hostname=_text(
            data.get("previous_hostname", ""),
            "previous hostname",
            allow_empty=True,
        ),
        previous_host=_text(
            data.get("previous_host", ""),
            "previous host",
            allow_empty=True,
        ),
        previous_username=_text(
            data.get("previous_username", ""),
            "previous username",
            allow_empty=True,
        ),
    )


def set_session_connection_password_request_to_wire(
    request: SetSessionConnectionPasswordRequest,
) -> Dict[str, Any]:
    if type(request) is not SetSessionConnectionPasswordRequest:
        raise TypeError("session connection password request is required")
    return {"connection_id": request.connection_id}


def set_session_connection_password_request_from_wire(
    value: Any,
) -> SetSessionConnectionPasswordRequest:
    data = _strict_fields(
        value,
        required={"connection_id"},
        context="session connection password request",
    )
    return SetSessionConnectionPasswordRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id"))
    )


def delete_connection_password_request_to_wire(
    request: DeleteConnectionPasswordRequest,
) -> Dict[str, Any]:
    if type(request) is not DeleteConnectionPasswordRequest:
        raise TypeError("delete connection password request is required")
    payload: Dict[str, Any] = {
        "connection_id": request.connection_id,
    }
    if request.previous_hostname:
        payload["previous_hostname"] = request.previous_hostname
    if request.previous_host:
        payload["previous_host"] = request.previous_host
    if request.previous_username:
        payload["previous_username"] = request.previous_username
    return payload


def delete_connection_password_request_from_wire(
    value: Any,
) -> DeleteConnectionPasswordRequest:
    data = _strict_fields(
        value,
        required={"connection_id"},
        optional={"previous_hostname", "previous_host", "previous_username"},
        context="delete connection password request",
    )
    return DeleteConnectionPasswordRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        previous_hostname=_text(
            data.get("previous_hostname", ""),
            "previous hostname",
            allow_empty=True,
        ),
        previous_host=_text(
            data.get("previous_host", ""),
            "previous host",
            allow_empty=True,
        ),
        previous_username=_text(
            data.get("previous_username", ""),
            "previous username",
            allow_empty=True,
        ),
    )


def store_key_passphrase_request_to_wire(
    request: StoreKeyPassphraseRequest,
) -> Dict[str, Any]:
    if type(request) is not StoreKeyPassphraseRequest:
        raise TypeError("store key passphrase request is required")
    return {
        "key_path": request.key_path,
        "interaction_scope_id": request.interaction_scope_id,
    }


def store_key_passphrase_request_from_wire(
    value: Any,
) -> StoreKeyPassphraseRequest:
    data = _strict_fields(
        value,
        required={"key_path", "interaction_scope_id"},
        context="store key passphrase request",
    )
    return StoreKeyPassphraseRequest(
        key_path=_text(data["key_path"], "key path"),
        interaction_scope_id=SessionId(
            _identifier(data["interaction_scope_id"], "key interaction scope id")
        ),
    )


def delete_key_passphrase_request_to_wire(
    request: DeleteKeyPassphraseRequest,
) -> Dict[str, Any]:
    if type(request) is not DeleteKeyPassphraseRequest:
        raise TypeError("delete key passphrase request is required")
    return {"key_path": request.key_path}


def delete_key_passphrase_request_from_wire(
    value: Any,
) -> DeleteKeyPassphraseRequest:
    data = _strict_fields(
        value,
        required={"key_path"},
        context="delete key passphrase request",
    )
    return DeleteKeyPassphraseRequest(
        key_path=_text(data["key_path"], "key path"),
    )


def lookup_key_passphrase_request_to_wire(
    request: LookupKeyPassphraseRequest,
) -> Dict[str, Any]:
    if type(request) is not LookupKeyPassphraseRequest:
        raise TypeError("lookup key passphrase request is required")
    return {"key_path": request.key_path}


def lookup_key_passphrase_request_from_wire(
    value: Any,
) -> LookupKeyPassphraseRequest:
    data = _strict_fields(
        value,
        required={"key_path"},
        context="lookup key passphrase request",
    )
    return LookupKeyPassphraseRequest(
        key_path=_text(data["key_path"], "key path"),
    )


def update_connection_metadata_request_to_wire(
    request: UpdateConnectionMetadataRequest,
) -> Dict[str, Any]:
    if type(request) is not UpdateConnectionMetadataRequest:
        raise TypeError("update connection metadata request is required")
    return {
        "connection_id": request.connection_id,
        "meta": thaw_safe_metadata(request.meta),
    }


def update_connection_metadata_request_from_wire(
    value: Any,
) -> UpdateConnectionMetadataRequest:
    data = _strict_fields(
        value,
        required={"connection_id", "meta"},
        context="update connection metadata request",
    )
    if type(data.get("meta")) is not dict:
        raise ValueError("meta must be an object")
    return UpdateConnectionMetadataRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        meta=data["meta"],
    )


def assign_connection_to_group_request_to_wire(
    request: AssignConnectionToGroupRequest,
) -> Dict[str, Any]:
    if type(request) is not AssignConnectionToGroupRequest:
        raise TypeError("assign connection to group request is required")
    payload: Dict[str, Any] = {"connection_id": request.connection_id}
    if request.group_id:
        payload["group_id"] = request.group_id
    return payload


def assign_connection_to_group_request_from_wire(
    value: Any,
) -> AssignConnectionToGroupRequest:
    data = _strict_fields(
        value,
        required={"connection_id"},
        optional={"group_id"},
        context="assign connection to group request",
    )
    return AssignConnectionToGroupRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        group_id=_text(data.get("group_id", ""), "group_id", allow_empty=True),
    )


def create_group_request_to_wire(
    request: CreateGroupRequest,
) -> Dict[str, Any]:
    if type(request) is not CreateGroupRequest:
        raise TypeError("create group request is required")
    payload: Dict[str, Any] = {"name": request.name}
    if request.parent_id:
        payload["parent_id"] = request.parent_id
    if request.color:
        payload["color"] = request.color
    return payload


def create_group_request_from_wire(value: Any) -> CreateGroupRequest:
    data = _strict_fields(
        value,
        required={"name"},
        optional={"parent_id", "color"},
        context="create group request",
    )
    return CreateGroupRequest(
        name=_text(data["name"], "group name"),
        parent_id=_text(data.get("parent_id", ""), "parent_id", allow_empty=True),
        color=_text(data.get("color", ""), "color", allow_empty=True),
    )


def delete_group_request_to_wire(
    request: DeleteGroupRequest,
) -> Dict[str, Any]:
    if type(request) is not DeleteGroupRequest:
        raise TypeError("delete group request is required")
    return {"group_id": request.group_id}


def delete_group_request_from_wire(value: Any) -> DeleteGroupRequest:
    data = _strict_fields(
        value,
        required={"group_id"},
        context="delete group request",
    )
    return DeleteGroupRequest(
        group_id=_text(data["group_id"], "group_id"),
    )


def rename_group_request_to_wire(
    request: RenameGroupRequest,
) -> Dict[str, Any]:
    if type(request) is not RenameGroupRequest:
        raise TypeError("rename group request is required")
    return {"group_id": request.group_id, "new_name": request.new_name}


def rename_group_request_from_wire(value: Any) -> RenameGroupRequest:
    data = _strict_fields(
        value,
        required={"group_id", "new_name"},
        context="rename group request",
    )
    return RenameGroupRequest(
        group_id=_text(data["group_id"], "group_id"),
        new_name=_text(data["new_name"], "new_name"),
    )


def split_connection_request_to_wire(
    request: SplitConnectionRequest,
) -> Dict[str, Any]:
    if type(request) is not SplitConnectionRequest:
        raise TypeError("split connection request is required")
    result = {
        "connection_id": request.connection_id,
        "original_host_token": request.original_host_token,
        "source_config_path": request.source_config_path,
        "nickname": request.nickname,
        "config_patch": dict(request.config_patch),
    }
    if request.hostname is not None:
        result["hostname"] = request.hostname
    if request.username is not None:
        result["username"] = request.username
    if request.port is not None:
        result["port"] = request.port
    if request.expected_generation is not None:
        result["expected_generation"] = request.expected_generation
    return result


def split_connection_request_from_wire(value: Any) -> SplitConnectionRequest:
    data = _strict_fields(
        value,
        required={
            "connection_id",
            "original_host_token",
            "source_config_path",
            "nickname",
            "config_patch",
        },
        optional={"hostname", "username", "port", "expected_generation"},
        context="split connection request",
    )
    expected_generation = data.get("expected_generation")
    if expected_generation is not None:
        if type(expected_generation) is not int:
            raise TypeError("expected_generation must be an integer if provided")

    port = data.get("port")
    if port is not None:
        if type(port) is not int:
            raise TypeError("port must be an integer")
        if not (1 <= port <= 65535):
            raise ValueError("port must be between 1 and 65535")

    return SplitConnectionRequest(
        connection_id=_identifier(data["connection_id"], "connection_id"),
        original_host_token=_text(data["original_host_token"], "original_host_token"),
        source_config_path=_text(data["source_config_path"], "source_config_path"),
        nickname=_text(data["nickname"], "nickname"),
        hostname=_text(data["hostname"], "hostname") if "hostname" in data else None,
        username=_text(data["username"], "username") if "username" in data else None,
        port=port,
        config_patch=dict(data["config_patch"]),
        expected_generation=expected_generation,
    )


def ssh_config_text_to_wire(result: SshConfigText) -> Dict[str, Any]:
    """Encode the daemon-resolved SSH config text document for the editor."""

    if type(result) is not SshConfigText:
        raise TypeError("SSH config text is required")
    return {
        "text": result.text,
        "revision": result.revision,
        "display_name": result.display_name,
        "writable": result.writable,
    }


def ssh_config_text_from_wire(value: Any) -> SshConfigText:
    """Decode a daemon SSH config text document with strict validation."""

    data = _strict_fields(
        value,
        required={"text", "revision", "display_name", "writable"},
        context="ssh config text",
    )
    return SshConfigText(
        text=_text(data["text"], "ssh config text", allow_empty=True),
        revision=_identifier(data["revision"], "ssh config revision"),
        display_name=_text(data["display_name"], "ssh config display name"),
        writable=_boolean(data["writable"], "ssh config writable"),
    )


def save_ssh_config_text_request_from_wire(
    value: Any,
) -> SaveSshConfigTextRequest:
    """Decode a raw SSH config text save request."""

    data = _strict_fields(
        value,
        required={"text", "expected_revision"},
        context="save ssh config text request",
    )
    return SaveSshConfigTextRequest(
        text=_text(data["text"], "ssh config text", allow_empty=True),
        expected_revision=_identifier(data["expected_revision"], "expected ssh config revision"),
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
        "dimensions": (
            terminal_dimensions_to_wire(request.dimensions)
            if request.dimensions is not None
            else None
        ),
        "remote_command": request.remote_command,
        "force_tty": request.force_tty,
    }


def open_session_request_from_wire(value: Any) -> OpenSessionRequest:
    data = _strict_fields(
        value,
        required={"connection_id"},
        optional={"dimensions", "remote_command", "force_tty"},
        context="open session request",
    )
    return OpenSessionRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        dimensions=(
            terminal_dimensions_from_wire(data["dimensions"])
            if data.get("dimensions") is not None
            else None
        ),
        remote_command=(
            _text(data["remote_command"], "remote command", allow_empty=False)
            if data.get("remote_command") is not None
            else None
        ),
        force_tty=bool(data.get("force_tty", False)),
    )


def attach_session_request_to_wire(request: AttachSessionRequest) -> Dict[str, Any]:
    if type(request) is not AttachSessionRequest:
        raise TypeError("attach session request is required")
    return {
        "session_id": request.session_id,
        "request_input": request.request_input,
        "want_terminal_output": request.want_terminal_output,
        "from_sequence": request.from_sequence,
    }


def attach_session_request_from_wire(value: Any) -> AttachSessionRequest:
    data = _strict_fields(
        value,
        required={"session_id", "request_input"},
        optional={"want_terminal_output", "from_sequence"},
        context="attach session request",
    )
    return AttachSessionRequest(
        session_id=_session_id(data["session_id"], "session id"),
        request_input=_boolean(data["request_input"], "request input"),
        want_terminal_output=_boolean(
            data.get("want_terminal_output", False),
            "terminal output preference",
        ),
        from_sequence=_integer(
            data.get("from_sequence", 0),
            "terminal replay sequence",
        ),
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
        "available_start": result.available_start,
        "live_sequence": result.live_sequence,
        "replay_truncated": result.replay_truncated,
        "eof": result.eof,
    }


def attach_session_result_from_wire(value: Any) -> AttachSessionResult:
    data = _strict_fields(
        value,
        required={"session", "attachment"},
        optional={
            "available_start",
            "live_sequence",
            "replay_truncated",
            "eof",
        },
        context="attach session result",
    )
    return AttachSessionResult(
        session=session_summary_from_wire(data["session"]),
        attachment=attachment_info_from_wire(data["attachment"]),
        available_start=_integer(data.get("available_start", 0), "available start"),
        live_sequence=_integer(data.get("live_sequence", 0), "live sequence"),
        replay_truncated=_boolean(
            data.get("replay_truncated", False),
            "replay truncated",
        ),
        eof=_boolean(data.get("eof", False), "terminal eof"),
    )


def external_terminal_launch_spec_to_wire(
    value: ExternalTerminalLaunchSpec,
) -> Dict[str, Any]:
    if not isinstance(value, ExternalTerminalLaunchSpec):
        raise TypeError("value must be an ExternalTerminalLaunchSpec")
    return {
        "argv": list(value.argv),
        "environment": [[name, item] for name, item in value.environment],
        "display_name": value.display_name,
        "secret_autofill_supported": value.secret_autofill_supported,
    }


def external_terminal_launch_spec_from_wire(
    value: Any,
) -> ExternalTerminalLaunchSpec:
    data = _strict_fields(
        value,
        required={"argv", "environment", "display_name", "secret_autofill_supported"},
        context="external terminal launch spec",
    )
    if type(data["argv"]) is not list:
        raise ValueError("external terminal argv must be a list")
    if type(data["environment"]) is not list:
        raise ValueError("external terminal environment must be a list")
    environment = []
    for item in data["environment"]:
        if type(item) is not list or len(item) != 2:
            raise ValueError("external terminal environment entries are pairs")
        environment.append((item[0], item[1]))
    return ExternalTerminalLaunchSpec(
        argv=tuple(data["argv"]),
        environment=tuple(environment),
        display_name=data["display_name"],
        secret_autofill_supported=data["secret_autofill_supported"],
    )


def terminal_dimensions_to_wire(value: TerminalDimensions) -> Dict[str, Any]:
    if type(value) is not TerminalDimensions:
        raise TypeError("terminal dimensions are required")
    return {"rows": value.rows, "columns": value.columns}


def terminal_dimensions_from_wire(value: Any) -> TerminalDimensions:
    data = _strict_fields(
        value,
        required={"rows", "columns"},
        context="terminal dimensions",
    )
    return TerminalDimensions(
        rows=_integer(data["rows"], "terminal rows"),
        columns=_integer(data["columns"], "terminal columns"),
    )


def resize_terminal_request_to_wire(
    request: ResizeTerminalRequest,
) -> Dict[str, Any]:
    if type(request) is not ResizeTerminalRequest:
        raise TypeError("terminal resize request is required")
    return {
        "session_id": request.session_id,
        "attachment_id": request.attachment_id,
        "dimensions": terminal_dimensions_to_wire(request.dimensions),
    }


def resize_terminal_request_from_wire(value: Any) -> ResizeTerminalRequest:
    data = _strict_fields(
        value,
        required={"session_id", "attachment_id", "dimensions"},
        context="terminal resize request",
    )
    return ResizeTerminalRequest(
        session_id=_session_id(data["session_id"], "session id"),
        attachment_id=AttachmentId(_identifier(data["attachment_id"], "attachment id")),
        dimensions=terminal_dimensions_from_wire(data["dimensions"]),
    )


def broadcast_terminal_input_request_to_wire(
    request: BroadcastTerminalInputRequest,
) -> Dict[str, Any]:
    if type(request) is not BroadcastTerminalInputRequest:
        raise TypeError("BroadcastTerminalInputRequest is required")
    return {
        "session_ids": list(request.session_ids),
        "command": request.command,
    }


def broadcast_terminal_input_request_from_wire(
    value: Any,
) -> BroadcastTerminalInputRequest:
    data = _strict_fields(
        value,
        required={"session_ids", "command"},
        context="terminal broadcast input request",
    )
    if type(data["session_ids"]) is not list:
        raise ValueError("terminal broadcast session_ids must be an array")
    return BroadcastTerminalInputRequest(
        session_ids=tuple(_session_id(item, "session id") for item in data["session_ids"]),
        command=data["command"],
    )


def claim_terminal_input_request_to_wire(
    request: ClaimTerminalInputRequest,
) -> Dict[str, Any]:
    if type(request) is not ClaimTerminalInputRequest:
        raise TypeError("terminal claim input request is required")
    return {
        "session_id": request.session_id,
        "attachment_id": request.attachment_id,
    }


def claim_terminal_input_request_from_wire(value: Any) -> ClaimTerminalInputRequest:
    data = _strict_fields(
        value,
        required={"session_id", "attachment_id"},
        context="terminal claim input request",
    )
    return ClaimTerminalInputRequest(
        session_id=_session_id(data["session_id"], "session id"),
        attachment_id=AttachmentId(_identifier(data["attachment_id"], "attachment id")),
    )


def release_terminal_input_request_to_wire(
    request: ReleaseTerminalInputRequest,
) -> Dict[str, Any]:
    if type(request) is not ReleaseTerminalInputRequest:
        raise TypeError("terminal release input request is required")
    return {
        "session_id": request.session_id,
        "attachment_id": request.attachment_id,
    }


def release_terminal_input_request_from_wire(value: Any) -> ReleaseTerminalInputRequest:
    data = _strict_fields(
        value,
        required={"session_id", "attachment_id"},
        context="terminal release input request",
    )
    return ReleaseTerminalInputRequest(
        session_id=_session_id(data["session_id"], "session id"),
        attachment_id=AttachmentId(_identifier(data["attachment_id"], "attachment id")),
    )


def replay_request_to_wire(request: ReplayRequest) -> Dict[str, Any]:
    if type(request) is not ReplayRequest:
        raise TypeError("terminal replay request is required")
    return {
        "session_id": request.session_id,
        "attachment_id": request.attachment_id,
        "after_sequence": request.after_sequence,
        "max_bytes": request.max_bytes,
    }


def replay_request_from_wire(value: Any) -> ReplayRequest:
    data = _strict_fields(
        value,
        required={"session_id", "attachment_id", "after_sequence", "max_bytes"},
        context="terminal replay request",
    )
    after = data["after_sequence"]
    if after is not None:
        after = _integer(after, "terminal replay sequence")
    return ReplayRequest(
        session_id=_session_id(data["session_id"], "session id"),
        attachment_id=AttachmentId(_identifier(data["attachment_id"], "attachment id")),
        after_sequence=after,
        max_bytes=_integer(data["max_bytes"], "terminal replay byte limit"),
    )


def replay_result_to_wire(result: ReplayResult) -> Dict[str, Any]:
    if type(result) is not ReplayResult:
        raise TypeError("terminal replay result is required")
    return {
        "session_id": result.session_id,
        "first_sequence": result.first_sequence,
        "next_sequence": result.next_sequence,
        "bounds": {
            "earliest_sequence": result.bounds.earliest_sequence,
            "latest_sequence": result.bounds.latest_sequence,
            "retained_bytes": result.bounds.retained_bytes,
        },
        "truncated": result.truncated,
        "eof": result.eof,
    }


def replay_result_from_wire(value: Any) -> ReplayResult:
    data = _strict_fields(
        value,
        required={
            "session_id",
            "first_sequence",
            "next_sequence",
            "bounds",
            "truncated",
            "eof",
        },
        context="terminal replay result",
    )
    bounds = _strict_fields(
        data["bounds"],
        required={"earliest_sequence", "latest_sequence", "retained_bytes"},
        context="terminal replay bounds",
    )
    return ReplayResult(
        session_id=_session_id(data["session_id"], "session id"),
        first_sequence=_integer(data["first_sequence"], "first sequence"),
        next_sequence=_integer(data["next_sequence"], "next sequence"),
        bounds=ReplayBounds(
            earliest_sequence=_integer(
                bounds["earliest_sequence"],
                "earliest sequence",
            ),
            latest_sequence=_integer(bounds["latest_sequence"], "latest sequence"),
            retained_bytes=_integer(bounds["retained_bytes"], "retained bytes"),
        ),
        truncated=_boolean(data["truncated"], "replay truncated"),
        eof=_boolean(data["eof"], "terminal eof"),
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


def _interaction_prompt_to_wire(
    interaction_type: InteractionType,
    prompt: HostKeyPrompt
    | PasswordPrompt
    | PassphrasePrompt
    | ChallengePrompt
    | PresencePrompt
    | ConfirmationPrompt,
) -> Dict[str, Any]:
    if interaction_type is InteractionType.HOST_KEY_CONFIRMATION:
        if type(prompt) is not HostKeyPrompt:
            raise TypeError("host-key interaction requires a host-key prompt")
        return {
            "hostname": prompt.hostname,
            "port": prompt.port,
            "key_type": prompt.key_type,
            "fingerprint": prompt.fingerprint,
            "status": prompt.status.value,
        }
    if interaction_type is InteractionType.PASSWORD:
        if type(prompt) is not PasswordPrompt:
            raise TypeError("password interaction requires a password prompt")
        return {
            "username": prompt.username,
            "hostname": prompt.hostname,
            "port": prompt.port,
            "attempt": prompt.attempt,
            "can_remember": prompt.can_remember,
            "saved_value_available": prompt.stored_secret_available,
        }
    if interaction_type is InteractionType.PRIVATE_KEY_PASSPHRASE:
        if type(prompt) is not PassphrasePrompt:
            raise TypeError("passphrase interaction requires a passphrase prompt")
        return {
            "key_display_name": prompt.key_display_name,
            "key_fingerprint": prompt.key_fingerprint,
            "attempt": prompt.attempt,
            "can_remember": prompt.can_remember,
            "saved_value_available": prompt.stored_secret_available,
            "confirmation_required": prompt.confirmation_required,
        }
    if interaction_type is InteractionType.KEYBOARD_INTERACTIVE:
        if type(prompt) is not ChallengePrompt:
            raise TypeError("challenge interaction requires a challenge prompt")
        return {"text": prompt.text, "attempt": prompt.attempt}
    if interaction_type is InteractionType.SECURITY_KEY_PRESENCE:
        if type(prompt) is not PresencePrompt:
            raise TypeError("presence interaction requires a presence prompt")
        return {"text": prompt.text}
    if interaction_type is InteractionType.CONFIRMATION:
        if type(prompt) is not ConfirmationPrompt:
            raise TypeError("confirmation interaction requires a confirmation prompt")
        return {"text": prompt.text}
    raise TypeError("unsupported interaction type")


def _interaction_prompt_from_wire(
    interaction_type: InteractionType,
    value: Any,
) -> (
    HostKeyPrompt
    | PasswordPrompt
    | PassphrasePrompt
    | ChallengePrompt
    | PresencePrompt
    | ConfirmationPrompt
):
    if interaction_type is InteractionType.HOST_KEY_CONFIRMATION:
        data = _strict_fields(
            value,
            required={"hostname", "port", "key_type", "fingerprint", "status"},
            context="host-key prompt",
        )
        return HostKeyPrompt(
            hostname=_text(data["hostname"], "host-key hostname"),
            port=_integer(data["port"], "host-key port"),
            key_type=_text(data["key_type"], "host-key type"),
            fingerprint=_text(data["fingerprint"], "host-key fingerprint"),
            status=HostKeyStatus(_identifier(data["status"], "host-key status")),
        )
    if interaction_type is InteractionType.PASSWORD:
        data = _strict_fields(
            value,
            required={
                "username",
                "hostname",
                "port",
                "attempt",
                "can_remember",
                "saved_value_available",
            },
            context="password prompt",
        )
        return PasswordPrompt(
            username=_text(data["username"], "password username"),
            hostname=_text(data["hostname"], "password hostname"),
            port=_integer(data["port"], "password port"),
            attempt=_integer(data["attempt"], "password attempt"),
            can_remember=_boolean(data["can_remember"], "password remember support"),
            stored_secret_available=_boolean(
                data["saved_value_available"],
                "password stored-secret availability",
            ),
        )
    if interaction_type is InteractionType.PRIVATE_KEY_PASSPHRASE:
        data = _strict_fields(
            value,
            required={
                "key_display_name",
                "key_fingerprint",
                "attempt",
                "can_remember",
                "saved_value_available",
            },
            optional={"confirmation_required"},
            context="passphrase prompt",
        )
        fingerprint = data["key_fingerprint"]
        if fingerprint is not None:
            fingerprint = _text(fingerprint, "key fingerprint")
        return PassphrasePrompt(
            key_display_name=_text(data["key_display_name"], "key display name"),
            key_fingerprint=fingerprint,
            attempt=_integer(data["attempt"], "passphrase attempt"),
            can_remember=_boolean(data["can_remember"], "passphrase remember support"),
            stored_secret_available=_boolean(
                data["saved_value_available"],
                "passphrase stored-secret availability",
            ),
            confirmation_required=_boolean(
                data.get("confirmation_required", False),
                "passphrase confirmation requirement",
            ),
        )
    if interaction_type is InteractionType.KEYBOARD_INTERACTIVE:
        data = _strict_fields(value, required={"text", "attempt"}, context="challenge prompt")
        return ChallengePrompt(
            text=_text(data["text"], "challenge prompt"),
            attempt=_integer(data["attempt"], "challenge attempt"),
        )
    if interaction_type is InteractionType.SECURITY_KEY_PRESENCE:
        data = _strict_fields(value, required={"text"}, context="presence prompt")
        return PresencePrompt(text=_text(data["text"], "presence prompt"))
    if interaction_type is InteractionType.CONFIRMATION:
        data = _strict_fields(value, required={"text"}, context="confirmation prompt")
        return ConfirmationPrompt(text=_text(data["text"], "confirmation prompt"))
    raise ValueError("unsupported interaction type")


def interaction_summary_to_wire(summary: InteractionSummary) -> Dict[str, Any]:
    if type(summary) is not InteractionSummary:
        raise TypeError("interaction summary is required")
    return {
        "id": summary.id,
        "session_id": summary.session_id,
        "connection_id": summary.connection_id,
        "type": summary.type.value,
        "state": summary.state.value,
        "created_at": _datetime_to_wire(summary.created_at, "interaction creation time"),
        "expires_at": _datetime_to_wire(summary.expires_at, "interaction expiry time"),
        "attempt": summary.attempt,
        "metadata": _interaction_prompt_to_wire(summary.type, summary.prompt),
        "responder_client_id": summary.responder_client_id,
    }


def interaction_summary_from_wire(value: Any) -> InteractionSummary:
    data = _strict_fields(
        value,
        required={
            "id",
            "session_id",
            "connection_id",
            "type",
            "state",
            "created_at",
            "expires_at",
            "attempt",
            "metadata",
            "responder_client_id",
        },
        context="interaction summary",
    )
    interaction_type = InteractionType(_identifier(data["type"], "interaction type"))
    responder = data["responder_client_id"]
    return InteractionSummary(
        id=InteractionId(_identifier(data["id"], "interaction id")),
        session_id=_interaction_scope_id(data["session_id"], "session id"),
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        type=interaction_type,
        state=InteractionState(_identifier(data["state"], "interaction state")),
        created_at=_datetime_from_wire(data["created_at"], "interaction creation time"),
        expires_at=_datetime_from_wire(data["expires_at"], "interaction expiry time"),
        attempt=_integer(data["attempt"], "interaction attempt"),
        prompt=_interaction_prompt_from_wire(interaction_type, data["metadata"]),
        responder_client_id=(
            ClientId(_identifier(responder, "responder client id"))
            if responder is not None
            else None
        ),
    )


def interaction_claim_to_wire(claim: InteractionClaim) -> Dict[str, Any]:
    if type(claim) is not InteractionClaim:
        raise TypeError("interaction claim is required")
    return {
        "interaction_id": claim.interaction_id,
        "responder_client_id": claim.responder_client_id,
        "nonce": claim.nonce,
        "expires_at": _datetime_to_wire(claim.expires_at, "claim expiry time"),
    }


def interaction_claim_from_wire(value: Any) -> InteractionClaim:
    data = _strict_fields(
        value,
        required={
            "interaction_id",
            "responder_client_id",
            "nonce",
            "expires_at",
        },
        context="interaction claim",
    )
    return InteractionClaim(
        interaction_id=InteractionId(_identifier(data["interaction_id"], "interaction id")),
        responder_client_id=ClientId(
            _identifier(data["responder_client_id"], "responder client id")
        ),
        nonce=_identifier(data["nonce"], "interaction nonce"),
        expires_at=_datetime_from_wire(data["expires_at"], "claim expiry time"),
    )


def interaction_decision_to_wire(
    request: InteractionDecisionRequest,
) -> Dict[str, Any]:
    if type(request) is not InteractionDecisionRequest:
        raise TypeError("interaction decision request is required")
    return {
        "interaction_id": request.interaction_id,
        "host_key_decision": (
            request.host_key_decision.value if request.host_key_decision is not None else None
        ),
        "value_action": (
            request.secret_decision.value if request.secret_decision is not None else None
        ),
        "remember_policy": request.remember_policy.value,
    }


def interaction_decision_from_wire(value: Any) -> InteractionDecisionRequest:
    data = _strict_fields(
        value,
        required={
            "interaction_id",
            "host_key_decision",
            "value_action",
            "remember_policy",
        },
        context="interaction decision",
    )
    host_key = data["host_key_decision"]
    secret = data["value_action"]
    return InteractionDecisionRequest(
        interaction_id=InteractionId(_identifier(data["interaction_id"], "interaction id")),
        host_key_decision=(
            HostKeyDecision(_identifier(host_key, "host-key decision"))
            if host_key is not None
            else None
        ),
        secret_decision=(
            SecretDecision(_identifier(secret, "secret decision")) if secret is not None else None
        ),
        remember_policy=RememberPolicy(_identifier(data["remember_policy"], "remember policy")),
    )


# -- SFTP / transfer / forward (Phase 10) ------------------------------------


def _service_failure_to_wire(failure: Optional[ServiceFailure]) -> Optional[Dict[str, Any]]:
    if failure is None:
        return None
    if type(failure) is not ServiceFailure:
        raise TypeError("service failure must be a ServiceFailure or None")
    return {"code": failure.code, "message": failure.message}


def _service_failure_from_wire(value: Any) -> Optional[ServiceFailure]:
    if value is None:
        return None
    data = _strict_fields(value, required={"code", "message"}, context="service failure")
    return ServiceFailure(
        code=_identifier(data["code"], "service failure code"),
        message=_identifier(data["message"], "service failure message"),
    )


def _optional_datetime_to_wire(value: Optional[datetime], context: str) -> Optional[str]:
    return _datetime_to_wire(value, context) if value is not None else None


def _optional_datetime_from_wire(value: Any, context: str) -> Optional[datetime]:
    return _datetime_from_wire(value, context) if value is not None else None


def _optional_client_id(value: Any, context: str) -> Optional[ClientId]:
    if value is None:
        return None
    return ClientId(_identifier(value, context))


def sftp_service_summary_to_wire(summary: SftpServiceSummary) -> Dict[str, Any]:
    if type(summary) is not SftpServiceSummary:
        raise TypeError("SFTP service summary is required")
    return {
        "id": summary.id,
        "connection_id": summary.connection_id,
        "state": summary.state.value,
        "created_at": _datetime_to_wire(summary.created_at, "SFTP service creation time"),
        "started_at": _optional_datetime_to_wire(summary.started_at, "SFTP service start time"),
        "closed_at": _optional_datetime_to_wire(summary.closed_at, "SFTP service close time"),
        "attachment_count": summary.attachment_count,
        "owner_client_id": summary.owner_client_id,
        "failure": _service_failure_to_wire(summary.failure),
    }


def sftp_service_summary_from_wire(value: Any) -> SftpServiceSummary:
    data = _strict_fields(
        value,
        required={
            "id",
            "connection_id",
            "state",
            "created_at",
            "started_at",
            "closed_at",
            "attachment_count",
            "owner_client_id",
            "failure",
        },
        context="SFTP service summary",
    )
    try:
        state = SftpServiceState(data["state"])
    except (TypeError, ValueError):
        raise ValueError("SFTP service summary contains an unknown state") from None
    return SftpServiceSummary(
        id=_sftp_service_id(data["id"], "SFTP service id"),
        connection_id=ConnectionId(
            _identifier(data["connection_id"], "SFTP service connection id")
        ),
        state=state,
        created_at=_datetime_from_wire(data["created_at"], "SFTP service creation time"),
        started_at=_optional_datetime_from_wire(data["started_at"], "SFTP service start time"),
        closed_at=_optional_datetime_from_wire(data["closed_at"], "SFTP service close time"),
        attachment_count=_integer(data["attachment_count"], "SFTP attachment count"),
        owner_client_id=_optional_client_id(data["owner_client_id"], "SFTP owner client id"),
        failure=_service_failure_from_wire(data["failure"]),
    )


def open_sftp_request_to_wire(request: OpenSftpRequest) -> Dict[str, Any]:
    if type(request) is not OpenSftpRequest:
        raise TypeError("open SFTP request is required")
    return {"connection_id": request.connection_id}


def open_sftp_request_from_wire(value: Any) -> OpenSftpRequest:
    data = _strict_fields(value, required={"connection_id"}, context="open SFTP request")
    return OpenSftpRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id"))
    )


def close_sftp_request_to_wire(request: CloseSftpRequest) -> Dict[str, Any]:
    if type(request) is not CloseSftpRequest:
        raise TypeError("close SFTP request is required")
    return {"service_id": request.service_id}


def close_sftp_request_from_wire(value: Any) -> CloseSftpRequest:
    data = _strict_fields(value, required={"service_id"}, context="close SFTP request")
    return CloseSftpRequest(service_id=_sftp_service_id(data["service_id"], "SFTP service id"))


def attach_sftp_request_to_wire(request: AttachSftpRequest) -> Dict[str, Any]:
    if type(request) is not AttachSftpRequest:
        raise TypeError("attach SFTP request is required")
    return {"service_id": request.service_id}


def attach_sftp_request_from_wire(value: Any) -> AttachSftpRequest:
    data = _strict_fields(value, required={"service_id"}, context="attach SFTP request")
    return AttachSftpRequest(service_id=_sftp_service_id(data["service_id"], "SFTP service id"))


def remote_file_entry_to_wire(entry: RemoteFileEntry) -> Dict[str, Any]:
    if type(entry) is not RemoteFileEntry:
        raise TypeError("remote file entry is required")
    return {
        "name": entry.name,
        "path": entry.path,
        "file_type": entry.file_type.value,
        "size": entry.size,
        "mode": entry.mode,
        "uid": entry.uid,
        "gid": entry.gid,
        "modified_at": _optional_datetime_to_wire(entry.modified_at, "remote file mtime"),
        "link_target": entry.link_target,
    }


def remote_file_entry_from_wire(value: Any) -> RemoteFileEntry:
    data = _strict_fields(
        value,
        required={
            "name",
            "path",
            "file_type",
            "size",
            "mode",
            "uid",
            "gid",
            "modified_at",
            "link_target",
        },
        context="remote file entry",
    )
    try:
        file_type = RemoteFileType(data["file_type"])
    except (TypeError, ValueError):
        raise ValueError("remote file entry contains an unknown file type") from None
    size = data["size"]
    if size is not None:
        size = _integer(size, "remote file size")
    mode = data["mode"]
    if mode is not None:
        mode = _integer(mode, "remote file mode")
    uid = data["uid"]
    if uid is not None:
        uid = _integer(uid, "remote file uid")
    gid = data["gid"]
    if gid is not None:
        gid = _integer(gid, "remote file gid")
    link_target = data["link_target"]
    if link_target is not None:
        link_target = _text(link_target, "remote file link target")
    return RemoteFileEntry(
        name=_text(data["name"], "remote file name"),
        path=_text(data["path"], "remote file path"),
        file_type=file_type,
        size=size,
        mode=mode,
        uid=uid,
        gid=gid,
        modified_at=_optional_datetime_from_wire(data["modified_at"], "remote file mtime"),
        link_target=link_target,
    )


def list_directory_request_to_wire(request: ListDirectoryRequest) -> Dict[str, Any]:
    if type(request) is not ListDirectoryRequest:
        raise TypeError("list directory request is required")
    return {
        "connection_id": request.connection_id,
        "path": request.path,
        "service_id": request.service_id,
        "cursor": request.cursor,
        "limit": request.limit,
    }


def list_directory_request_from_wire(value: Any) -> ListDirectoryRequest:
    data = _strict_fields(
        value,
        required={"connection_id", "path"},
        optional={"service_id", "cursor", "limit"},
        context="list directory request",
    )
    service_id = data.get("service_id")
    limit = data.get("limit")
    if limit is not None:
        limit = _integer(limit, "SFTP list limit")
    cursor = data.get("cursor")
    if cursor is not None:
        cursor = _text(cursor, "SFTP list cursor", allow_empty=True)
    return ListDirectoryRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        path=_text(data["path"], "SFTP path"),
        service_id=(
            _sftp_service_id(service_id, "SFTP service id") if service_id is not None else None
        ),
        cursor=cursor,
        limit=limit,
    )


def list_directory_result_to_wire(result: ListDirectoryResult) -> Dict[str, Any]:
    if type(result) is not ListDirectoryResult:
        raise TypeError("list directory result is required")
    return {
        "path": result.path,
        "entries": [remote_file_entry_to_wire(entry) for entry in result.entries],
        "truncated": result.truncated,
        "next_cursor": result.next_cursor,
    }


def list_directory_result_from_wire(value: Any) -> ListDirectoryResult:
    data = _strict_fields(
        value,
        required={"path", "entries", "truncated", "next_cursor"},
        context="list directory result",
    )
    entries = data["entries"]
    if type(entries) is not list:
        raise ValueError("list directory entries must be an array")
    next_cursor = data["next_cursor"]
    if next_cursor is not None:
        next_cursor = _text(next_cursor, "SFTP list next cursor", allow_empty=True)
    return ListDirectoryResult(
        path=_text(data["path"], "list directory path"),
        entries=tuple(remote_file_entry_from_wire(item) for item in entries),
        truncated=_boolean(data["truncated"], "truncated"),
        next_cursor=next_cursor,
    )


def _sftp_file_target_to_wire(target: SftpFileTarget) -> str:
    if not isinstance(target, SftpFileTarget):
        raise TypeError("SFTP file target is required")
    return target.value


def _sftp_file_target_from_wire(value: Any) -> SftpFileTarget:
    try:
        return SftpFileTarget(_identifier(value, "SFTP file target"))
    except (TypeError, ValueError):
        raise ValueError("SFTP file target is unknown") from None


def _sftp_file_access_from_wire(value: Any) -> "SftpFileAccess":
    try:
        return SftpFileAccess(_identifier(value, "SFTP file access"))
    except (TypeError, ValueError):
        raise ValueError("SFTP file access is unknown") from None


def sftp_read_file_request_to_wire(request: SftpReadFileRequest) -> Dict[str, Any]:
    if type(request) is not SftpReadFileRequest:
        raise TypeError("SFTP read file request is required")
    return {
        "target": _sftp_file_target_to_wire(request.target),
        "path": request.path,
        "service_id": request.service_id,
        "access": request.access.value,
    }


def sftp_read_file_request_from_wire(value: Any) -> SftpReadFileRequest:
    data = _strict_fields(
        value,
        required={"target", "path"},
        optional={"service_id", "access"},
        context="SFTP read file request",
    )
    service_id = data.get("service_id")
    access = data.get("access", SftpFileAccess.NORMAL.value)
    return SftpReadFileRequest(
        target=_sftp_file_target_from_wire(data["target"]),
        path=_text(data["path"], "SFTP file path"),
        service_id=(
            _sftp_service_id(service_id, "SFTP service id") if service_id is not None else None
        ),
        access=_sftp_file_access_from_wire(access),
    )


def sftp_read_file_result_to_wire(result: SftpReadFileResult) -> Dict[str, Any]:
    if type(result) is not SftpReadFileResult:
        raise TypeError("SFTP read file result is required")
    return {
        "target": _sftp_file_target_to_wire(result.target),
        "path": result.path,
        "content": result.content,
        "exists": result.exists,
        "revision": result.revision,
        "size": result.size,
        "mode": result.mode,
    }


def sftp_read_file_result_from_wire(value: Any) -> SftpReadFileResult:
    data = _strict_fields(
        value,
        required={"target", "path", "content", "exists", "revision", "size", "mode"},
        context="SFTP read file result",
    )
    mode = data["mode"]
    return SftpReadFileResult(
        target=_sftp_file_target_from_wire(data["target"]),
        path=_text(data["path"], "SFTP file result path"),
        content=_text(data["content"], "SFTP file content", allow_empty=True),
        exists=_boolean(data["exists"], "SFTP file exists"),
        revision=_identifier(data["revision"], "file revision"),
        size=_integer(data["size"], "SFTP file size"),
        mode=None if mode is None else _integer(mode, "SFTP file mode"),
    )


def sftp_replace_file_request_to_wire(request: SftpReplaceFileRequest) -> Dict[str, Any]:
    if type(request) is not SftpReplaceFileRequest:
        raise TypeError("SFTP replace file request is required")
    return {
        "target": _sftp_file_target_to_wire(request.target),
        "path": request.path,
        "content": request.content,
        "expected_revision": request.expected_revision,
        "backup": request.backup,
        "service_id": request.service_id,
        "access": request.access.value,
    }


def sftp_replace_file_request_from_wire(value: Any) -> SftpReplaceFileRequest:
    data = _strict_fields(
        value,
        required={"target", "path", "content", "expected_revision", "backup"},
        optional={"service_id", "access"},
        context="SFTP replace file request",
    )
    service_id = data.get("service_id")
    access = data.get("access", SftpFileAccess.NORMAL.value)
    return SftpReplaceFileRequest(
        target=_sftp_file_target_from_wire(data["target"]),
        path=_text(data["path"], "SFTP file path"),
        content=_text(data["content"], "SFTP file content", allow_empty=True),
        expected_revision=_identifier(data["expected_revision"], "expected file revision"),
        backup=_boolean(data["backup"], "SFTP file backup"),
        service_id=(
            _sftp_service_id(service_id, "SFTP service id") if service_id is not None else None
        ),
        access=_sftp_file_access_from_wire(access),
    )


def sftp_replace_file_result_to_wire(result: SftpReplaceFileResult) -> Dict[str, Any]:
    if type(result) is not SftpReplaceFileResult:
        raise TypeError("SFTP replace file result is required")
    return {
        "target": _sftp_file_target_to_wire(result.target),
        "path": result.path,
        "revision": result.revision,
        "size": result.size,
        "backup_path": result.backup_path,
    }


def sftp_replace_file_result_from_wire(value: Any) -> SftpReplaceFileResult:
    data = _strict_fields(
        value,
        required={"target", "path", "revision", "size", "backup_path"},
        context="SFTP replace file result",
    )
    backup_path = data["backup_path"]
    if backup_path is not None:
        backup_path = _text(backup_path, "SFTP backup path", allow_empty=False)
    return SftpReplaceFileResult(
        target=_sftp_file_target_from_wire(data["target"]),
        path=_text(data["path"], "SFTP replacement path"),
        revision=_identifier(data["revision"], "new file revision"),
        size=_integer(data["size"], "SFTP replacement size"),
        backup_path=backup_path,
    )


def sftp_path_request_to_wire(request: SftpPathRequest) -> Dict[str, Any]:
    if type(request) is not SftpPathRequest:
        raise TypeError("SFTP path request is required")
    wire = {"service_id": request.service_id, "path": request.path}
    if request.recursive:
        wire["recursive"] = True
    return wire


def sftp_path_request_from_wire(value: Any) -> SftpPathRequest:
    data = _strict_fields(
        value,
        required={"service_id", "path"},
        optional={"recursive"},
        context="SFTP path request",
    )
    return SftpPathRequest(
        service_id=_sftp_service_id(data["service_id"], "SFTP service id"),
        path=_text(data["path"], "SFTP path"),
        recursive=(
            _boolean(data["recursive"], "SFTP recursive flag") if "recursive" in data else False
        ),
    )


def sftp_create_file_request_to_wire(request: SftpCreateFileRequest) -> Dict[str, Any]:
    if type(request) is not SftpCreateFileRequest:
        raise TypeError("SFTP create file request is required")
    return {"service_id": request.service_id, "path": request.path}


def sftp_create_file_request_from_wire(value: Any) -> SftpCreateFileRequest:
    data = _strict_fields(
        value,
        required={"service_id", "path"},
        context="SFTP create file request",
    )
    return SftpCreateFileRequest(
        service_id=_sftp_service_id(data["service_id"], "SFTP service id"),
        path=_text(data["path"], "SFTP create path"),
    )


def sftp_create_file_result_to_wire(result: SftpCreateFileResult) -> Dict[str, Any]:
    if type(result) is not SftpCreateFileResult:
        raise TypeError("SFTP create file result is required")
    return {"path": result.path, "mode": result.mode}


def sftp_create_file_result_from_wire(value: Any) -> SftpCreateFileResult:
    data = _strict_fields(
        value,
        required={"path", "mode"},
        context="SFTP create file result",
    )
    return SftpCreateFileResult(
        path=_text(data["path"], "SFTP create result path"),
        mode=_integer(data["mode"], "SFTP create result mode"),
    )


def sftp_directory_size_request_to_wire(request: SftpDirectorySizeRequest) -> Dict[str, Any]:
    if type(request) is not SftpDirectorySizeRequest:
        raise TypeError("SFTP directory size request is required")
    return {"service_id": request.service_id, "path": request.path}


def sftp_directory_size_request_from_wire(value: Any) -> SftpDirectorySizeRequest:
    data = _strict_fields(
        value,
        required={"service_id", "path"},
        context="SFTP directory size request",
    )
    return SftpDirectorySizeRequest(
        service_id=_sftp_service_id(data["service_id"], "SFTP service id"),
        path=_text(data["path"], "SFTP size path"),
    )


def sftp_directory_size_result_to_wire(result: SftpDirectorySizeResult) -> Dict[str, Any]:
    if type(result) is not SftpDirectorySizeResult:
        raise TypeError("SFTP directory size result is required")
    return {
        "path": result.path,
        "size_bytes": result.size_bytes,
        "file_count": result.file_count,
        "directory_count": result.directory_count,
    }


def sftp_directory_size_result_from_wire(value: Any) -> SftpDirectorySizeResult:
    data = _strict_fields(
        value,
        required={"path", "size_bytes", "file_count", "directory_count"},
        context="SFTP directory size result",
    )
    return SftpDirectorySizeResult(
        path=_text(data["path"], "SFTP size result path"),
        size_bytes=_integer(data["size_bytes"], "SFTP size result size bytes"),
        file_count=_integer(data["file_count"], "SFTP size result file count"),
        directory_count=_integer(data["directory_count"], "SFTP size result directory count"),
    )


def sftp_rename_request_to_wire(request: SftpRenameRequest) -> Dict[str, Any]:
    if type(request) is not SftpRenameRequest:
        raise TypeError("SFTP rename request is required")
    return {
        "service_id": request.service_id,
        "source_path": request.source_path,
        "destination_path": request.destination_path,
        "overwrite": request.overwrite,
    }


def sftp_rename_request_from_wire(value: Any) -> SftpRenameRequest:
    data = _strict_fields(
        value,
        required={"service_id", "source_path", "destination_path"},
        optional={"overwrite"},
        context="SFTP rename request",
    )
    overwrite = data.get("overwrite", False)
    return SftpRenameRequest(
        service_id=_sftp_service_id(data["service_id"], "SFTP service id"),
        source_path=_text(data["source_path"], "SFTP rename source path"),
        destination_path=_text(data["destination_path"], "SFTP rename destination path"),
        overwrite=_boolean(overwrite, "SFTP rename overwrite"),
    )


def sftp_copy_request_to_wire(request: SftpCopyRequest) -> Dict[str, Any]:
    if type(request) is not SftpCopyRequest:
        raise TypeError("SFTP copy request is required")
    return {
        "service_id": request.service_id,
        "source_path": request.source_path,
        "destination_path": request.destination_path,
        "recursive": request.recursive,
        "move": request.move,
    }


def sftp_copy_request_from_wire(value: Any) -> SftpCopyRequest:
    data = _strict_fields(
        value,
        required={"service_id", "source_path", "destination_path"},
        optional={"recursive", "move"},
        context="SFTP copy request",
    )
    return SftpCopyRequest(
        service_id=_sftp_service_id(data["service_id"], "SFTP service id"),
        source_path=_text(data["source_path"], "SFTP copy source path"),
        destination_path=_text(data["destination_path"], "SFTP copy destination path"),
        recursive=_boolean(data.get("recursive", False), "SFTP copy recursive"),
        move=_boolean(data.get("move", False), "SFTP copy move"),
    )


def sftp_chmod_request_to_wire(request: SftpChmodRequest) -> Dict[str, Any]:
    if type(request) is not SftpChmodRequest:
        raise TypeError("SFTP chmod request is required")
    return {"service_id": request.service_id, "path": request.path, "mode": request.mode}


def sftp_chmod_request_from_wire(value: Any) -> SftpChmodRequest:
    data = _strict_fields(
        value,
        required={"service_id", "path", "mode"},
        context="SFTP chmod request",
    )
    return SftpChmodRequest(
        service_id=_sftp_service_id(data["service_id"], "SFTP service id"),
        path=_text(data["path"], "SFTP path"),
        mode=_integer(data["mode"], "SFTP chmod mode"),
    )


def sftp_symlink_request_to_wire(request: SftpSymlinkRequest) -> Dict[str, Any]:
    if type(request) is not SftpSymlinkRequest:
        raise TypeError("SFTP symlink request is required")
    return {
        "service_id": request.service_id,
        "target_path": request.target_path,
        "link_path": request.link_path,
    }


def sftp_symlink_request_from_wire(value: Any) -> SftpSymlinkRequest:
    data = _strict_fields(
        value,
        required={"service_id", "target_path", "link_path"},
        context="SFTP symlink request",
    )
    return SftpSymlinkRequest(
        service_id=_sftp_service_id(data["service_id"], "SFTP service id"),
        target_path=_text(data["target_path"], "SFTP symlink target path"),
        link_path=_text(data["link_path"], "SFTP symlink link path"),
    )


def transfer_summary_to_wire(summary: TransferSummary) -> Dict[str, Any]:
    if type(summary) is not TransferSummary:
        raise TypeError("transfer summary is required")
    return {
        "id": summary.id,
        "connection_id": summary.connection_id,
        "sftp_service_id": summary.sftp_service_id,
        "backend": summary.backend.value,
        "direction": summary.direction.value,
        "state": summary.state.value,
        "source_display": summary.source_display,
        "destination_display": summary.destination_display,
        "bytes_total": summary.bytes_total,
        "bytes_completed": summary.bytes_completed,
        "created_at": _datetime_to_wire(summary.created_at, "transfer creation time"),
        "started_at": _optional_datetime_to_wire(summary.started_at, "transfer start time"),
        "completed_at": _optional_datetime_to_wire(
            summary.completed_at, "transfer completion time"
        ),
        "owner_client_id": summary.owner_client_id,
        "failure": _service_failure_to_wire(summary.failure),
    }


def transfer_summary_from_wire(value: Any) -> TransferSummary:
    data = _strict_fields(
        value,
        required={
            "id",
            "connection_id",
            "direction",
            "state",
            "source_display",
            "destination_display",
            "bytes_total",
            "bytes_completed",
            "created_at",
            "started_at",
            "completed_at",
            "owner_client_id",
            "failure",
        },
        optional={"sftp_service_id", "backend"},
        context="transfer summary",
    )
    try:
        backend = TransferBackend(data.get("backend", TransferBackend.SFTP.value))
    except (TypeError, ValueError):
        raise ValueError("transfer summary contains an unknown backend") from None
    try:
        direction = TransferDirection(data["direction"])
    except (TypeError, ValueError):
        raise ValueError("transfer summary contains an unknown direction") from None
    try:
        state = TransferState(data["state"])
    except (TypeError, ValueError):
        raise ValueError("transfer summary contains an unknown state") from None
    bytes_total = data["bytes_total"]
    if bytes_total is not None:
        bytes_total = _integer(bytes_total, "transfer total bytes")
    return TransferSummary(
        id=_transfer_id(data["id"], "transfer id"),
        connection_id=ConnectionId(_identifier(data["connection_id"], "transfer connection id")),
        sftp_service_id=(
            _sftp_service_id(data["sftp_service_id"], "transfer SFTP service id")
            if data.get("sftp_service_id") is not None
            else None
        ),
        backend=backend,
        direction=direction,
        state=state,
        source_display=_text(data["source_display"], "transfer source display"),
        destination_display=_text(data["destination_display"], "transfer destination display"),
        bytes_total=bytes_total,
        bytes_completed=_integer(data["bytes_completed"], "transfer completed bytes"),
        created_at=_datetime_from_wire(data["created_at"], "transfer creation time"),
        started_at=_optional_datetime_from_wire(data["started_at"], "transfer start time"),
        completed_at=_optional_datetime_from_wire(data["completed_at"], "transfer completion time"),
        owner_client_id=_optional_client_id(data["owner_client_id"], "transfer owner client id"),
        failure=_service_failure_from_wire(data["failure"]),
    )


def start_transfer_request_to_wire(request: StartTransferRequest) -> Dict[str, Any]:
    if type(request) is not StartTransferRequest:
        raise TypeError("start transfer request is required")
    return {
        "connection_id": request.connection_id,
        "sftp_service_id": request.sftp_service_id,
        "direction": request.direction.value,
        "remote_path": request.remote_path,
        "local_path": request.local_path,
        "conflict_policy": request.conflict_policy.value,
        "recursive": request.recursive,
        "local_mode": request.local_mode.value,
    }


def start_transfer_request_from_wire(value: Any) -> StartTransferRequest:
    data = _strict_fields(
        value,
        required={
            "connection_id",
            "sftp_service_id",
            "direction",
            "remote_path",
            "local_path",
        },
        optional={"conflict_policy", "recursive", "local_mode"},
        context="start transfer request",
    )
    try:
        direction = TransferDirection(_identifier(data["direction"], "transfer direction"))
    except (TypeError, ValueError):
        raise ValueError("start transfer request contains an unknown direction") from None
    conflict_policy = data.get("conflict_policy")
    try:
        conflict_policy = (
            TransferConflictPolicy(conflict_policy)
            if conflict_policy is not None
            else TransferConflictPolicy.FAIL
        )
    except ValueError:
        raise ValueError("start transfer request contains an unknown conflict policy") from None
    local_mode = data.get("local_mode")
    try:
        local_mode = (
            TransferLocalMode(local_mode)
            if local_mode is not None
            else TransferLocalMode.DAEMON_PATH
        )
    except ValueError:
        raise ValueError("start transfer request contains an unknown local mode") from None
    recursive = data.get("recursive", False)
    return StartTransferRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        sftp_service_id=_sftp_service_id(data["sftp_service_id"], "transfer SFTP service id"),
        direction=direction,
        remote_path=_text(data["remote_path"], "transfer remote path"),
        local_path=_text(data["local_path"], "transfer local path"),
        conflict_policy=conflict_policy,
        recursive=_boolean(recursive, "transfer recursive flag"),
        local_mode=local_mode,
    )


def scp_transfer_request_to_wire(
    request: StartScpTransferRequest,
) -> Dict[str, Any]:
    if type(request) is not StartScpTransferRequest:
        raise TypeError("SCP transfer request is required")
    return {
        "connection_id": request.connection_id,
        "direction": request.direction.value,
        "sources": list(request.sources),
        "destination": request.destination,
        "conflict_policy": request.conflict_policy.value,
        "recursive": request.recursive,
    }


def scp_transfer_request_from_wire(value: Any) -> StartScpTransferRequest:
    data = _strict_fields(
        value,
        required={"connection_id", "direction", "sources", "destination"},
        optional={"conflict_policy", "recursive"},
        context="SCP transfer request",
    )
    sources = data["sources"]
    if type(sources) is not list:
        raise ValueError("SCP transfer sources must be an array")
    try:
        direction = TransferDirection(_identifier(data["direction"], "transfer direction"))
    except (TypeError, ValueError):
        raise ValueError("SCP transfer request contains an unknown direction") from None
    try:
        conflict_policy = TransferConflictPolicy(
            data.get("conflict_policy", TransferConflictPolicy.OVERWRITE.value)
        )
    except (TypeError, ValueError):
        raise ValueError("SCP transfer request contains an unknown conflict policy") from None
    return StartScpTransferRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        direction=direction,
        sources=tuple(_text(item, "SCP source path") for item in sources),
        destination=_text(data["destination"], "SCP destination path"),
        conflict_policy=conflict_policy,
        recursive=_boolean(data.get("recursive", False), "SCP recursive"),
    )


def cancel_transfer_request_to_wire(request: CancelTransferRequest) -> Dict[str, Any]:
    if type(request) is not CancelTransferRequest:
        raise TypeError("cancel transfer request is required")
    return {"transfer_id": request.transfer_id}


def cancel_transfer_request_from_wire(value: Any) -> CancelTransferRequest:
    data = _strict_fields(
        value,
        required={"transfer_id"},
        context="cancel transfer request",
    )
    return CancelTransferRequest(transfer_id=_transfer_id(data["transfer_id"], "transfer id"))


def forward_summary_to_wire(summary: ForwardSummary) -> Dict[str, Any]:
    if type(summary) is not ForwardSummary:
        raise TypeError("forward summary is required")
    return {
        "id": summary.id,
        "connection_id": summary.connection_id,
        "type": summary.type.value,
        "state": summary.state.value,
        "bind_host": summary.bind_host,
        "bind_port": summary.bind_port,
        "destination_host": summary.destination_host,
        "destination_port": summary.destination_port,
        "created_at": _datetime_to_wire(summary.created_at, "forward creation time"),
        "active_at": _optional_datetime_to_wire(summary.active_at, "forward active time"),
        "closed_at": _optional_datetime_to_wire(summary.closed_at, "forward close time"),
        "owner_client_id": summary.owner_client_id,
        "failure": _service_failure_to_wire(summary.failure),
        "session_id": summary.session_id,
    }


def forward_summary_from_wire(value: Any) -> ForwardSummary:
    data = _strict_fields(
        value,
        required={
            "id",
            "connection_id",
            "type",
            "state",
            "bind_host",
            "bind_port",
            "destination_host",
            "destination_port",
            "created_at",
            "active_at",
            "closed_at",
            "owner_client_id",
            "failure",
            "session_id",
        },
        context="forward summary",
    )
    try:
        forward_type = ForwardType(data["type"])
    except (TypeError, ValueError):
        raise ValueError("forward summary contains an unknown type") from None
    try:
        state = ForwardState(data["state"])
    except (TypeError, ValueError):
        raise ValueError("forward summary contains an unknown state") from None
    destination_host = data["destination_host"]
    if destination_host is not None:
        destination_host = _text(destination_host, "forward destination host")
    destination_port = data["destination_port"]
    if destination_port is not None:
        destination_port = _integer(destination_port, "forward destination port")
    session_id = data["session_id"]
    if session_id is not None:
        session_id = _session_id(session_id, "forward session id")
    return ForwardSummary(
        id=_forward_id(data["id"], "forward id"),
        connection_id=ConnectionId(_identifier(data["connection_id"], "forward connection id")),
        type=forward_type,
        state=state,
        bind_host=_text(data["bind_host"], "forward bind host"),
        bind_port=_integer(data["bind_port"], "forward bind port"),
        destination_host=destination_host,
        destination_port=destination_port,
        created_at=_datetime_from_wire(data["created_at"], "forward creation time"),
        active_at=_optional_datetime_from_wire(data["active_at"], "forward active time"),
        closed_at=_optional_datetime_from_wire(data["closed_at"], "forward close time"),
        owner_client_id=_optional_client_id(data["owner_client_id"], "forward owner client id"),
        failure=_service_failure_from_wire(data["failure"]),
        session_id=session_id,
    )


def open_forward_request_to_wire(request: OpenForwardRequest) -> Dict[str, Any]:
    if type(request) is not OpenForwardRequest:
        raise TypeError("open forward request is required")
    return {
        "connection_id": request.connection_id,
        "type": request.type.value,
        "bind_host": request.bind_host,
        "bind_port": request.bind_port,
        "destination_host": request.destination_host,
        "destination_port": request.destination_port,
    }


def open_forward_request_from_wire(value: Any) -> OpenForwardRequest:
    data = _strict_fields(
        value,
        required={"connection_id", "type", "bind_host", "bind_port"},
        optional={"destination_host", "destination_port"},
        context="open forward request",
    )
    try:
        forward_type = ForwardType(_identifier(data["type"], "forward type"))
    except (TypeError, ValueError):
        raise ValueError("open forward request contains an unknown type") from None
    destination_host = data.get("destination_host")
    if destination_host is not None:
        destination_host = _text(destination_host, "forward destination host")
    destination_port = data.get("destination_port")
    if destination_port is not None:
        destination_port = _integer(destination_port, "forward destination port")
    return OpenForwardRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        type=forward_type,
        bind_host=_text(data["bind_host"], "forward bind host"),
        bind_port=_integer(data["bind_port"], "forward bind port"),
        destination_host=destination_host,
        destination_port=destination_port,
    )


def close_forward_request_to_wire(request: CloseForwardRequest) -> Dict[str, Any]:
    if type(request) is not CloseForwardRequest:
        raise TypeError("close forward request is required")
    return {"forward_id": request.forward_id}


def close_forward_request_from_wire(value: Any) -> CloseForwardRequest:
    data = _strict_fields(
        value,
        required={"forward_id"},
        context="close forward request",
    )
    return CloseForwardRequest(forward_id=_forward_id(data["forward_id"], "forward id"))


def claim_forward_request_to_wire(request: ClaimForwardRequest) -> Dict[str, Any]:
    return {"forward_id": request.forward_id}


def claim_forward_request_from_wire(value: Any) -> ClaimForwardRequest:
    data = _strict_fields(
        value,
        required={"forward_id"},
        context="claim forward request",
    )
    return ClaimForwardRequest(forward_id=_forward_id(data["forward_id"], "forward id"))


def daemon_resource_counts_to_wire(counts: DaemonResourceCounts) -> Dict[str, Any]:
    if type(counts) is not DaemonResourceCounts:
        raise TypeError("daemon resource counts are required")
    return {
        "clients": counts.clients,
        "sessions_active": counts.sessions_active,
        "sessions_retained": counts.sessions_retained,
        "sftp_active": counts.sftp_active,
        "sftp_retained": counts.sftp_retained,
        "transfers_queued": counts.transfers_queued,
        "transfers_starting": counts.transfers_starting,
        "transfers_running": counts.transfers_running,
        "transfers_retained": counts.transfers_retained,
        "forwards_active": counts.forwards_active,
        "forwards_retained": counts.forwards_retained,
        "interactions_pending": counts.interactions_pending,
    }


def daemon_resource_counts_from_wire(value: Any) -> DaemonResourceCounts:
    data = _strict_fields(
        value,
        required={
            "clients",
            "sessions_active",
            "sessions_retained",
            "sftp_active",
            "sftp_retained",
            "transfers_queued",
            "transfers_starting",
            "transfers_running",
            "transfers_retained",
            "forwards_active",
            "forwards_retained",
            "interactions_pending",
        },
        context="daemon resource counts",
    )
    return DaemonResourceCounts(
        clients=_integer(data["clients"], "clients"),
        sessions_active=_integer(data["sessions_active"], "sessions_active"),
        sessions_retained=_integer(data["sessions_retained"], "sessions_retained"),
        sftp_active=_integer(data["sftp_active"], "sftp_active"),
        sftp_retained=_integer(data["sftp_retained"], "sftp_retained"),
        transfers_queued=_integer(data["transfers_queued"], "transfers_queued"),
        transfers_starting=_integer(data["transfers_starting"], "transfers_starting"),
        transfers_running=_integer(data["transfers_running"], "transfers_running"),
        transfers_retained=_integer(data["transfers_retained"], "transfers_retained"),
        forwards_active=_integer(data["forwards_active"], "forwards_active"),
        forwards_retained=_integer(data["forwards_retained"], "forwards_retained"),
        interactions_pending=_integer(data["interactions_pending"], "interactions_pending"),
    )


def daemon_idle_info_to_wire(idle: DaemonIdleInfo) -> Dict[str, Any]:
    if type(idle) is not DaemonIdleInfo:
        raise TypeError("daemon idle info is required")
    return {
        "idle_shutdown_enabled": idle.idle_shutdown_enabled,
        "idle_shutdown_seconds": idle.idle_shutdown_seconds,
        "idle_since": (
            _datetime_to_wire(idle.idle_since, "idle_since")
            if idle.idle_since is not None
            else None
        ),
        "idle_deadline": (
            _datetime_to_wire(idle.idle_deadline, "idle_deadline")
            if idle.idle_deadline is not None
            else None
        ),
        "idle_blockers": list(idle.idle_blockers),
    }


def daemon_idle_info_from_wire(value: Any) -> DaemonIdleInfo:
    data = _strict_fields(
        value,
        required={
            "idle_shutdown_enabled",
            "idle_shutdown_seconds",
            "idle_since",
            "idle_deadline",
            "idle_blockers",
        },
        context="daemon idle info",
    )
    blockers = data["idle_blockers"]
    if type(blockers) is not list:
        raise ValueError("idle_blockers must be an array")
    seconds = data["idle_shutdown_seconds"]
    if seconds is not None and type(seconds) not in (int, float):
        raise ValueError("idle_shutdown_seconds must be a number or null")
    enabled = data["idle_shutdown_enabled"]
    if type(enabled) is not bool:
        raise ValueError("idle_shutdown_enabled must be a boolean")
    return DaemonIdleInfo(
        idle_shutdown_enabled=enabled,
        idle_shutdown_seconds=None if seconds is None else float(seconds),
        idle_since=(
            _datetime_from_wire(data["idle_since"], "idle_since")
            if data["idle_since"] is not None
            else None
        ),
        idle_deadline=(
            _datetime_from_wire(data["idle_deadline"], "idle_deadline")
            if data["idle_deadline"] is not None
            else None
        ),
        idle_blockers=tuple(_identifier(item, "idle blocker") for item in blockers),
    )


def daemon_status_to_wire(status: DaemonStatus) -> Dict[str, Any]:
    if type(status) is not DaemonStatus:
        raise TypeError("daemon status is required")
    return {
        "state": status.state.value,
        "server_instance_id": status.server_instance_id,
        "started_at": _datetime_to_wire(status.started_at, "daemon started_at"),
        "protocol_version": status.protocol_version,
        "api_implementation_version": status.api_implementation_version,
        "daemon_version": status.daemon_version,
        "development_revision": status.development_revision,
        "resources": daemon_resource_counts_to_wire(status.resources),
        "idle": daemon_idle_info_to_wire(status.idle),
        "shutdown_deadline": (
            _datetime_to_wire(status.shutdown_deadline, "shutdown_deadline")
            if status.shutdown_deadline is not None
            else None
        ),
        "disconnect_reason": status.disconnect_reason,
        "restart_requested": status.restart_requested,
    }


def daemon_status_from_wire(value: Any) -> DaemonStatus:
    data = _strict_fields(
        value,
        required={
            "state",
            "server_instance_id",
            "started_at",
            "protocol_version",
            "api_implementation_version",
            "daemon_version",
            "development_revision",
            "resources",
            "idle",
            "shutdown_deadline",
            "disconnect_reason",
            "restart_requested",
        },
        context="daemon status",
    )
    try:
        state = DaemonLifecycleState(data["state"])
    except (TypeError, ValueError):
        raise ValueError("daemon status contains an unknown state") from None
    revision = data["development_revision"]
    if type(revision) is not str:
        raise ValueError("development_revision must be a string")
    reason = data["disconnect_reason"]
    if reason is not None and (type(reason) is not str or not reason):
        raise ValueError("disconnect_reason must be a non-empty string or null")
    restart = data["restart_requested"]
    if type(restart) is not bool:
        raise ValueError("restart_requested must be a boolean")
    return DaemonStatus(
        state=state,
        server_instance_id=_identifier(data["server_instance_id"], "server instance id"),
        started_at=_datetime_from_wire(data["started_at"], "daemon started_at"),
        protocol_version=_identifier(data["protocol_version"], "protocol version"),
        api_implementation_version=_identifier(
            data["api_implementation_version"],
            "api implementation version",
        ),
        daemon_version=_identifier(data["daemon_version"], "daemon version"),
        development_revision=revision,
        resources=daemon_resource_counts_from_wire(data["resources"]),
        idle=daemon_idle_info_from_wire(data["idle"]),
        shutdown_deadline=(
            _datetime_from_wire(data["shutdown_deadline"], "shutdown_deadline")
            if data["shutdown_deadline"] is not None
            else None
        ),
        disconnect_reason=reason,
        restart_requested=restart,
    )


def daemon_diagnostics_to_wire(diagnostics: DaemonDiagnostics) -> Dict[str, Any]:
    if type(diagnostics) is not DaemonDiagnostics:
        raise TypeError("daemon diagnostics are required")
    return {
        "status": daemon_status_to_wire(diagnostics.status),
        "uptime_seconds": float(diagnostics.uptime_seconds),
        "executor_queue_depth": diagnostics.executor_queue_depth,
        "thread_counts_by_role": dict(diagnostics.thread_counts_by_role),
        "open_descriptor_count": diagnostics.open_descriptor_count,
        "rss_bytes": diagnostics.rss_bytes,
        "socket_bound": diagnostics.socket_bound,
        "keep_alive_lease": diagnostics.keep_alive_lease,
    }


def daemon_diagnostics_from_wire(value: Any) -> DaemonDiagnostics:
    data = _strict_fields(
        value,
        required={
            "status",
            "uptime_seconds",
            "executor_queue_depth",
            "thread_counts_by_role",
            "open_descriptor_count",
            "rss_bytes",
            "socket_bound",
            "keep_alive_lease",
        },
        context="daemon diagnostics",
    )
    roles = data["thread_counts_by_role"]
    if type(roles) is not dict:
        raise ValueError("thread_counts_by_role must be an object")
    parsed_roles = {
        _identifier(key, "thread role"): _integer(count, "thread count")
        for key, count in roles.items()
    }
    uptime = data["uptime_seconds"]
    if type(uptime) not in (int, float) or uptime < 0:
        raise ValueError("uptime_seconds must be a non-negative number")
    for name in ("open_descriptor_count", "rss_bytes"):
        raw = data[name]
        if raw is not None and (type(raw) is not int or raw < 0):
            raise ValueError(f"{name} must be a non-negative int or null")
    for flag_name in ("socket_bound", "keep_alive_lease"):
        if type(data[flag_name]) is not bool:
            raise ValueError(f"{flag_name} must be a boolean")
    return DaemonDiagnostics(
        status=daemon_status_from_wire(data["status"]),
        uptime_seconds=float(uptime),
        executor_queue_depth=_integer(data["executor_queue_depth"], "executor_queue_depth"),
        thread_counts_by_role=parsed_roles,
        open_descriptor_count=data["open_descriptor_count"],
        rss_bytes=data["rss_bytes"],
        socket_bound=data["socket_bound"],
        keep_alive_lease=data["keep_alive_lease"],
    )


def stop_daemon_request_to_wire(request: StopDaemonRequest) -> Dict[str, Any]:
    if type(request) is not StopDaemonRequest:
        raise TypeError("stop daemon request is required")
    return {
        "force": request.force,
        "confirmation": request.confirmation,
    }


def stop_daemon_request_from_wire(value: Any) -> StopDaemonRequest:
    data = _strict_fields(
        value,
        required=set(),
        optional={"force", "confirmation"},
        context="stop daemon request",
    )
    force = data.get("force", False)
    if type(force) is not bool:
        raise ValueError("force must be a boolean")
    token = data.get("confirmation")
    if token is not None and (type(token) is not str or not token.strip()):
        raise ValueError("confirmation must be a non-empty string or null")
    return StopDaemonRequest(force=force, confirmation=token)


def restart_daemon_request_to_wire(request: RestartDaemonRequest) -> Dict[str, Any]:
    if type(request) is not RestartDaemonRequest:
        raise TypeError("restart daemon request is required")
    return {
        "force": request.force,
        "confirmation": request.confirmation,
    }


def restart_daemon_request_from_wire(value: Any) -> RestartDaemonRequest:
    data = _strict_fields(
        value,
        required=set(),
        optional={"force", "confirmation"},
        context="restart daemon request",
    )
    force = data.get("force", False)
    if type(force) is not bool:
        raise ValueError("force must be a boolean")
    token = data.get("confirmation")
    if token is not None and (type(token) is not str or not token.strip()):
        raise ValueError("confirmation must be a non-empty string or null")
    return RestartDaemonRequest(force=force, confirmation=token)


def set_daemon_log_level_request_to_wire(
    request: SetDaemonLogLevelRequest,
) -> Dict[str, Any]:
    if type(request) is not SetDaemonLogLevelRequest:
        raise TypeError("set daemon log level request is required")
    return {"level": request.level.value}


def set_daemon_log_level_request_from_wire(value: Any) -> SetDaemonLogLevelRequest:
    data = _strict_fields(
        value,
        required={"level"},
        optional=set(),
        context="set daemon log level request",
    )
    try:
        level = DaemonLogLevel(data["level"])
    except (TypeError, ValueError):
        raise ValueError("daemon log level must be warning, info, or debug") from None
    return SetDaemonLogLevelRequest(level=level)


def set_operation_mode_request_to_wire(
    request: SetOperationModeRequest,
) -> Dict[str, Any]:
    if type(request) is not SetOperationModeRequest:
        raise TypeError("set operation mode request is required")
    return {
        "mode": request.mode.value,
        "seed_isolated_config": request.seed_isolated_config,
    }


def set_operation_mode_request_from_wire(value: Any) -> SetOperationModeRequest:
    data = _strict_fields(
        value,
        required={"mode"},
        optional={"seed_isolated_config"},
        context="set operation mode request",
    )
    try:
        mode = OperationMode(data["mode"])
    except (TypeError, ValueError):
        raise ValueError("operation mode must be default or isolated") from None
    seed = data.get("seed_isolated_config", False)
    if type(seed) is not bool:
        raise ValueError("seed_isolated_config must be a boolean")
    return SetOperationModeRequest(mode=mode, seed_isolated_config=seed)


def operation_mode_files_to_wire(files: OperationModeFiles) -> Dict[str, Any]:
    if type(files) is not OperationModeFiles:
        raise TypeError("operation mode files are required")
    return {
        "root_config_path": files.root_config_path,
        "root_config_exists": files.root_config_exists,
        "known_hosts_path": files.known_hosts_path,
        "known_hosts_exists": files.known_hosts_exists,
        "imported_fragment_path": files.imported_fragment_path,
        "imported_fragment_exists": files.imported_fragment_exists,
    }


def operation_mode_files_from_wire(value: Any) -> OperationModeFiles:
    data = _strict_fields(
        value,
        required={
            "root_config_path",
            "root_config_exists",
            "known_hosts_path",
            "known_hosts_exists",
            "imported_fragment_path",
            "imported_fragment_exists",
        },
        context="operation mode files",
    )
    return OperationModeFiles(
        root_config_path=_identifier(data["root_config_path"], "root config path"),
        root_config_exists=_boolean(data["root_config_exists"], "root_config_exists"),
        known_hosts_path=_identifier(data["known_hosts_path"], "known hosts path"),
        known_hosts_exists=_boolean(data["known_hosts_exists"], "known_hosts_exists"),
        imported_fragment_path=_identifier(
            data["imported_fragment_path"], "imported fragment path"
        ),
        imported_fragment_exists=_boolean(
            data["imported_fragment_exists"], "imported_fragment_exists"
        ),
    )


def operation_mode_result_to_wire(result: OperationModeResult) -> Dict[str, Any]:
    if type(result) is not OperationModeResult:
        raise TypeError("operation mode result is required")
    return {
        "accepted": result.accepted,
        "active_mode": result.active_mode.value,
        "generation": result.generation,
        "seeded": result.seeded,
        "conflict": result.conflict,
        "message": result.message,
        "target_description": result.target_description,
        "persisted_mode": result.persisted_mode.value if result.persisted_mode else None,
        "rollback_completed": result.rollback_completed,
        "recovery_required": result.recovery_required,
        "default_files": (
            operation_mode_files_to_wire(result.default_files)
            if result.default_files is not None
            else None
        ),
        "isolated_files": (
            operation_mode_files_to_wire(result.isolated_files)
            if result.isolated_files is not None
            else None
        ),
        "app_config_path": result.app_config_path,
        "app_config_exists": result.app_config_exists,
    }


def operation_mode_result_from_wire(value: Any) -> OperationModeResult:
    data = _strict_fields(
        value,
        required={"accepted", "active_mode", "generation", "seeded", "conflict", "message", "target_description"},
        optional={
            "persisted_mode",
            "rollback_completed",
            "recovery_required",
            "default_files",
            "isolated_files",
            "app_config_path",
            "app_config_exists",
        },
        context="operation mode result",
    )
    try:
        mode = OperationMode(data["active_mode"])
    except (TypeError, ValueError):
        raise ValueError("operation mode result contains an unknown mode") from None
    persisted_raw = data.get("persisted_mode")
    if persisted_raw is None:
        persisted_mode = None
    else:
        try:
            persisted_mode = OperationMode(persisted_raw)
        except (TypeError, ValueError):
            raise ValueError("operation mode result contains an unknown persisted mode") from None
    recovery_required = data.get("recovery_required", False)
    recovery_required = _boolean(recovery_required, "recovery_required")
    # These fields were added to the 0.40 operation-mode result.  A daemon
    # that reports recovery without the additive rollback flag must still
    # decode to a truthful model; the invariant requires rollback_completed to
    # be false whenever recovery is required.
    rollback_completed = data.get("rollback_completed", not recovery_required)
    # default_files/isolated_files/app_config_path/app_config_exists were added
    # to the 0.41 operation-mode result. A pre-0.41 daemon omits them entirely;
    # decode that as "unknown" rather than failing the whole response.
    default_files_raw = data.get("default_files")
    default_files = (
        operation_mode_files_from_wire(default_files_raw)
        if default_files_raw is not None
        else None
    )
    isolated_files_raw = data.get("isolated_files")
    isolated_files = (
        operation_mode_files_from_wire(isolated_files_raw)
        if isolated_files_raw is not None
        else None
    )
    return OperationModeResult(
        accepted=_boolean(data["accepted"], "accepted"),
        active_mode=mode,
        generation=_integer(data["generation"], "generation"),
        seeded=_boolean(data["seeded"], "seeded"),
        conflict=_boolean(data["conflict"], "conflict"),
        # Successful status responses intentionally use an empty explanation;
        # rejection/recovery responses carry a user-facing message.
        message=_text(data["message"], "message", allow_empty=True),
        target_description=_text(data["target_description"], "target description"),
        persisted_mode=persisted_mode,
        rollback_completed=_boolean(rollback_completed, "rollback_completed"),
        recovery_required=recovery_required,
        default_files=default_files,
        isolated_files=isolated_files,
        app_config_path=_text(data.get("app_config_path", ""), "app_config_path", allow_empty=True),
        app_config_exists=_boolean(data.get("app_config_exists", False), "app_config_exists"),
    )


def daemon_stop_result_to_wire(result: DaemonStopResult) -> Dict[str, Any]:
    if type(result) is not DaemonStopResult:
        raise TypeError("daemon stop result is required")
    return {
        "accepted": result.accepted,
        "state": result.state.value,
        "resources": daemon_resource_counts_to_wire(result.resources),
        "will_lose": list(result.will_lose),
        "confirmation": result.confirmation,
        "message": result.message,
        "restart_requested": result.restart_requested,
    }


def daemon_stop_result_from_wire(value: Any) -> DaemonStopResult:
    data = _strict_fields(
        value,
        required={
            "accepted",
            "state",
            "resources",
            "will_lose",
            "confirmation",
            "message",
            "restart_requested",
        },
        context="daemon stop result",
    )
    if type(data["accepted"]) is not bool:
        raise ValueError("accepted must be a boolean")
    try:
        state = DaemonLifecycleState(data["state"])
    except (TypeError, ValueError):
        raise ValueError("daemon stop result contains an unknown state") from None
    will_lose = data["will_lose"]
    if type(will_lose) is not list:
        raise ValueError("will_lose must be an array")
    token = data["confirmation"]
    if token is not None and (type(token) is not str or not token.strip()):
        raise ValueError("confirmation must be a non-empty string or null")
    message = data["message"]
    if type(message) is not str:
        raise ValueError("message must be a string")
    restart = data["restart_requested"]
    if type(restart) is not bool:
        raise ValueError("restart_requested must be a boolean")
    return DaemonStopResult(
        accepted=data["accepted"],
        state=state,
        resources=daemon_resource_counts_from_wire(data["resources"]),
        will_lose=tuple(_identifier(item, "will_lose entry") for item in will_lose),
        confirmation=token,
        message=message,
        restart_requested=restart,
    )


# ---------------------------------------------------------------------------
# SSH overrides codec
# ---------------------------------------------------------------------------


def global_ssh_overrides_to_wire(
    overrides: Any,
) -> Dict[str, Any]:
    from ..models.settings import GlobalSshOverrides

    if type(overrides) is not GlobalSshOverrides:
        raise TypeError("GlobalSshOverrides is required")
    return {
        "revision": overrides.revision,
        "connect_timeout": overrides.connect_timeout,
        "connection_attempts": overrides.connection_attempts,
        "server_alive_interval": overrides.server_alive_interval,
        "server_alive_count_max": overrides.server_alive_count_max,
        "strict_host_key_checking": overrides.strict_host_key_checking,
        "batch_mode": overrides.batch_mode,
        "compression": overrides.compression,
        "verbosity": overrides.verbosity,
        "debug_enabled": overrides.debug_enabled,
        "applies_immediately": overrides.applies_immediately,
    }


def global_ssh_overrides_from_wire(value: Any) -> Any:
    from ..models.settings import GlobalSshOverrides

    data = _strict_fields(
        value,
        required={
            "revision",
            "connect_timeout",
            "connection_attempts",
            "server_alive_interval",
            "server_alive_count_max",
            "strict_host_key_checking",
            "batch_mode",
            "compression",
            "verbosity",
            "debug_enabled",
        },
        optional={"applies_immediately"},
        context="global ssh overrides",
    )
    return GlobalSshOverrides(
        revision=_identifier(data["revision"], "revision"),
        connect_timeout=_integer(data["connect_timeout"], "connect_timeout"),
        connection_attempts=_integer(data["connection_attempts"], "connection_attempts"),
        server_alive_interval=_integer(data["server_alive_interval"], "server_alive_interval"),
        server_alive_count_max=_integer(data["server_alive_count_max"], "server_alive_count_max"),
        strict_host_key_checking=_text(
            data["strict_host_key_checking"],
            "strict_host_key_checking",
            allow_empty=True,
        ),
        batch_mode=_boolean(data["batch_mode"], "batch_mode"),
        compression=_boolean(data["compression"], "compression"),
        verbosity=_integer(data["verbosity"], "verbosity"),
        debug_enabled=_boolean(data["debug_enabled"], "debug_enabled"),
        applies_immediately=_boolean(data.get("applies_immediately", True), "applies_immediately"),
    )


def update_global_ssh_overrides_request_to_wire(
    request: Any,
) -> Dict[str, Any]:
    from ..models.settings import UpdateGlobalSshOverridesRequest

    if type(request) is not UpdateGlobalSshOverridesRequest:
        raise TypeError("UpdateGlobalSshOverridesRequest is required")
    result: Dict[str, Any] = {"patch": dict(request.patch)}
    if request.expected_revision is not None:
        result["expected_revision"] = request.expected_revision
    return result


def update_global_ssh_overrides_request_from_wire(
    value: Any,
) -> Any:
    from ..models.settings import UpdateGlobalSshOverridesRequest

    data = _strict_fields(
        value,
        required={"patch"},
        optional={"expected_revision"},
        context="update global ssh overrides request",
    )
    patch = data["patch"]
    if type(patch) is not dict:
        raise ValueError("patch must be an object")
    return UpdateGlobalSshOverridesRequest(
        patch=patch,
        expected_revision=data.get("expected_revision"),
    )


# ---------------------------------------------------------------------------
# Daemon-owned secret-backend management
# ---------------------------------------------------------------------------


def secret_configuration_to_wire(configuration: Any) -> Dict[str, Any]:
    from ..models.secrets import SecretConfiguration

    if type(configuration) is not SecretConfiguration:
        raise TypeError("SecretConfiguration is required")
    return {
        "revision": configuration.revision,
        "backend": configuration.backend,
        "session_timeout": configuration.session_timeout,
        "remember_in_keyring": configuration.remember_in_keyring,
        "bitwarden_profile": configuration.bitwarden_profile,
        "bitwarden_server": configuration.bitwarden_server,
        "keepassxc_database": configuration.keepassxc_database,
        "keepassxc_keyfile": configuration.keepassxc_keyfile,
    }


def secret_configuration_from_wire(value: Any) -> Any:
    from ..models.secrets import SecretConfiguration

    data = _strict_fields(
        value,
        required={
            "revision",
            "backend",
            "session_timeout",
            "remember_in_keyring",
            "bitwarden_profile",
            "bitwarden_server",
            "keepassxc_database",
            "keepassxc_keyfile",
        },
        context="secret configuration",
    )
    return SecretConfiguration(
        revision=_text(data["revision"], "revision"),
        backend=_text(data["backend"], "backend", allow_empty=True),
        session_timeout=_integer(data["session_timeout"], "session_timeout"),
        remember_in_keyring=_boolean(data["remember_in_keyring"], "remember_in_keyring"),
        bitwarden_profile=_text(data["bitwarden_profile"], "bitwarden_profile", allow_empty=True),
        bitwarden_server=_text(data["bitwarden_server"], "bitwarden_server", allow_empty=True),
        keepassxc_database=_text(
            data["keepassxc_database"], "keepassxc_database", allow_empty=True
        ),
        keepassxc_keyfile=_text(data["keepassxc_keyfile"], "keepassxc_keyfile", allow_empty=True),
    )


def update_secret_configuration_request_to_wire(request: Any) -> Dict[str, Any]:
    from ..models.secrets import UpdateSecretConfigurationRequest

    if type(request) is not UpdateSecretConfigurationRequest:
        raise TypeError("UpdateSecretConfigurationRequest is required")
    result: Dict[str, Any] = {"patch": dict(request.patch)}
    if request.expected_revision is not None:
        result["expected_revision"] = request.expected_revision
    return result


def update_secret_configuration_request_from_wire(value: Any) -> Any:
    from ..models.secrets import UpdateSecretConfigurationRequest

    data = _strict_fields(
        value,
        required={"patch"},
        optional={"expected_revision"},
        context="update secret configuration request",
    )
    patch = data["patch"]
    if type(patch) is not dict:
        raise ValueError("patch must be an object")
    return UpdateSecretConfigurationRequest(
        patch=patch,
        expected_revision=data.get("expected_revision"),
    )


def secret_backend_descriptor_to_wire(descriptor: Any) -> Dict[str, Any]:
    from ..models.secrets import SecretBackendDescriptor

    if type(descriptor) is not SecretBackendDescriptor:
        raise TypeError("SecretBackendDescriptor is required")
    return {
        "name": descriptor.name,
        "label": descriptor.label,
        "available": descriptor.available,
        "selected": descriptor.selected,
        "session_backed": descriptor.session_backed,
        "locked": descriptor.locked,
        "needs_unlock": descriptor.needs_unlock,
        "login_required": descriptor.login_required,
        "persists_secrets": descriptor.persists_secrets,
        "capabilities": list(descriptor.capabilities),
        "diagnostic": descriptor.diagnostic,
    }


def secret_backend_descriptor_from_wire(value: Any) -> Any:
    from ..models.secrets import SecretBackendDescriptor

    data = _strict_fields(
        value,
        required={
            "name",
            "label",
            "available",
            "selected",
            "session_backed",
            "locked",
            "needs_unlock",
            "login_required",
            "persists_secrets",
            "capabilities",
        },
        optional={"diagnostic"},
        context="secret backend descriptor",
    )
    capabilities = data["capabilities"]
    if type(capabilities) is not list or not all(type(item) is str for item in capabilities):
        raise ValueError("capabilities must be a list of strings")
    return SecretBackendDescriptor(
        name=_text(data["name"], "name"),
        label=_text(data["label"], "label", allow_empty=True),
        available=_boolean(data["available"], "available"),
        selected=_boolean(data["selected"], "selected"),
        session_backed=_boolean(data["session_backed"], "session_backed"),
        locked=_boolean(data["locked"], "locked"),
        needs_unlock=_boolean(data["needs_unlock"], "needs_unlock"),
        login_required=_boolean(data["login_required"], "login_required"),
        persists_secrets=_boolean(data["persists_secrets"], "persists_secrets"),
        capabilities=tuple(capabilities),
        diagnostic=data.get("diagnostic", ""),
    )


def secret_backend_registry_to_wire(registry: Any) -> Dict[str, Any]:
    from ..models.secrets import SecretBackendRegistry

    if type(registry) is not SecretBackendRegistry:
        raise TypeError("SecretBackendRegistry is required")
    return {
        "backends": [secret_backend_descriptor_to_wire(b) for b in registry.backends],
        "effective_backend": registry.effective_backend,
        "selected_backend": registry.selected_backend,
    }


def secret_backend_registry_from_wire(value: Any) -> Any:
    from ..models.secrets import SecretBackendRegistry

    data = _strict_fields(
        value,
        required={"backends", "effective_backend", "selected_backend"},
        context="secret backend registry",
    )
    backends = data["backends"]
    if type(backends) is not list:
        raise ValueError("backends must be a list")
    return SecretBackendRegistry(
        backends=tuple(secret_backend_descriptor_from_wire(item) for item in backends),
        effective_backend=_text(data["effective_backend"], "effective_backend"),
        selected_backend=_text(data["selected_backend"], "selected_backend"),
    )


def secret_backend_state_to_wire(state: Any) -> Dict[str, Any]:
    from ..models.secrets import SecretBackendState

    if type(state) is not SecretBackendState:
        raise TypeError("SecretBackendState is required")
    return state.to_dict()


def secret_backend_state_from_wire(value: Any) -> Any:
    from ..models.secrets import SecretBackendState

    data = _strict_fields(
        value,
        required={
            "selected_backend",
            "effective_backend",
            "locked",
            "needs_unlock",
            "login_required",
            "session_timeout",
            "remember_in_keyring",
            "persists_secrets",
        },
        context="secret backend state",
    )
    return SecretBackendState(
        selected_backend=_text(data["selected_backend"], "selected_backend"),
        effective_backend=_text(data["effective_backend"], "effective_backend"),
        locked=_boolean(data["locked"], "locked"),
        needs_unlock=_boolean(data["needs_unlock"], "needs_unlock"),
        login_required=_boolean(data["login_required"], "login_required"),
        session_timeout=_integer(data["session_timeout"], "session_timeout"),
        remember_in_keyring=_boolean(data["remember_in_keyring"], "remember_in_keyring"),
        persists_secrets=_boolean(data["persists_secrets"], "persists_secrets"),
    )


def secret_unlock_result_to_wire(result: Any) -> Dict[str, Any]:
    from ..models.secrets import SecretUnlockResult

    if type(result) is not SecretUnlockResult:
        raise TypeError("SecretUnlockResult is required")
    return result.to_dict()


def secret_unlock_result_from_wire(value: Any) -> Any:
    from ..models.secrets import SecretUnlockResult, UnlockResultKind

    data = _strict_fields(
        value,
        required={"kind", "backend"},
        optional={"message"},
        context="secret unlock result",
    )
    kind = data["kind"]
    if type(kind) is not str or kind not in UnlockResultKind._value2member_map_:
        raise ValueError("kind is not a valid unlock result kind")
    return SecretUnlockResult(
        kind=UnlockResultKind(kind),
        backend=_text(data["backend"], "backend"),
        message=data.get("message", ""),
    )


def secret_operation_result_to_wire(result: Any) -> Dict[str, Any]:
    from ..models.secrets import SecretOperationResult

    if type(result) is not SecretOperationResult:
        raise TypeError("SecretOperationResult is required")
    return result.to_dict()


def secret_operation_result_from_wire(value: Any) -> Any:
    from ..models.secrets import SecretOperationResult, SecretOperationState

    data = _strict_fields(
        value,
        required={"state", "backend"},
        optional={"message"},
        context="secret operation result",
    )
    state = data["state"]
    if type(state) is not str or state not in SecretOperationState._value2member_map_:
        raise ValueError("state is not a valid secret operation state")
    return SecretOperationResult(
        state=SecretOperationState(state),
        backend=_text(data["backend"], "backend"),
        message=data.get("message", ""),
    )


def bitwarden_status_to_wire(status: Any) -> Dict[str, Any]:
    from ..models.secrets import BitwardenStatus

    if type(status) is not BitwardenStatus:
        raise TypeError("BitwardenStatus is required")
    return status.to_dict()


def bitwarden_status_from_wire(value: Any) -> Any:
    from ..models.secrets import BitwardenStatus

    data = _strict_fields(
        value,
        required={"logged_in", "unlocked", "needs_login", "email", "server_url", "profile"},
        optional={"twofa_required", "message"},
        context="bitwarden status",
    )
    return BitwardenStatus(
        logged_in=_boolean(data["logged_in"], "logged_in"),
        unlocked=_boolean(data["unlocked"], "unlocked"),
        needs_login=_boolean(data["needs_login"], "needs_login"),
        email=_text(data["email"], "email", allow_empty=True),
        server_url=_text(data["server_url"], "server_url", allow_empty=True),
        profile=_text(data["profile"], "profile", allow_empty=True),
        twofa_required=_boolean(data.get("twofa_required", False), "twofa_required"),
        message=data.get("message", ""),
    )


def rbw_status_to_wire(status: Any) -> Dict[str, Any]:
    from ..models.secrets import RbwStatus

    if type(status) is not RbwStatus:
        raise TypeError("RbwStatus is required")
    return status.to_dict()


def rbw_status_from_wire(value: Any) -> Any:
    from ..models.secrets import RbwStatus

    data = _strict_fields(
        value,
        required={"installed", "configured", "unlocked", "email", "base_url"},
        optional={"message"},
        context="rbw status",
    )
    return RbwStatus(
        installed=_boolean(data["installed"], "installed"),
        configured=_boolean(data["configured"], "configured"),
        unlocked=_boolean(data["unlocked"], "unlocked"),
        email=_text(data["email"], "email", allow_empty=True),
        base_url=_text(data["base_url"], "base_url", allow_empty=True),
        message=data.get("message", ""),
    )


def secret_transfer_result_to_wire(result: Any) -> Dict[str, Any]:
    from ..models.secrets import SecretTransferResult

    if type(result) is not SecretTransferResult:
        raise TypeError("SecretTransferResult is required")
    return result.to_dict()


def secret_transfer_result_from_wire(value: Any) -> Any:
    from ..models.secrets import SecretOperationState, SecretTransferResult

    data = _strict_fields(
        value,
        required={"operation", "path", "counts", "warnings", "status"},
        optional={"message"},
        context="secret transfer result",
    )
    counts = data["counts"]
    if type(counts) is not dict or not all(
        type(k) is str and type(v) is int for k, v in counts.items()
    ):
        raise ValueError("counts must be an object of integer counts")
    warnings = data["warnings"]
    if type(warnings) is not list or not all(type(w) is str for w in warnings):
        raise ValueError("warnings must be a list of strings")
    status = data["status"]
    if type(status) is not str or status not in SecretOperationState._value2member_map_:
        raise ValueError("status is not a valid secret transfer state")
    return SecretTransferResult(
        operation=data["operation"],
        path=_text(data["path"], "path", allow_empty=True),
        counts=counts,
        warnings=tuple(warnings),
        status=SecretOperationState(status),
        message=data.get("message", ""),
    )


# ---------------------------------------------------------------------------
# Identity provider management, agent keys, deployment, authorized keys
# ---------------------------------------------------------------------------


def identity_provider_descriptor_to_wire(descriptor: Any) -> Dict[str, Any]:
    from ..models.identity import IdentityProviderDescriptor

    if type(descriptor) is not IdentityProviderDescriptor:
        raise TypeError("IdentityProviderDescriptor is required")
    return {
        "provider_id": descriptor.provider_id,
        "label": descriptor.label,
        "available": descriptor.available,
        "selected": descriptor.selected,
        "effective_agent_socket": descriptor.effective_agent_socket,
        "custom_socket_required": descriptor.custom_socket_required,
        "capabilities": list(descriptor.capabilities),
        "detail": descriptor.detail,
    }


def identity_provider_descriptor_from_wire(value: Any) -> Any:
    from ..models.identity import IdentityProviderDescriptor

    data = _strict_fields(
        value,
        required={
            "provider_id",
            "label",
            "available",
            "selected",
            "effective_agent_socket",
            "custom_socket_required",
            "capabilities",
            "detail",
        },
        context="identity provider descriptor",
    )
    capabilities = data["capabilities"]
    if type(capabilities) is not list or not all(type(item) is str for item in capabilities):
        raise ValueError("capabilities must be a list of strings")
    return IdentityProviderDescriptor(
        provider_id=_identifier(data["provider_id"], "provider id"),
        label=_text(data["label"], "provider label"),
        available=_boolean(data["available"], "provider available"),
        selected=_boolean(data["selected"], "provider selected"),
        effective_agent_socket=_text(
            data["effective_agent_socket"], "effective agent socket", allow_empty=True
        ),
        custom_socket_required=_boolean(data["custom_socket_required"], "custom socket required"),
        capabilities=tuple(capabilities),
        detail=_text(data["detail"], "provider detail", allow_empty=True),
    )


def identity_provider_registry_to_wire(registry: Any) -> Dict[str, Any]:
    from ..models.identity import IdentityProviderRegistry

    if type(registry) is not IdentityProviderRegistry:
        raise TypeError("IdentityProviderRegistry is required")
    return {
        "providers": [
            identity_provider_descriptor_to_wire(provider) for provider in registry.providers
        ],
        "revision": registry.revision,
    }


def identity_provider_registry_from_wire(value: Any) -> Any:
    from ..models.identity import IdentityProviderRegistry

    data = _strict_fields(
        value,
        required={"providers", "revision"},
        context="identity provider registry",
    )
    providers = data["providers"]
    if type(providers) is not list:
        raise ValueError("providers must be a list")
    return IdentityProviderRegistry(
        providers=tuple(identity_provider_descriptor_from_wire(provider) for provider in providers),
        revision=_identifier(data["revision"], "identity revision"),
    )


def identity_state_to_wire(state: Any) -> Dict[str, Any]:
    from ..models.identity import IdentityState

    if type(state) is not IdentityState:
        raise TypeError("IdentityState is required")
    return {
        "provider": state.provider,
        "custom_socket": state.custom_socket,
        "effective_agent_socket": state.effective_agent_socket,
        "agent_available": state.agent_available,
        "ssh_copy_id_available": state.ssh_copy_id_available,
        "revision": state.revision,
    }


def identity_state_from_wire(value: Any) -> Any:
    from ..models.identity import IdentityState

    data = _strict_fields(
        value,
        required={
            "provider",
            "custom_socket",
            "effective_agent_socket",
            "agent_available",
            "ssh_copy_id_available",
            "revision",
        },
        context="identity state",
    )
    return IdentityState(
        provider=_identifier(data["provider"], "identity provider"),
        custom_socket=_text(data["custom_socket"], "custom socket", allow_empty=True),
        effective_agent_socket=_text(
            data["effective_agent_socket"], "effective agent socket", allow_empty=True
        ),
        agent_available=_boolean(data["agent_available"], "agent available"),
        ssh_copy_id_available=_boolean(data["ssh_copy_id_available"], "ssh-copy-id available"),
        revision=_identifier(data["revision"], "identity revision"),
    )


def update_identity_selection_request_to_wire(request: Any) -> Dict[str, Any]:
    from ..models.identity import UpdateIdentitySelectionRequest

    if type(request) is not UpdateIdentitySelectionRequest:
        raise TypeError("UpdateIdentitySelectionRequest is required")
    result: Dict[str, Any] = {"provider": request.provider}
    if request.expected_revision is not None:
        result["expected_revision"] = request.expected_revision
    return result


def update_identity_selection_request_from_wire(value: Any) -> Any:
    from ..models.identity import UpdateIdentitySelectionRequest

    data = _strict_fields(
        value,
        required={"provider"},
        optional={"expected_revision"},
        context="update identity selection request",
    )
    return UpdateIdentitySelectionRequest(
        provider=_identifier(data["provider"], "identity provider"),
        expected_revision=data.get("expected_revision"),
    )


def update_identity_configuration_request_to_wire(request: Any) -> Dict[str, Any]:
    from ..models.identity import UpdateIdentityConfigurationRequest

    if type(request) is not UpdateIdentityConfigurationRequest:
        raise TypeError("UpdateIdentityConfigurationRequest is required")
    result: Dict[str, Any] = {"custom_socket": request.custom_socket}
    if request.expected_revision is not None:
        result["expected_revision"] = request.expected_revision
    return result


def update_identity_configuration_request_from_wire(value: Any) -> Any:
    from ..models.identity import UpdateIdentityConfigurationRequest

    data = _strict_fields(
        value,
        required={"custom_socket"},
        optional={"expected_revision"},
        context="update identity configuration request",
    )
    custom_socket = data["custom_socket"]
    if type(custom_socket) is not str:
        raise ValueError("custom_socket must be a string")
    return UpdateIdentityConfigurationRequest(
        custom_socket=custom_socket,
        expected_revision=data.get("expected_revision"),
    )


def _agent_key_to_wire(key: Any) -> Dict[str, Any]:
    from ..models.identity import AgentKey

    if type(key) is not AgentKey:
        raise TypeError("AgentKey is required")
    return {
        "fingerprint": key.fingerprint,
        "comment": key.comment,
        "key_type": key.key_type,
    }


def _agent_key_from_wire(value: Any) -> Any:
    from ..models.identity import AgentKey

    data = _strict_fields(
        value,
        required={"fingerprint", "comment", "key_type"},
        context="agent key",
    )
    return AgentKey(
        fingerprint=_identifier(data["fingerprint"], "agent key fingerprint"),
        comment=_text(data["comment"], "agent key comment", allow_empty=True),
        key_type=_text(data["key_type"], "agent key type", allow_empty=True),
    )


def agent_key_list_to_wire(key_list: Any) -> Dict[str, Any]:
    from ..models.identity import AgentKeyList

    if type(key_list) is not AgentKeyList:
        raise TypeError("AgentKeyList is required")
    return {"keys": [_agent_key_to_wire(key) for key in key_list.keys]}


def agent_key_list_from_wire(value: Any) -> Any:
    from ..models.identity import AgentKeyList

    data = _strict_fields(value, required={"keys"}, context="agent key list")
    keys = data["keys"]
    if type(keys) is not list:
        raise ValueError("keys must be a list")
    return AgentKeyList(keys=tuple(_agent_key_from_wire(key) for key in keys))


def list_provider_agent_keys_request_to_wire(request: Any) -> Dict[str, Any]:
    from ..models.identity import ListProviderAgentKeysRequest

    if type(request) is not ListProviderAgentKeysRequest:
        raise TypeError("ListProviderAgentKeysRequest is required")
    return {"provider_id": request.provider_id}


def list_provider_agent_keys_request_from_wire(value: Any) -> Any:
    from ..models.identity import ListProviderAgentKeysRequest

    data = _strict_fields(
        value,
        required={"provider_id"},
        context="list provider agent keys request",
    )
    return ListProviderAgentKeysRequest(
        provider_id=_identifier(data["provider_id"], "provider id")
    )


def agent_key_mutation_request_to_wire(request: Any) -> Dict[str, Any]:
    from ..models.identity import AgentKeyMutationRequest

    if type(request) is not AgentKeyMutationRequest:
        raise TypeError("AgentKeyMutationRequest is required")
    return {"key_id": request.key_id, "scope": request.scope.value}


def agent_key_mutation_request_from_wire(value: Any) -> Any:
    from ..models.identity import AgentKeyMutationRequest

    data = _strict_fields(
        value,
        required={"key_id", "scope"},
        context="agent key mutation request",
    )
    return AgentKeyMutationRequest(
        key_id=_identifier(data["key_id"], "key id"),
        scope=_key_store_scope(data["scope"], "key store scope"),
    )


def deploy_key_request_to_wire(request: Any) -> Dict[str, Any]:
    from ..models.identity import DeployKeyRequest

    if type(request) is not DeployKeyRequest:
        raise TypeError("DeployKeyRequest is required")
    return {
        "connection_id": request.connection_id,
        "key_id": request.key_id,
        "scope": request.scope.value,
        "force": request.force,
    }


def deploy_key_request_from_wire(value: Any) -> Any:
    from ..models.identity import DeployKeyRequest

    data = _strict_fields(
        value,
        required={"connection_id", "key_id", "scope", "force"},
        context="deploy key request",
    )
    return DeployKeyRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        key_id=_identifier(data["key_id"], "key id"),
        scope=_key_store_scope(data["scope"], "key store scope"),
        force=_boolean(data["force"], "force"),
    )


def operation_summary_to_wire(summary: Any) -> Dict[str, Any]:
    from ..models.operations import OperationSummary

    if type(summary) is not OperationSummary:
        raise TypeError("OperationSummary is required")
    return {
        "operation_id": summary.operation_id,
        "kind": summary.kind.value,
        "state": summary.state.value,
        "message": summary.message,
        "connection_id": summary.connection_id,
        "created_at": _datetime_to_wire(summary.created_at, "operation creation time"),
        "started_at": _optional_datetime_to_wire(summary.started_at, "operation start time"),
        "finished_at": _optional_datetime_to_wire(summary.finished_at, "operation finish time"),
        "progress": summary.progress,
        "owner_client_id": summary.owner_client_id,
        "failure": _service_failure_to_wire(summary.failure),
        "result": _json_value(summary.result, "operation result"),
    }


def operation_summary_from_wire(value: Any) -> Any:
    from ..models.operations import OperationKind, OperationState, OperationSummary

    data = _strict_fields(
        value,
        required={
            "operation_id",
            "kind",
            "state",
            "message",
            "connection_id",
            "created_at",
            "started_at",
            "finished_at",
            "progress",
            "owner_client_id",
            "failure",
        },
        optional={"result"},
        context="operation summary",
    )
    try:
        kind = OperationKind(data["kind"])
    except (TypeError, ValueError):
        raise ValueError("operation summary contains an unknown kind") from None
    try:
        state = OperationState(data["state"])
    except (TypeError, ValueError):
        raise ValueError("operation summary contains an unknown state") from None
    connection_id = data["connection_id"]
    if connection_id is not None:
        connection_id = ConnectionId(_identifier(connection_id, "operation connection id"))
    progress = data["progress"]
    if progress is not None:
        if type(progress) is int:
            progress = float(progress)
        if type(progress) is not float:
            raise ValueError("operation progress must be a number or null")
    result = data.get("result")
    if result is not None and type(result) is not dict:
        raise ValueError("operation result must be a JSON object or null")
    return OperationSummary(
        operation_id=_identifier(data["operation_id"], "operation id"),
        kind=kind,
        state=state,
        message=_text(data["message"], "operation message", allow_empty=True),
        connection_id=connection_id,
        created_at=_datetime_from_wire(data["created_at"], "operation creation time"),
        started_at=_optional_datetime_from_wire(data["started_at"], "operation start time"),
        finished_at=_optional_datetime_from_wire(data["finished_at"], "operation finish time"),
        progress=progress,
        owner_client_id=_optional_client_id(data["owner_client_id"], "operation owner client id"),
        failure=_service_failure_from_wire(data["failure"]),
        result=result,
    )


def operation_id_request_to_wire(operation_id: Any) -> Dict[str, Any]:
    from ..models.operations import OperationId

    return {"operation_id": OperationId(_identifier(operation_id, "operation id"))}


def broadcast_command_request_to_wire(request: Any) -> Dict[str, Any]:
    from ..models.broadcast import BroadcastCommandRequest
    from ..models.interactions import ExecutionInteractionMode

    if type(request) is not BroadcastCommandRequest:
        raise TypeError("BroadcastCommandRequest is required")
    policy = request.policy
    policy_wire = {
        "failure_policy": policy.failure_policy.value,
        "concurrency_limit": policy.concurrency_limit,
        "timeout_seconds": policy.timeout_seconds,
        "capture_stdout": policy.capture_stdout,
        "capture_stderr": policy.capture_stderr,
        "output_limit_bytes": policy.output_limit_bytes,
    }
    if policy.interaction_mode is ExecutionInteractionMode.AUTOFILL_ONLY:
        policy_wire["interaction_mode"] = ExecutionInteractionMode.AUTOFILL_ONLY.value
    return {
        "connection_ids": list(request.connection_ids),
        "command": request.command,
        "policy": policy_wire,
    }


def broadcast_command_request_from_wire(value: Any) -> Any:
    from ..models.broadcast import (
        BroadcastCommandRequest,
        BroadcastExecutionPolicy,
        BroadcastFailurePolicy,
    )
    from ..models.interactions import ExecutionInteractionMode

    data = _strict_fields(
        value,
        required={"connection_ids", "command", "policy"},
        optional={"input_requested"},
        context="broadcast command request",
    )
    if "input_requested" in data and type(data["input_requested"]) is not bool:
        raise ValueError("broadcast input_requested must be a boolean")
    if type(data["connection_ids"]) is not list:
        raise ValueError("broadcast connection_ids must be an array")
    policy_data = _strict_fields(
        data["policy"],
        required={
            "failure_policy",
            "concurrency_limit",
            "timeout_seconds",
            "capture_stdout",
            "capture_stderr",
            "output_limit_bytes",
        },
        optional={"interaction_mode"},
        context="broadcast execution policy",
    )
    try:
        failure_policy = BroadcastFailurePolicy(policy_data["failure_policy"])
    except (TypeError, ValueError):
        raise ValueError("broadcast policy contains an unknown failure policy") from None
    try:
        interaction_mode = ExecutionInteractionMode(
            policy_data.get(
                "interaction_mode", ExecutionInteractionMode.INTERACTIVE.value
            )
        )
    except (TypeError, ValueError):
        raise ValueError("broadcast policy contains an unknown interaction mode") from None
    timeout = policy_data["timeout_seconds"]
    if timeout is not None and (type(timeout) not in (int, float) or isinstance(timeout, bool)):
        raise ValueError("broadcast timeout must be a number or null")
    return BroadcastCommandRequest(
        tuple(ConnectionId(_identifier(item, "connection id")) for item in data["connection_ids"]),
        _text(data["command"], "broadcast command"),
        BroadcastExecutionPolicy(
            failure_policy=failure_policy,
            concurrency_limit=_integer(
                policy_data["concurrency_limit"], "broadcast concurrency limit"
            ),
            timeout_seconds=timeout,
            capture_stdout=_boolean(policy_data["capture_stdout"], "capture stdout"),
            capture_stderr=_boolean(policy_data["capture_stderr"], "capture stderr"),
            output_limit_bytes=_integer(
                policy_data["output_limit_bytes"], "broadcast output limit"
            ),
            interaction_mode=interaction_mode,
        ),
    )


def host_command_result_to_wire(result: Any) -> Dict[str, Any]:
    from ..models.broadcast import HostCommandResult

    if type(result) is not HostCommandResult:
        raise TypeError("HostCommandResult is required")
    return {
        "connection_id": result.connection_id,
        "state": result.state.value,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "truncated": result.truncated,
        "failure": _service_failure_to_wire(result.failure),
    }


def host_command_result_from_wire(value: Any) -> Any:
    from ..models.broadcast import HostCommandResult, HostCommandState

    data = _strict_fields(
        value,
        required={
            "connection_id",
            "state",
            "exit_code",
            "stdout",
            "stderr",
            "truncated",
            "failure",
        },
        context="host command result",
    )
    try:
        state = HostCommandState(data["state"])
    except (TypeError, ValueError):
        raise ValueError("host command result contains an unknown state") from None
    exit_code = data["exit_code"]
    if exit_code is not None:
        exit_code = _integer(exit_code, "host command exit code")
    return HostCommandResult(
        ConnectionId(_identifier(data["connection_id"], "connection id")),
        state,
        exit_code,
        _text(data["stdout"], "stdout", allow_empty=True),
        _text(data["stderr"], "stderr", allow_empty=True),
        _boolean(data["truncated"], "truncated"),
        _service_failure_from_wire(data["failure"]),
    )


def broadcast_command_summary_to_wire(summary: Any) -> Dict[str, Any]:
    from ..models.broadcast import BroadcastCommandSummary

    if type(summary) is not BroadcastCommandSummary:
        raise TypeError("BroadcastCommandSummary is required")
    return {
        "operation": operation_summary_to_wire(summary.operation),
        "targets": [host_command_result_to_wire(item) for item in summary.targets],
    }


def broadcast_command_summary_from_wire(value: Any) -> Any:
    from ..models.broadcast import BroadcastCommandSummary

    data = _strict_fields(
        value, required={"operation", "targets"}, context="broadcast command summary"
    )
    if type(data["targets"]) is not list:
        raise ValueError("broadcast targets must be an array")
    return BroadcastCommandSummary(
        operation_summary_from_wire(data["operation"]),
        tuple(host_command_result_from_wire(item) for item in data["targets"]),
    )


def operation_id_request_from_wire(value: Any) -> Any:
    from ..models.operations import OperationId

    data = _strict_fields(value, required={"operation_id"}, context="operation id request")
    return OperationId(_identifier(data["operation_id"], "operation id"))


def _authorized_key_entry_to_wire(entry: Any) -> Dict[str, Any]:
    from ..models.identity import AuthorizedKeyEntry

    if type(entry) is not AuthorizedKeyEntry:
        raise TypeError("AuthorizedKeyEntry is required")
    return {
        "line_id": entry.line_id,
        "kind": entry.kind.value,
        "raw_line": entry.raw_line,
        "key_type": entry.key_type,
        "fingerprint": entry.fingerprint,
        "comment": entry.comment,
        "disabled": entry.disabled,
    }


def _authorized_key_entry_from_wire(value: Any) -> Any:
    from ..models.identity import AuthorizedKeyEntry, AuthorizedKeyLineKind

    data = _strict_fields(
        value,
        required={
            "line_id",
            "kind",
            "raw_line",
            "key_type",
            "fingerprint",
            "comment",
            "disabled",
        },
        context="authorized key entry",
    )
    try:
        kind = AuthorizedKeyLineKind(data["kind"])
    except (TypeError, ValueError):
        raise ValueError("authorized key entry contains an unknown kind") from None
    return AuthorizedKeyEntry(
        line_id=_identifier(data["line_id"], "authorized key line id"),
        kind=kind,
        raw_line=_text(data["raw_line"], "authorized key raw line", allow_empty=True),
        key_type=_text(data["key_type"], "authorized key type", allow_empty=True),
        fingerprint=_text(data["fingerprint"], "authorized key fingerprint", allow_empty=True),
        comment=_text(data["comment"], "authorized key comment", allow_empty=True),
        disabled=_boolean(data["disabled"], "authorized key disabled"),
    )


def authorized_key_list_to_wire(key_list: Any) -> Dict[str, Any]:
    from ..models.identity import AuthorizedKeyList

    if type(key_list) is not AuthorizedKeyList:
        raise TypeError("AuthorizedKeyList is required")
    return {
        "connection_id": key_list.connection_id,
        "entries": [_authorized_key_entry_to_wire(entry) for entry in key_list.entries],
        "file_revision": key_list.file_revision,
    }


def authorized_key_list_from_wire(value: Any) -> Any:
    from ..models.identity import AuthorizedKeyList

    data = _strict_fields(
        value,
        required={"connection_id", "entries", "file_revision"},
        context="authorized key list",
    )
    entries = data["entries"]
    if type(entries) is not list:
        raise ValueError("entries must be a list")
    return AuthorizedKeyList(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        entries=tuple(_authorized_key_entry_from_wire(entry) for entry in entries),
        file_revision=_identifier(data["file_revision"], "authorized keys revision"),
    )


def list_authorized_keys_request_to_wire(request: Any) -> Dict[str, Any]:
    from ..models.identity import ListAuthorizedKeysRequest

    if type(request) is not ListAuthorizedKeysRequest:
        raise TypeError("ListAuthorizedKeysRequest is required")
    return {"connection_id": request.connection_id}


def list_authorized_keys_request_from_wire(value: Any) -> Any:
    from ..models.identity import ListAuthorizedKeysRequest

    data = _strict_fields(
        value,
        required={"connection_id"},
        context="list authorized keys request",
    )
    return ListAuthorizedKeysRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id"))
    )


def remove_authorized_key_request_to_wire(request: Any) -> Dict[str, Any]:
    from ..models.identity import RemoveAuthorizedKeyRequest

    if type(request) is not RemoveAuthorizedKeyRequest:
        raise TypeError("RemoveAuthorizedKeyRequest is required")
    return {
        "connection_id": request.connection_id,
        "line_id": request.line_id,
        "file_revision": request.file_revision,
    }


def remove_authorized_key_request_from_wire(value: Any) -> Any:
    from ..models.identity import RemoveAuthorizedKeyRequest

    data = _strict_fields(
        value,
        required={"connection_id", "line_id", "file_revision"},
        context="remove authorized key request",
    )
    return RemoveAuthorizedKeyRequest(
        connection_id=ConnectionId(_identifier(data["connection_id"], "connection id")),
        line_id=_identifier(data["line_id"], "authorized key line id"),
        file_revision=_identifier(data["file_revision"], "authorized keys revision"),
    )
