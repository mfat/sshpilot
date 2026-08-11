#!/usr/bin/env python3
"""Generate deterministic documentation artifacts for the public API surface."""

from __future__ import annotations

import argparse
import dataclasses
import enum
import inspect
import json
import sys
import types
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Tuple,
    TypeVar,
    Union,
    get_args,
    get_type_hints,
)


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
VERSION_BASELINE_DIR = ROOT / "tests/api/snapshots/versions"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sshpilot.api import (  # noqa: E402
    API_IMPLEMENTATION_VERSION,
    PROTOCOL_VERSION,
    Capability,
    ErrorCode,
    EventType,
)
from sshpilot.api import __all__ as API_EXPORTS  # noqa: E402
from sshpilot.api.capabilities import Capabilities  # noqa: E402
from sshpilot.api.client import SshPilotClient  # noqa: E402
from sshpilot.api.daemon_client import (  # noqa: E402
    DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES,
)
from sshpilot.api.events import CoreEvent  # noqa: E402
from sshpilot.core.connection_application_service import (  # noqa: E402
    IMPLEMENTED_CLIENT_METHOD_CAPABILITIES,
)
from sshpilot.api.method_capabilities import (  # noqa: E402
    UNSUPPORTED_CLIENT_METHOD_CAPABILITIES,
)
from sshpilot.api.models import __all__ as MODEL_EXPORTS  # noqa: E402
from sshpilot.api.models import common, connections, interactions, operations  # noqa: E402
from sshpilot.api.models import (  # noqa: E402
    daemon,
    sessions,
    terminal,
    transfers,
    known_hosts,
    keys,
    connection_store,
)
from sshpilot.api.transport import __all__ as TRANSPORT_EXPORTS  # noqa: E402
from sshpilot.api.transport import envelopes  # noqa: E402
from sshpilot.daemon.dispatch import DAEMON_METHOD_CAPABILITIES  # noqa: E402


MODEL_MODULES = (
    common,
    connections,
    sessions,
    terminal,
    interactions,
    transfers,
    operations,
    daemon,
    known_hosts,
    keys,
    connection_store,
    envelopes,
)
EXTRA_MODELS = (Capabilities, CoreEvent)
EXTRA_ENUMS = (Capability, EventType, ErrorCode)
UNION_ORIGINS = (Union,)
if hasattr(types, "UnionType"):
    UNION_ORIGINS += (types.UnionType,)
MISSING_ANNOTATION = object()

IMPLEMENTED_MODELS = {
    "Capabilities",
    "ClientInfo",
    "CompatibilityResult",
    "ConnectionDetails",
    "ConnectionSummary",
    "CoreInfo",
    "DeleteConnectionPasswordRequest",
    "DeleteKeyPassphraseRequest",
    "DeletePluginSecretRequest",
    "GroupReference",
    "GetPluginSecretRequest",
    "LookupKeyPassphraseRequest",
    "ErrorData",
    "ErrorResponseEnvelope",
    "EventEnvelope",
    "HandshakeRequest",
    "HandshakeResult",
    "RequestEnvelope",
    "SuccessResponseEnvelope",
    "AttachSessionRequest",
    "AttachSessionResult",
    "AttachmentInfo",
    "BroadcastTerminalInputRequest",
    "CloseSessionRequest",
    "DetachSessionRequest",
    "OpenSessionRequest",
    "SessionCapabilities",
    "SessionExitInfo",
    "SessionFailure",
    "SessionSummary",
    "ServiceFailure",
    "SftpServiceSummary",
    "OpenSftpRequest",
    "AttachSftpRequest",
    "CloseSftpRequest",
    "ListDirectoryRequest",
    "ListDirectoryResult",
    "RemoteFileEntry",
    "SftpPathRequest",
    "SftpRenameRequest",
    "SftpChmodRequest",
    "SftpSymlinkRequest",
    "TransferSummary",
    "StartTransferRequest",
    "StartScpTransferRequest",
    "TransferBackend",
    "CancelTransferRequest",
    "ForwardSummary",
    "OpenForwardRequest",
    "CloseForwardRequest",
    "DaemonStatus",
    "DaemonDiagnostics",
    "DaemonResourceCounts",
    "DaemonIdleInfo",
    "DaemonStopResult",
    "StopDaemonRequest",
    "RestartDaemonRequest",
    "StoreConnectionPasswordRequest",
    "StoreKeyPassphraseRequest",
    "StorePluginSecretRequest",
    "VerifyKeyPassphraseRequest",
    "VerifyKeyPassphraseResult",
    "KnownHostEntrySummary",
    "KnownHostsSnapshot",
    "RemoveKnownHostEntriesRequest",
    "KnownHostsMutationResult",
}
PARTIAL_MODELS = {"CoreEvent"}

SENSITIVE_FIELDS = {
    "CoreEvent": {"payload"},
    "ErrorData": {"details"},
    "ErrorResponseEnvelope": {"error"},
    "EventEnvelope": {"payload"},
    "InteractionResponse": {"value"},
    "OperationSummary": {"result"},
    "PluginArgument": {"value"},
    "PluginOperationResult": {"values"},
    "ReplayResult": {"data"},
    "RequestEnvelope": {"params"},
    "SftpReadFileResult": {"content"},
    "SftpReplaceFileRequest": {"content"},
    "SaveSshConfigTextRequest": {"text"},
    "StoreConnectionPasswordRequest": {"password"},
    "StorePluginSecretRequest": {"value"},
    "SshConfigText": {"text"},
    "SuccessResponseEnvelope": {"result"},
    "TerminalInput": {"data"},
    "BroadcastTerminalInputRequest": {"command"},
    "TerminalOutput": {"data"},
}

RELATED_METHODS = {
    "Capabilities": ("get_capabilities",),
    "CompatibilityResult": ("get_capabilities",),
    "ConnectionDetails": (
        "get_connection",
        "create_connection",
        "update_connection",
    ),
    "ConnectionSummary": ("list_connections",),
    "CreateConnectionRequest": ("create_connection",),
    "DeleteConnectionRequest": ("delete_connection",),
    "DeleteConnectionResult": ("delete_connection",),
    "UpdateConnectionRequest": ("update_connection",),
    "OpenSessionRequest": ("open_session",),
    "SessionSummary": (
        "list_sessions",
        "get_session",
        "open_session",
        "attach_session",
    ),
    "AttachSessionRequest": ("attach_session",),
    "AttachSessionResult": ("attach_session",),
    "DetachSessionRequest": ("detach_session",),
    "CloseSessionRequest": ("close_session",),
    "TerminalInput": ("send_terminal_input",),
    "BroadcastTerminalInputRequest": ("broadcast_terminal_input",),
    "ResizeTerminalRequest": ("resize_terminal",),
    "ReplayRequest": ("replay_terminal",),
    "ReplayResult": ("replay_terminal",),
    "InteractionResponse": ("respond_to_interaction",),
    "CoreEvent": ("subscribe_events",),
    "SftpServiceSummary": (
        "list_sftp_services",
        "get_sftp_service",
        "open_sftp",
        "attach_sftp",
    ),
    "OpenSftpRequest": ("open_sftp",),
    "AttachSftpRequest": ("attach_sftp",),
    "CloseSftpRequest": ("close_sftp",),
    "ListDirectoryRequest": ("sftp_list_directory",),
    "ListDirectoryResult": ("sftp_list_directory",),
    "RemoteFileEntry": ("sftp_list_directory", "sftp_stat", "sftp_lstat"),
    "SftpPathRequest": (
        "sftp_stat",
        "sftp_lstat",
        "sftp_realpath",
        "sftp_readlink",
        "sftp_mkdir",
        "sftp_rmdir",
        "sftp_remove",
    ),
    "SftpRenameRequest": ("sftp_rename",),
    "SftpChmodRequest": ("sftp_chmod",),
    "SftpSymlinkRequest": ("sftp_symlink",),
    "TransferSummary": ("list_transfers", "get_transfer", "start_transfer", "start_scp_transfer"),
    "StartTransferRequest": ("start_transfer",),
    "StartScpTransferRequest": ("start_scp_transfer",),
    "TransferBackend": ("start_scp_transfer",),
    "CancelTransferRequest": ("cancel_transfer",),
    "ForwardSummary": ("list_forwards", "get_forward", "open_forward"),
    "OpenForwardRequest": ("open_forward",),
    "CloseForwardRequest": ("close_forward",),
    "DaemonStatus": ("get_daemon_status",),
    "DaemonDiagnostics": ("get_daemon_diagnostics",),
    "DaemonStopResult": ("stop_daemon", "restart_daemon"),
    "StopDaemonRequest": ("stop_daemon",),
    "RestartDaemonRequest": ("restart_daemon",),
    "KnownHostEntrySummary": ("list_known_hosts", "remove_known_host_entries"),
    "KnownHostsSnapshot": ("list_known_hosts",),
    "RemoveKnownHostEntriesRequest": ("remove_known_host_entries",),
    "KnownHostsMutationResult": ("remove_known_host_entries",),
    "StoreKeyPassphraseRequest": ("store_key_passphrase",),
    "VerifyKeyPassphraseRequest": ("verify_key_passphrase",),
    "VerifyKeyPassphraseResult": ("verify_key_passphrase",),
}

RELATED_EVENTS = {
    "ConnectionSummary": (
        "connection.created",
        "connection.updated",
        "connection.deleted",
    ),
    "CoreEvent": tuple(item.value for item in EventType),
    "InteractionRequest": ("session.interaction_requested",),
    "SessionExitInfo": ("session.exited",),
    "SessionSummary": (
        "session.created",
        "session.state_changed",
        "session.closed",
    ),
    "TerminalOutput": ("session.output",),
    "SftpServiceSummary": (
        "sftp.created",
        "sftp.state_changed",
        "sftp.closed",
        "sftp.failed",
    ),
    "TransferSummary": (
        "transfer.created",
        "transfer.started",
        "transfer.progress",
        "transfer.item_completed",
        "transfer.completed",
        "transfer.cancelled",
        "transfer.failed",
    ),
    "ForwardSummary": (
        "forward.created",
        "forward.starting",
        "forward.active",
        "forward.closed",
        "forward.failed",
    ),
}

FIELD_EXAMPLES = {
    "after_sequence": 0,
    "attachment_count": 0,
    "attachment_id": "attachment-2",
    "bind_host": "127.0.0.1",
    "bind_port": 8022,
    "client_id": "client-abc123",
    "columns": 80,
    "connection_id": "production",
    "created_at": "2030-01-01T00:00:00Z",
    "earliest_sequence": 0,
    "expires_at": None,
    "first_sequence": 0,
    "hostname": "example.invalid",
    "id": "production",
    "interaction_id": "interaction-4",
    "latest_sequence": 0,
    "max_bytes": 1048576,
    "message": "Example request",
    "name": "example",
    "next_sequence": 1,
    "nickname": "example",
    "operation": "status",
    "path": "/remote/example",
    "plugin_id": "example.plugin",
    "port": 22,
    "protocol": "ssh",
    "reason": "example",
    "request_id": "request-3",
    "retained_bytes": 0,
    "rows": 24,
    "sequence": 0,
    "session_id": "session-12",
    "target_host": "example.invalid",
    "target_port": 22,
    "timestamp": "2030-01-01T00:00:00Z",
    "transfer_id": "transfer-3",
    "username": "user",
    "version": "1.0",
}


def public_models() -> List[type]:
    """Return all frontend-neutral dataclasses defined by the API modules."""

    found = list(EXTRA_MODELS)
    for module in MODEL_MODULES:
        for name, value in vars(module).items():
            if (
                not name.startswith("_")
                and inspect.isclass(value)
                and value.__module__ == module.__name__
                and dataclasses.is_dataclass(value)
            ):
                found.append(value)
    return sorted(set(found), key=lambda item: item.__name__)


def public_enums() -> List[type]:
    """Return all stable string enums defined by the API modules."""

    found = list(EXTRA_ENUMS)
    for module in MODEL_MODULES:
        for name, value in vars(module).items():
            if (
                not name.startswith("_")
                and inspect.isclass(value)
                and value.__module__ == module.__name__
                and issubclass(value, enum.Enum)
            ):
                found.append(value)
    return sorted(set(found), key=lambda item: item.__name__)


def identifier_types() -> List[str]:
    """Return the public opaque identifier aliases."""

    return sorted(
        name
        for name, value in vars(common).items()
        if not name.startswith("_")
        and getattr(value, "__module__", None) == common.__name__
        and hasattr(value, "__supertype__")
    )


def client_methods() -> List[str]:
    """Return the public methods declared by ``SshPilotClient``."""

    return sorted(
        name
        for name, value in vars(SshPilotClient).items()
        if not name.startswith("_") and inspect.isfunction(value)
    )


def _public_model_map() -> Dict[str, type]:
    return {model.__name__: model for model in public_models()}


def _public_type_map() -> Dict[str, type]:
    """Return documented dataclasses plus public enums used in relationships."""

    found = _public_model_map()
    found.update({enum_type.__name__: enum_type for enum_type in public_enums()})
    return found


def _models_in_annotation(annotation: Any, model_map: Mapping[str, type]) -> set[type]:
    found: set[type] = set()
    if inspect.isclass(annotation) and any(
        annotation is model for model in model_map.values()
    ):
        found.add(annotation)
    for nested in get_args(annotation):
        found.update(_models_in_annotation(nested, model_map))
    return found


def _method_annotations(method: Any) -> Mapping[str, Any]:
    """Resolve a client method's type hints without hiding model drift."""

    globalns = dict(getattr(method, "__globals__", {}))
    globalns["CoreEvent"] = CoreEvent
    try:
        return get_type_hints(method, globalns=globalns)
    except (NameError, TypeError):
        # A callback annotation may refer to a private event implementation
        # that is intentionally not a public model.  Its ordinary request and
        # result annotations remain available in the raw mapping.
        return getattr(method, "__annotations__", {})


def implemented_client_methods() -> frozenset[str]:
    """Return methods implemented by either the core or daemon client path."""

    return frozenset(
        set(IMPLEMENTED_CLIENT_METHOD_CAPABILITIES)
        | set(DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES)
    )


@lru_cache(maxsize=1)
def derived_related_methods() -> Dict[str, tuple[str, ...]]:
    """Derive direct request/result model relationships from client methods."""

    models = _public_type_map()
    related: Dict[str, set[str]] = {}
    for method_name in implemented_client_methods():
        method = getattr(SshPilotClient, method_name, None)
        if method is None:
            continue
        for annotation in _method_annotations(method).values():
            for model in _models_in_annotation(annotation, models):
                related.setdefault(model.__name__, set()).add(method_name)
    return {
        name: tuple(sorted(methods)) for name, methods in related.items()
    }


def related_methods_for(model_name: str) -> tuple[str, ...]:
    """Merge reviewed manual relationships with annotation-derived ones."""

    return tuple(
        sorted(
            set(RELATED_METHODS.get(model_name, ()))
            | set(derived_related_methods().get(model_name, ()))
        )
    )


def client_method_contract() -> Dict[str, Dict[str, Any]]:
    """Return structured runtime status and capability metadata."""

    result = {}
    for name, capability in IMPLEMENTED_CLIENT_METHOD_CAPABILITIES.items():
        result[name] = {
            "status": "implemented",
            "capability": capability.value if capability is not None else None,
        }
    for name, capability in UNSUPPORTED_CLIENT_METHOD_CAPABILITIES.items():
        daemon_capability = DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES.get(name)
        result[name] = {
            "status": (
                "daemon-only"
                if daemon_capability is not None
                else "schema-only"
            ),
            "capability": (
                daemon_capability.value
                if daemon_capability is not None
                else capability.value
            ),
        }
    return dict(sorted(result.items()))


def daemon_method_contract() -> Dict[str, Dict[str, Any]]:
    """Return the explicit Phase 1 wire-method capability mapping."""

    return {
        name: {
            "capability": capability.value if capability is not None else None,
        }
        for name, capability in sorted(DAEMON_METHOD_CAPABILITIES.items())
    }


def client_signatures() -> Dict[str, Dict[str, Any]]:
    """Return stable parameter and return-type shapes for client methods."""

    result = {}
    for name in client_methods():
        method = getattr(SshPilotClient, name)
        signature = inspect.signature(method)
        # Read annotations without evaluating or rendering them through
        # inspect.Signature. Their source spelling is stable across supported
        # interpreters, including Python 3.9 where inspect.get_annotations is
        # unavailable; _type_name normalizes evaluated annotations as well.
        annotations = getattr(method, "__annotations__", {})
        result[name] = {
            "parameters": [
                {
                    "name": parameter.name,
                    "kind": parameter.kind.name.lower(),
                    "type": (
                        "untyped"
                        if (
                            annotation := annotations.get(
                                parameter.name,
                                MISSING_ANNOTATION,
                            )
                        )
                        is MISSING_ANNOTATION
                        else _type_name(annotation)
                    ),
                }
                for parameter in signature.parameters.values()
                if parameter.name != "self"
            ],
            "return": (
                "untyped"
                if (
                    return_annotation := annotations.get(
                        "return",
                        MISSING_ANNOTATION,
                    )
                )
                is MISSING_ANNOTATION
                else _type_name(return_annotation)
            ),
        }
    return result


def _type_name(annotation: Any) -> str:
    if isinstance(annotation, TypeVar):
        return annotation.__name__
    if hasattr(annotation, "__supertype__"):
        return annotation.__name__
    if annotation is Any:
        return "Any"
    if annotation is None or annotation is type(None):
        return "None"
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if origin in UNION_ORIGINS:
        return " | ".join(_type_name(arg) for arg in args)
    if origin in (tuple, Tuple):
        if len(args) == 2 and args[1] is Ellipsis:
            return f"tuple[{_type_name(args[0])}, ...]"
        return f"tuple[{', '.join(_type_name(arg) for arg in args)}]"
    if origin in (frozenset,):
        return f"frozenset[{_type_name(args[0])}]"
    if origin is not None:
        origin_name = getattr(origin, "__name__", str(origin).replace("typing.", ""))
        return f"{origin_name}[{', '.join(_type_name(arg) for arg in args)}]"
    return getattr(annotation, "__name__", str(annotation).replace("typing.", ""))


def _normalize(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_normalize(item) for item in value]
    if isinstance(value, (frozenset, set)):
        normalized = [_normalize(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if dataclasses.is_dataclass(value):
        return {
            item.name: _normalize(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, bytes):
        return "<binary bytes>"
    return value


def _default(field: dataclasses.Field[Any]) -> Tuple[bool, Any]:
    if field.default is not dataclasses.MISSING:
        value = field.default
        if hasattr(value, "__repr__") and repr(value) == "UNSET":
            return True, "`UNSET`"
        return True, _normalize(value)
    if field.default_factory is not dataclasses.MISSING:
        factory = field.default_factory
        name = getattr(factory, "__name__", "factory")
        if name == "utc_now":
            return True, "<UTC timestamp at creation>"
        try:
            return True, _normalize(factory())
        except Exception:
            return True, f"<{name}()>"
    return False, None


def _example_for_type(annotation: Any, field_name: str, stack: Tuple[type, ...]) -> Any:
    if field_name in FIELD_EXAMPLES:
        return FIELD_EXAMPLES[field_name]
    if hasattr(annotation, "__supertype__"):
        return FIELD_EXAMPLES.get(annotation.__name__.replace("Id", "_id").lower(), "id:example")
    if isinstance(annotation, TypeVar) or annotation is Any:
        return {}
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if origin in UNION_ORIGINS:
        non_none = [arg for arg in args if arg is not type(None)]
        return None if len(non_none) != len(args) else _example_for_type(non_none[0], field_name, stack)
    if origin in (tuple, Tuple, list, List, frozenset, set):
        return []
    if inspect.isclass(annotation) and issubclass(annotation, enum.Enum):
        return next(iter(annotation)).value
    if inspect.isclass(annotation) and dataclasses.is_dataclass(annotation):
        return _example_for_model(annotation, stack)
    if annotation is str:
        return "example"
    if annotation is int:
        return 0
    if annotation is bool:
        return False
    if annotation is bytes:
        return "<binary bytes>"
    if annotation is datetime:
        return "2030-01-01T00:00:00Z"
    return {}


def _example_for_model(model: type, stack: Tuple[type, ...] = ()) -> Dict[str, Any]:
    if model in stack:
        return {}
    example: Dict[str, Any] = {}
    sensitive = SENSITIVE_FIELDS.get(model.__name__, set())
    for field in dataclasses.fields(model):
        if field.name in sensitive:
            example[field.name] = "<sensitive value omitted>"
            continue
        has_default, default = _default(field)
        if has_default:
            example[field.name] = default
        else:
            example[field.name] = _example_for_type(
                field.type,
                field.name,
                stack + (model,),
            )
    return example


def _model_status(name: str) -> str:
    if name in _implemented_model_names():
        return "Implemented"
    if name in PARTIAL_MODELS:
        return "Partially implemented"
    return "Schema only"


def _implemented_model_names() -> frozenset[str]:
    """Derive implemented request/result models and retain explicit extras."""

    derived = set(derived_related_methods())
    return frozenset(set(IMPLEMENTED_MODELS) | derived)


def validate_generator_metadata(
    *,
    sensitive_fields: Mapping[str, set[str]] | None = None,
    related_methods: Mapping[str, tuple[str, ...]] | None = None,
    related_events: Mapping[str, tuple[str, ...]] | None = None,
    implemented_models: set[str] | None = None,
) -> None:
    """Validate generator metadata against the live public API.

    The optional mappings make the validator directly testable with synthetic
    stale metadata. Normal generation always uses the module's reviewed
    metadata tables and annotation-derived implementation information.
    """

    models = _public_type_map()
    sensitive = SENSITIVE_FIELDS if sensitive_fields is None else sensitive_fields
    methods = RELATED_METHODS if related_methods is None else related_methods
    events = RELATED_EVENTS if related_events is None else related_events
    failures: list[str] = []

    for model_name, fields in sensitive.items():
        model = models.get(model_name)
        if model is None:
            failures.append(f"SENSITIVE_FIELDS references unknown model {model_name}")
            continue
        if not dataclasses.is_dataclass(model):
            failures.append(
                f"SENSITIVE_FIELDS target {model_name} is not a dataclass model"
            )
            continue
        model_fields = {field.name: field for field in dataclasses.fields(model)}
        for field_name in fields:
            field = model_fields.get(field_name)
            if field is None:
                failures.append(
                    f"SENSITIVE_FIELDS references unknown field {model_name}.{field_name}"
                )
            elif field.repr is not False:
                failures.append(
                    f"sensitive field {model_name}.{field_name} must use repr=False"
                )

    known_methods = set(client_methods())
    for model_name, method_names in methods.items():
        if model_name not in models:
            failures.append(f"RELATED_METHODS references unknown model {model_name}")
        for method_name in method_names:
            if method_name not in known_methods:
                failures.append(
                    f"RELATED_METHODS references unknown client method {method_name}"
                )

    known_events = {item.value for item in EventType}
    for model_name, event_names in events.items():
        if model_name not in models:
            failures.append(f"RELATED_EVENTS references unknown model {model_name}")
        for event_name in event_names:
            if event_name not in known_events:
                failures.append(
                    f"RELATED_EVENTS references unknown event {event_name}"
                )

    for status_name, status_models in (
        ("IMPLEMENTED_MODELS", IMPLEMENTED_MODELS),
        ("PARTIAL_MODELS", PARTIAL_MODELS),
    ):
        for model_name in status_models:
            if model_name not in models:
                failures.append(
                    f"{status_name} references unknown model {model_name}"
                )
    status_overlap = set(IMPLEMENTED_MODELS) & set(PARTIAL_MODELS)
    if status_overlap:
        failures.append(
            "a model cannot be both implemented and partial: "
            + ", ".join(sorted(status_overlap))
        )

    derived = derived_related_methods()
    expected_implemented = set(derived)
    declared_implemented = (
        _implemented_model_names()
        if implemented_models is None
        else set(implemented_models)
    )
    missing = expected_implemented - declared_implemented
    if missing:
        failures.append(
            "implemented client request/result models are missing from metadata: "
            + ", ".join(sorted(missing))
        )
    partial_conflicts = expected_implemented & set(PARTIAL_MODELS)
    if partial_conflicts:
        failures.append(
            "implemented client models cannot be marked partially implemented: "
            + ", ".join(sorted(partial_conflicts))
        )

    for model_name, method_names in derived.items():
        effective = set(methods.get(model_name, ())) | set(derived.get(model_name, ()))
        if not set(method_names) <= effective:
            failures.append(
                f"implemented model {model_name} is missing a derived method relationship"
            )

    if failures:
        raise ValueError("Invalid API generator metadata:\n" + "\n".join(failures))


def build_surface() -> Dict[str, Any]:
    """Build the stable review snapshot of the public API."""

    return {
        "api_exports": sorted(API_EXPORTS),
        "api_implementation_version": API_IMPLEMENTATION_VERSION,
        "capabilities": sorted(item.value for item in Capability),
        "client_method_contract": client_method_contract(),
        "client_methods": client_methods(),
        "client_signatures": client_signatures(),
        "daemon_method_contract": daemon_method_contract(),
        "error_codes": sorted(item.value for item in ErrorCode),
        "event_types": sorted(item.value for item in EventType),
        "identifier_types": identifier_types(),
        "model_exports": sorted(MODEL_EXPORTS),
        "models": {
            model.__name__: [field.name for field in dataclasses.fields(model)]
            for model in public_models()
        },
        "protocol_version": PROTOCOL_VERSION,
        "transport_exports": sorted(TRANSPORT_EXPORTS),
        "public_enums": {
            enum_type.__name__: [item.value for item in enum_type]
            for enum_type in public_enums()
        },
    }


def build_schema() -> Dict[str, Any]:
    """Build a transport-neutral structural catalog, not an HTTP/OpenAPI schema."""

    return {
        "format": "sshpilot-api-schema-v1",
        "protocol_version": PROTOCOL_VERSION,
        "api_implementation_version": API_IMPLEMENTATION_VERSION,
        "client_method_contract": client_method_contract(),
        "daemon_method_contract": daemon_method_contract(),
        "identifiers": identifier_types(),
        "enums": {
            enum_type.__name__: [item.value for item in enum_type]
            for enum_type in public_enums()
        },
        "models": {
            model.__name__: {
                "status": _model_status(model.__name__),
                "fields": [
                    {
                        "name": field.name,
                        "type": _type_name(field.type),
                        "required": not _default(field)[0],
                        "default": _default(field)[1],
                        "sensitive": field.name
                        in SENSITIVE_FIELDS.get(model.__name__, set()),
                    }
                    for field in dataclasses.fields(model)
                ],
            }
            for model in public_models()
        },
    }


def _markdown_value(value: Any) -> str:
    if value is None:
        return "`null`"
    if isinstance(value, str):
        return f"`{value}`"
    return f"`{json.dumps(value, sort_keys=True)}`"


def _model_purpose(model: type) -> str:
    """Return explicit model documentation, never a dataclass signature."""

    purpose = model.__dict__.get("__doc__")
    if not purpose:
        return f"Frontend-neutral `{model.__name__}` record."
    purpose = inspect.cleandoc(purpose)
    # dataclasses supplies ``ClassName(field: type, ...)`` as __doc__ when the
    # class body has no docstring.  Its type rendering varies by interpreter.
    if purpose.startswith(f"{model.__name__}("):
        return f"Frontend-neutral `{model.__name__}` record."
    return purpose


def build_model_index() -> str:
    """Build the generated per-model structural reference."""

    lines = [
        "# Generated model index",
        "",
        "<!-- generated by scripts/generate_api_artifacts.py; do not edit by hand -->",
        "",
        "This is a deterministic structural reference derived from public dataclasses.",
        "It is transport-neutral and is not an OpenAPI document. Runtime semantics live",
        "in [../models.md](../models.md). Synthetic examples never read live objects or",
        "stored connection data.",
        "",
    ]
    for model in public_models():
        name = model.__name__
        purpose = _model_purpose(model)
        lines.extend(
            [
                f"<!-- api-model: {name} -->",
                f"## `{name}`",
                "",
                f"**Status:** {_model_status(name)}",
                f"**Introduced:** Protocol v1",
                f"**Purpose:** {purpose}",
                "",
            ]
        )
        methods = related_methods_for(name)
        events = RELATED_EVENTS.get(name, ())
        lines.append(
            "**Related methods:** "
            + (", ".join(f"`{item}`" for item in methods) if methods else "None")
        )
        lines.append(
            "**Related events:** "
            + (", ".join(f"`{item}`" for item in events) if events else "None")
        )
        lines.extend(
            [
                "",
                "| Field | Type | Required | Default | Sensitive |",
                "| --- | --- | ---: | --- | ---: |",
            ]
        )
        for field in dataclasses.fields(model):
            has_default, default = _default(field)
            lines.append(
                f"| `{field.name}` | `{_type_name(field.type)}` | "
                f"{'No' if has_default else 'Yes'} | "
                f"{_markdown_value(default) if has_default else '—'} | "
                f"{'Yes' if field.name in SENSITIVE_FIELDS.get(name, set()) else 'No'} |"
            )
        lines.extend(
            [
                "",
                "Synthetic representation:",
                "",
                "```json",
                json.dumps(_example_for_model(model), indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def artifacts() -> Mapping[Path, str]:
    validate_generator_metadata()
    return {
        ROOT / "docs/api/generated/model-index.md": build_model_index(),
        ROOT / "docs/api/generated/schema.json": _json_text(build_schema()),
        ROOT / "tests/api/snapshots/public_api.json": _json_text(build_surface()),
    }


def version_baseline_path(
    version: str = API_IMPLEMENTATION_VERSION,
    *,
    baseline_dir: Path = VERSION_BASELINE_DIR,
) -> Path:
    return baseline_dir / f"{version}.json"


def validate_version_baseline(
    surface: Mapping[str, Any],
    *,
    version: str = API_IMPLEMENTATION_VERSION,
    baseline_dir: Path = VERSION_BASELINE_DIR,
) -> None:
    """Require the current public surface to match its reviewed version file."""

    path = version_baseline_path(version, baseline_dir=baseline_dir)
    if not path.exists():
        raise ValueError(
            f"No accepted public API baseline exists for implementation version {version}. "
            "Review compatibility, bump the implementation version, update the API "
            "changelog, regenerate artifacts, then explicitly accept the new version "
            "baseline with --accept-version-baseline."
        )
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read API version baseline {path}: {error}") from error
    if baseline != dict(surface):
        raise ValueError(
            f"Public API surface changed while API_IMPLEMENTATION_VERSION remains {version}. "
            "Review compatibility, bump the implementation version, update the API "
            "changelog, regenerate artifacts, then explicitly accept the new version "
            "baseline."
        )


def write_version_baseline(
    surface: Mapping[str, Any],
    *,
    version: str = API_IMPLEMENTATION_VERSION,
    baseline_dir: Path = VERSION_BASELINE_DIR,
) -> Path:
    path = version_baseline_path(version, baseline_dir=baseline_dir)
    if path.exists():
        # Acceptance is intentionally create-only.  An existing reviewed
        # baseline may be accepted again, but never replaced under the same
        # implementation version.
        validate_version_baseline(
            surface,
            version=version,
            baseline_dir=baseline_dir,
        )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_text(surface), encoding="utf-8")
    try:
        display_path = path.relative_to(ROOT)
    except ValueError:
        display_path = path
    print(f"Accepted public API baseline {display_path}")
    return path


def check_artifacts(expected: Mapping[Path, str]) -> int:
    stale = []
    for path, content in expected.items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            stale.append(path.relative_to(ROOT))
    if not stale:
        print("API artifacts are current.")
        return 0
    print("API artifacts are stale or missing:", file=sys.stderr)
    for path in stale:
        print(f"  - {path}", file=sys.stderr)
    print(
        "Review compatibility and docs/api/CHANGELOG.md, then run "
        "`python3 scripts/generate_api_artifacts.py`.",
        file=sys.stderr,
    )
    return 1


def write_artifacts(expected: Mapping[Path, str]) -> None:
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"Wrote {path.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Fail if committed API artifacts or the version baseline differ.",
    )
    mode.add_argument(
        "--accept-version-baseline",
        action="store_true",
        help="Explicitly accept the reviewed public surface for the current API version.",
    )
    args = parser.parse_args()
    try:
        expected = artifacts()
        surface = build_surface()
        if args.accept_version_baseline:
            write_version_baseline(surface)
            write_artifacts(expected)
            return 0
        validate_version_baseline(surface)
        if args.check:
            return check_artifacts(expected)
        write_artifacts(expected)
        return 0
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
