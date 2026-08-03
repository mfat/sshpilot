"""Synchronous Protocol v1 client for the local sshPilot daemon."""

from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import time
from sshpilot.runtime_identity import new_unique_client_id, new_request_id
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, NoReturn, Optional, Union

from sshpilot import __version__ as sshpilot_version

from .capabilities import Capabilities, Capability
from .errors import ErrorCode, SshPilotError, unsupported_capability
from .events import (
    CoreEventCallback,
    EventPublisher,
    EventType,
    Subscription,
)
from .method_capabilities import UNSUPPORTED_CLIENT_METHOD_CAPABILITIES
from .models.common import (
    ClientId,
    ConnectionId,
    ForwardId,
    RequestId,
    SessionId,
    SftpServiceId,
    TransferId,
)
from .models.connections import (
    ConnectionDetails,
    ConnectionEditorDetails,
    ConnectionSummary,
    CreateConnectionRequest,
    DeleteConnectionPasswordRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    DeleteKeyPassphraseRequest,
    StoreConnectionPasswordRequest,
    StoreKeyPassphraseRequest,
    UpdateConnectionRequest,
)
from .models.daemon import (
    DaemonDiagnostics,
    DaemonStatus,
    DaemonStopResult,
    RestartDaemonRequest,
    StopDaemonRequest,
)
from .models.interactions import (
    InteractionClaim,
    InteractionDecisionRequest,
    InteractionId,
    InteractionSummary,
)
from .models.operations import (
    AttachSftpRequest,
    ClaimForwardRequest,
    CloseForwardRequest,
    CloseSftpRequest,
    ForwardSummary,
    ListDirectoryRequest,
    ListDirectoryResult,
    OpenForwardRequest,
    OpenSftpRequest,
    RemoteFileEntry,
    SftpChmodRequest,
    SftpPathRequest,
    SftpRenameRequest,
    SftpServiceSummary,
    SftpSymlinkRequest,
)
from .models.sessions import (
    AttachSessionRequest,
    AttachSessionResult,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionSummary,
)
from .models.terminal import (
    ClaimTerminalInputRequest,
    ReleaseTerminalInputRequest,
    ReplayRequest,
    ReplayResult,
    ResizeTerminalRequest,
    TerminalInput,
    TerminalOutput,
)
from .models.transfers import (
    CancelTransferRequest,
    StartTransferRequest,
    TransferSummary,
)
from .terminal_events import (
    TerminalContinuityCallback,
    TerminalEofCallback,
    TerminalErrorCallback,
    TerminalOutputCallback,
    TerminalSubscription,
)
from .transport.codec import (
    assign_connection_to_group_request_to_wire,
    attach_session_request_to_wire,
    attach_session_result_from_wire,
    attach_sftp_request_to_wire,
    cancel_transfer_request_to_wire,
    capabilities_from_wire,
    claim_forward_request_to_wire,
    claim_terminal_input_request_to_wire,
    close_forward_request_to_wire,
    close_session_request_to_wire,
    close_sftp_request_to_wire,
    connection_details_from_wire,
    connection_editor_details_from_wire,
    connection_summary_from_wire,
    create_connection_request_to_wire,
    create_group_request_to_wire,
    daemon_diagnostics_from_wire,
    daemon_status_from_wire,
    daemon_stop_result_from_wire,
    decode_envelope,
    delete_connection_password_request_to_wire,
    delete_key_passphrase_request_to_wire,
    delete_connection_request_to_wire,
    delete_connection_result_from_wire,
    delete_group_request_to_wire,
    detach_session_request_to_wire,
    encode_envelope,
    error_from_wire,
    forward_summary_from_wire,
    handshake_request_to_wire,
    handshake_result_from_wire,
    interaction_claim_from_wire,
    interaction_decision_to_wire,
    interaction_summary_from_wire,
    list_directory_request_to_wire,
    list_directory_result_from_wire,
    open_forward_request_to_wire,
    open_session_request_to_wire,
    open_sftp_request_to_wire,
    public_event_from_envelope,
    release_terminal_input_request_to_wire,
    remote_file_entry_from_wire,
    rename_group_request_to_wire,
    split_connection_request_to_wire,
    replay_request_to_wire,
    replay_result_from_wire,
    resize_terminal_request_to_wire,
    restart_daemon_request_to_wire,
    session_summary_from_wire,
    sftp_chmod_request_to_wire,
    sftp_path_request_to_wire,
    sftp_rename_request_to_wire,
    sftp_service_summary_from_wire,
    sftp_symlink_request_to_wire,
    start_transfer_request_to_wire,
    stop_daemon_request_to_wire,
    store_connection_password_request_to_wire,
    store_key_passphrase_request_to_wire,
    transfer_summary_from_wire,
    update_connection_metadata_request_to_wire,
    update_connection_request_to_wire,
)
from .transport.envelopes import (
    ErrorResponseEnvelope,
    EventEnvelope,
    HandshakeRequest,
    RequestEnvelope,
    SuccessResponseEnvelope,
)
from .transport.framing import (
    FramingError,
    encode_binary_frame,
    encode_frame,
    receive_multiplexed_frame,
)
from .transport.terminal_frames import (
    TerminalFrame,
    TerminalFrameFlags,
    TerminalFrameKind,
    encode_terminal_payload,
)
from .transport.secret_frames import (
    SecretFrame,
    SecretFrameKind,
    encode_secret_payload,
)
from .version import PROTOCOL_VERSION

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT = 5.0
DEFAULT_CLIENT_EVENT_DISPATCH_LIMIT = 256
_EVENT_STOP = object()
receive_frame = receive_multiplexed_frame

DAEMON_IMPLEMENTED_CLIENT_METHOD_CAPABILITIES = {
    "attach_session": Capability.SESSIONS_WRITE,
    "claim_terminal_input": Capability.TERMINAL_INPUT,
    "close_session": Capability.SESSIONS_WRITE,
    "detach_session": Capability.SESSIONS_WRITE,
    "get_daemon_diagnostics": Capability.DAEMON_STATUS,
    "get_daemon_status": Capability.DAEMON_STATUS,
    "get_session": Capability.SESSIONS_READ,
    "get_interaction": Capability.INTERACTIONS_READ,
    "claim_interaction": Capability.INTERACTIONS_RESPOND,
    "cancel_interaction": Capability.INTERACTIONS_RESPOND,
    "list_interactions": Capability.INTERACTIONS_READ,
    "list_sessions": Capability.SESSIONS_READ,
    "open_session": Capability.SESSIONS_WRITE,
    "release_terminal_input": Capability.TERMINAL_INPUT,
    "restart_daemon": Capability.DAEMON_CONTROL,
    "send_terminal_input": Capability.TERMINAL_INPUT,
    "stop_daemon": Capability.DAEMON_CONTROL,
    "subscribe_terminal": Capability.TERMINAL_OUTPUT,
    "resize_terminal": Capability.TERMINAL_RESIZE,
    "release_interaction": Capability.INTERACTIONS_RESPOND,
    "respond_to_interaction": Capability.INTERACTIONS_RESPOND,
    "send_interaction_secret": Capability.INTERACTIONS_RESPOND,
    "replay_terminal": Capability.TERMINAL_REPLAY,
    "list_sftp_services": Capability.SFTP_READ,
    "get_sftp_service": Capability.SFTP_READ,
    "open_sftp": Capability.SFTP_WRITE,
    "claim_forward": Capability.FORWARDS_WRITE,
    "attach_sftp": Capability.SFTP_WRITE,
    "detach_sftp": Capability.SFTP_WRITE,
    "close_sftp": Capability.SFTP_WRITE,
    "sftp_list_directory": Capability.SFTP_READ,
    "sftp_stat": Capability.SFTP_METADATA,
    "sftp_lstat": Capability.SFTP_METADATA,
    "sftp_realpath": Capability.SFTP_METADATA,
    "sftp_readlink": Capability.SFTP_METADATA,
    "sftp_mkdir": Capability.SFTP_MUTATE,
    "sftp_rmdir": Capability.SFTP_MUTATE,
    "sftp_remove": Capability.SFTP_MUTATE,
    "sftp_rename": Capability.SFTP_MUTATE,
    "sftp_chmod": Capability.SFTP_MUTATE,
    "sftp_symlink": Capability.SFTP_MUTATE,
    "list_transfers": Capability.TRANSFERS_READ,
    "get_transfer": Capability.TRANSFERS_READ,
    "start_transfer": Capability.TRANSFERS_WRITE,
    "cancel_transfer": Capability.TRANSFERS_WRITE,
    "list_forwards": Capability.FORWARDS_READ,
    "get_forward": Capability.FORWARDS_READ,
    "open_forward": Capability.FORWARDS_WRITE,
    "close_forward": Capability.FORWARDS_WRITE,
}


@dataclass
class _PendingRequest:
    completed: threading.Event
    response: Optional[Union[SuccessResponseEnvelope, ErrorResponseEnvelope]] = None
    error: Optional[SshPilotError] = None
    method: Optional[str] = None
    started_at: float = field(default_factory=time.monotonic)
    sent: bool = False


@dataclass(frozen=True)
class _TransportFailureNotice:
    error: SshPilotError


@dataclass(frozen=True)
class _TerminalCallbacks:
    on_output: TerminalOutputCallback
    on_continuity_lost: Optional[TerminalContinuityCallback]
    on_eof: Optional[TerminalEofCallback]
    on_error: Optional[TerminalErrorCallback]


class _ConcurrentRequestGate:
    """Compatibility context manager; pending correlation permits concurrency."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


class DaemonClient:
    """Synchronous requests with one persistent socket reader and event handoff."""

    def __init__(
        self,
        *,
        socket_path: Optional[os.PathLike] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        connect_timeout: Optional[float] = None,
        client_name: str = "daemon-client",
        client_version: str = sshpilot_version,
        client_id: Optional[str] = None,
        frontend_type: Optional[str] = None,
        event_dispatch_limit: int = DEFAULT_CLIENT_EVENT_DISPATCH_LIMIT,
    ) -> None:
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("daemon request timeout must be positive")
        if connect_timeout is None:
            connect_timeout = timeout
        elif type(connect_timeout) not in (int, float) or connect_timeout <= 0:
            raise ValueError("daemon connect timeout must be positive")
        if type(event_dispatch_limit) is not int or event_dispatch_limit < 1:
            raise ValueError("event dispatch limit must be positive")
        self._timeout = float(timeout)
        self._connect_timeout = float(connect_timeout)
        self._socket_path = (
            Path(socket_path) if socket_path is not None else self.default_socket_path()
        )
        self._client_id = ClientId(client_id) if client_id else new_unique_client_id()
        self._client_name = client_name
        self._client_version = client_version
        self._frontend_type = frontend_type
        self._request_lock = _ConcurrentRequestGate()
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._publisher = EventPublisher()
        self._pending_requests: Dict[RequestId, _PendingRequest] = {}
        self._event_queue: queue.Queue = queue.Queue(maxsize=event_dispatch_limit)
        self._terminal_queue: queue.Queue = queue.Queue(maxsize=event_dispatch_limit)
        self._terminal_subscribers: Dict[
            SessionId,
            Dict[int, _TerminalCallbacks],
        ] = {}
        self._next_terminal_subscription = 1
        self._terminal_sequences: Dict[SessionId, int] = {}
        self._terminal_overflow: set[SessionId] = set()
        self._last_event_sequence: Optional[int] = None
        self._closed = False
        self._close_complete = False
        self._transport_failed = False
        self._on_transport_lost: Optional[Callable[[SshPilotError], None]] = None
        self._socket: Optional[socket.socket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._event_thread: Optional[threading.Thread] = None
        self._terminal_thread: Optional[threading.Thread] = None
        self._capabilities: Optional[Capabilities] = None
        self._selected_protocol_version: Optional[str] = None
        self._server_instance_id: Optional[str] = None
        self._daemon_version: Optional[str] = None
        self._daemon_started_at: str = ""
        self._development_revision: str = ""
        self._daemon_api_implementation_version: str = ""
        try:
            self._connect_and_handshake()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def default_socket_path() -> Path:
        # Lazy import avoids making socket-path policy part of envelope modules.
        from sshpilot.daemon.lifecycle import resolve_socket_path

        return resolve_socket_path()

    @property
    def server_instance_id(self) -> str:
        if self._server_instance_id is None:
            raise SshPilotError(
                ErrorCode.PROTOCOL_ERROR,
                "The daemon handshake is incomplete",
            )
        return self._server_instance_id

    @property
    def daemon_version(self) -> Optional[str]:
        return self._daemon_version

    @property
    def daemon_started_at(self) -> str:
        return self._daemon_started_at

    @property
    def development_revision(self) -> str:
        return self._development_revision

    def build_mismatch(self) -> Optional[str]:
        """Return a safe reason when the daemon build looks stale, else None.

        Compares application version, API implementation version, and the
        optional opaque ``SSHPILOT_DEV_REVISION`` token. Never includes paths.
        """
        from sshpilot import __version__ as app_version
        from .version import API_IMPLEMENTATION_VERSION

        if self._daemon_version and self._daemon_version != app_version:
            return "daemon_version"
        caps = self._capabilities
        if caps is not None and caps.api_implementation_version != API_IMPLEMENTATION_VERSION:
            return "api_implementation_version"
        if (
            self._daemon_api_implementation_version
            and self._daemon_api_implementation_version != API_IMPLEMENTATION_VERSION
        ):
            return "api_implementation_version"
        local_revision = (os.environ.get("SSHPILOT_DEV_REVISION") or "").strip()
        if (
            local_revision
            and self._development_revision
            and local_revision != self._development_revision
        ):
            return "development_revision"
        return None

    def _log_daemon_identity(self) -> None:
        mismatch = self.build_mismatch()
        logger.info(
            "Daemon handshake identity version=%s api=%s instance=%s "
            "started_at=%s revision=%s mismatch=%s",
            self._daemon_version or "",
            self._daemon_api_implementation_version
            or (
                self._capabilities.api_implementation_version
                if self._capabilities is not None
                else ""
            ),
            self._server_instance_id or "",
            self._daemon_started_at or "",
            self._development_revision or "",
            mismatch or "none",
        )
        if mismatch:
            logger.warning(
                "Daemon build metadata differs from this application "
                "(%s). Restart sshpilotd before testing new daemon code.",
                mismatch,
            )

    def _require_write_compatibility(self, operation: str) -> None:
        """Reject write operations if the daemon is running an incompatible API."""
        from .version import API_IMPLEMENTATION_VERSION
        
        incompatible = False
        caps = self._capabilities
        if caps is not None and caps.api_implementation_version != API_IMPLEMENTATION_VERSION:
            incompatible = True
        elif (
            self._daemon_api_implementation_version
            and self._daemon_api_implementation_version != API_IMPLEMENTATION_VERSION
        ):
            incompatible = True
            
        if incompatible:
            from .errors import DaemonRestartRequiredError
            raise DaemonRestartRequiredError(
                f"Cannot {operation} because the background daemon is running an "
                "older or incompatible version. Please restart the application."
            )

    def threads_alive(self) -> dict:
        """Return reader/event/terminal thread liveness for watchdog assertions."""
        return {
            "reader": bool(
                self._reader_thread is not None and self._reader_thread.is_alive()
            ),
            "event": bool(
                self._event_thread is not None and self._event_thread.is_alive()
            ),
            "terminal": bool(
                self._terminal_thread is not None
                and self._terminal_thread.is_alive()
            ),
        }

    def get_capabilities(self) -> Capabilities:
        capabilities = self._capabilities
        if capabilities is None:
            raise self._closed_error()
        return capabilities

    def list_connections(self) -> List[ConnectionSummary]:
        result = self._request("connections.list", {})
        if type(result) is not list:
            self._fail_protocol("The daemon returned an invalid connection list")
        try:
            return [connection_summary_from_wire(item) for item in result]
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid connection list")

    def get_connection(self, connection_id: ConnectionId) -> ConnectionDetails:
        result = self._request(
            "connections.get",
            {"connection_id": connection_id},
        )
        try:
            return connection_details_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid connection details")

    def get_connection_editor(
        self, connection_id: ConnectionId
    ) -> ConnectionEditorDetails:
        self._require_capability(Capability.CONNECTIONS_CONFIG_READ)
        result = self._request(
            "connections.get_editor",
            {"connection_id": connection_id},
        )
        try:
            return connection_editor_details_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol(
                "The daemon returned invalid connection editor details"
            )

    def create_connection(self, request: CreateConnectionRequest) -> 'ConnectionMutationResult':
        self._require_write_compatibility("create connection")
        self._require_capability(Capability.CONNECTIONS_WRITE)
        if request.config_patch:
            self._require_capability(Capability.CONNECTIONS_CONFIG_WRITE)
        result = self._request(
            "connections.create",
            create_connection_request_to_wire(request),
            mutation_connection_id=None,
        )
        try:
            from .transport.codec import connection_mutation_result_from_wire
            return connection_mutation_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid connection details")

    def update_connection(
        self,
        connection_id: ConnectionId,
        request: UpdateConnectionRequest,
    ) -> 'ConnectionMutationResult':
        self._require_write_compatibility("update connection")
        self._require_capability(Capability.CONNECTIONS_WRITE)
        if request.config_patch:
            self._require_capability(Capability.CONNECTIONS_CONFIG_WRITE)
        result = self._request(
            "connections.update",
            {
                "connection_id": connection_id,
                "update": update_connection_request_to_wire(request),
            },
            mutation_connection_id=connection_id,
        )
        try:
            from .transport.codec import connection_mutation_result_from_wire
            return connection_mutation_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid connection details")

    def delete_connection(self, request: DeleteConnectionRequest) -> DeleteConnectionResult:
        self._require_write_compatibility("delete connection")
        self._require_capability(Capability.CONNECTIONS_WRITE)
        result = self._request(
            "connections.delete",
            delete_connection_request_to_wire(request),
            mutation_connection_id=request.connection_id,
        )
        try:
            return delete_connection_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid delete result")

    def store_connection_password(self, request: StoreConnectionPasswordRequest) -> bool:
        self._require_write_compatibility("store password")
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        result = self._request(
            "connections.store_password",
            store_connection_password_request_to_wire(request),
            mutation_connection_id=request.connection_id,
        )
        if type(result) is not bool:
            self._fail_protocol("The daemon returned an invalid password store result")
        return result

    def delete_connection_password(self, request: DeleteConnectionPasswordRequest) -> bool:
        self._require_write_compatibility("delete password")
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        result = self._request(
            "connections.delete_password",
            delete_connection_password_request_to_wire(request),
            mutation_connection_id=request.connection_id,
        )
        if type(result) is not bool:
            self._fail_protocol("The daemon returned an invalid password delete result")
        return result

    def store_key_passphrase(self, request: StoreKeyPassphraseRequest) -> bool:
        self._require_write_compatibility("store passphrase")
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        result = self._request(
            "connections.store_passphrase",
            store_key_passphrase_request_to_wire(request),
        )
        if type(result) is not bool:
            self._fail_protocol("The daemon returned an invalid passphrase store result")
        return result

    def delete_key_passphrase(self, request: DeleteKeyPassphraseRequest) -> bool:
        self._require_write_compatibility("delete passphrase")
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        result = self._request(
            "connections.delete_passphrase",
            delete_key_passphrase_request_to_wire(request),
        )
        if type(result) is not bool:
            self._fail_protocol(
                "The daemon returned an invalid passphrase delete result"
            )
        return result

    def lookup_key_passphrase(self, key_path: str) -> Optional[str]:
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        result = self._request(
            "connections.lookup_passphrase",
            {"key_path": key_path},
        )
        if result is not None and type(result) is not str:
            self._fail_protocol("The daemon returned an invalid passphrase lookup result")
        return result

    def update_connection_metadata(
        self, connection_id: ConnectionId, meta: Dict[str, Any]
    ) -> bool:
        self._require_capability(Capability.CONNECTIONS_METADATA_WRITE)
        self._require_write_compatibility("update connection metadata")
        from .models.connections import UpdateConnectionMetadataRequest
        request = UpdateConnectionMetadataRequest(connection_id=connection_id, meta=meta)
        result = self._request(
            "connections.update_metadata",
            update_connection_metadata_request_to_wire(request),
            mutation_connection_id=connection_id,
        )
        if type(result) is not bool:
            self._fail_protocol("The daemon returned an invalid metadata update result")
        return result

    def assign_connection_to_group(
        self, connection_id: ConnectionId, group_id: str
    ) -> bool:
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        self._require_write_compatibility("assign connection to group")
        from .models.connections import AssignConnectionToGroupRequest
        request = AssignConnectionToGroupRequest(
            connection_id=connection_id, group_id=group_id
        )
        result = self._request(
            "connections.assign_to_group",
            assign_connection_to_group_request_to_wire(request),
            mutation_connection_id=connection_id,
        )
        if type(result) is not bool:
            self._fail_protocol("The daemon returned an invalid group assignment result")
        return result

    def create_group(
        self, name: str, parent_id: str = "", color: str = ""
    ) -> Optional[str]:
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        self._require_write_compatibility("create group")
        from .models.connections import CreateGroupRequest
        request = CreateGroupRequest(name=name, parent_id=parent_id, color=color)
        result = self._request(
            "connections.create_group",
            create_group_request_to_wire(request),
        )
        if result is not None and type(result) is not str:
            self._fail_protocol("The daemon returned an invalid group ID")
        return result

    def delete_group(self, group_id: str) -> bool:
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        self._require_write_compatibility("delete group")
        from .models.connections import DeleteGroupRequest
        request = DeleteGroupRequest(group_id=group_id)
        result = self._request(
            "connections.delete_group",
            delete_group_request_to_wire(request),
        )
        if type(result) is not bool:
            self._fail_protocol("The daemon returned an invalid group delete result")
        return result

    def rename_group(self, group_id: str, new_name: str) -> bool:
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        self._require_write_compatibility("rename group")
        from .models.connections import RenameGroupRequest
        request = RenameGroupRequest(group_id=group_id, new_name=new_name)
        result = self._request(
            "connections.rename_group",
            rename_group_request_to_wire(request),
        )
        if type(result) is not bool:
            self._fail_protocol("The daemon returned an invalid group rename result")
        return result

    def split_connection(self, request) -> 'ConnectionMutationResult':
        self._require_write_compatibility("split connection")
        self._require_capability(Capability.CONNECTIONS_SPLIT)
        result = self._request(
            "connections.split",
            split_connection_request_to_wire(request),
        )
        try:
            from .transport.codec import connection_mutation_result_from_wire
            return connection_mutation_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid split result")

    def open_session(self, request: OpenSessionRequest) -> SessionSummary:
        self._require_capability(Capability.SESSIONS_WRITE)
        result = self._request(
            "sessions.open",
            open_session_request_to_wire(request),
            session_mutation=True,
        )
        try:
            return session_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid session summary")

    def list_sessions(self) -> List[SessionSummary]:
        self._require_capability(Capability.SESSIONS_READ)
        result = self._request("sessions.list", {})
        if type(result) is not list:
            self._fail_protocol("The daemon returned an invalid session list")
        try:
            return [session_summary_from_wire(item) for item in result]
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid session list")

    def get_daemon_status(self) -> DaemonStatus:
        self._require_capability(Capability.DAEMON_STATUS)
        result = self._request("daemon.status", {})
        try:
            return daemon_status_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid status snapshot")

    def get_daemon_diagnostics(self) -> DaemonDiagnostics:
        self._require_capability(Capability.DAEMON_STATUS)
        result = self._request("daemon.diagnostics", {})
        try:
            return daemon_diagnostics_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid diagnostics")

    def stop_daemon(
        self,
        request: Optional[StopDaemonRequest] = None,
    ) -> DaemonStopResult:
        self._require_capability(Capability.DAEMON_CONTROL)
        payload = stop_daemon_request_to_wire(request or StopDaemonRequest())
        result = self._request("daemon.stop", payload)
        try:
            return daemon_stop_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid stop result")

    def restart_daemon(
        self,
        request: Optional[RestartDaemonRequest] = None,
    ) -> DaemonStopResult:
        self._require_capability(Capability.DAEMON_CONTROL)
        payload = restart_daemon_request_to_wire(request or RestartDaemonRequest())
        result = self._request("daemon.restart", payload)
        try:
            return daemon_stop_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid restart result")

    def get_session(self, session_id: SessionId) -> SessionSummary:
        self._require_capability(Capability.SESSIONS_READ)
        result = self._request("sessions.get", {"session_id": session_id})
        try:
            return session_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid session summary")

    def attach_session(self, request: AttachSessionRequest) -> AttachSessionResult:
        self._require_capability(Capability.SESSIONS_WRITE)
        result = self._request(
            "sessions.attach",
            attach_session_request_to_wire(request),
        )
        try:
            return attach_session_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid attachment")

    def detach_session(self, request: DetachSessionRequest) -> None:
        self._require_capability(Capability.SESSIONS_WRITE)
        result = self._request(
            "sessions.detach",
            detach_session_request_to_wire(request),
        )
        if result is not None:
            self._fail_protocol("The daemon returned an invalid detach result")

    def close_session(self, request: CloseSessionRequest) -> None:
        self._require_capability(Capability.SESSIONS_WRITE)
        result = self._request(
            "sessions.close",
            close_session_request_to_wire(request),
            mutation_session_id=request.session_id,
            session_mutation=True,
        )
        if result is not None:
            self._fail_protocol("The daemon returned an invalid close result")

    # -- SFTP services --------------------------------------------------
    def list_sftp_services(self) -> List[SftpServiceSummary]:
        self._require_capability(Capability.SFTP_READ)
        result = self._request("sftp.list_services", {})
        if type(result) is not list:
            self._fail_protocol("The daemon returned an invalid SFTP service list")
        try:
            return [sftp_service_summary_from_wire(item) for item in result]
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid SFTP service list")

    def get_sftp_service(self, service_id: SftpServiceId) -> SftpServiceSummary:
        self._require_capability(Capability.SFTP_READ)
        result = self._request("sftp.get_service", {"service_id": service_id})
        try:
            return sftp_service_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid SFTP service summary")

    def open_sftp(self, request: OpenSftpRequest) -> SftpServiceSummary:
        self._require_capability(Capability.SFTP_WRITE)
        result = self._request(
            "sftp.open",
            open_sftp_request_to_wire(request),
            session_mutation=True,
        )
        try:
            return sftp_service_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid SFTP service summary")

    def attach_sftp(self, request: AttachSftpRequest) -> SftpServiceSummary:
        self._require_capability(Capability.SFTP_WRITE)
        result = self._request("sftp.attach", attach_sftp_request_to_wire(request))
        try:
            return sftp_service_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid SFTP service summary")

    def detach_sftp(self, service_id: SftpServiceId) -> None:
        self._require_capability(Capability.SFTP_WRITE)
        result = self._request("sftp.detach", {"service_id": service_id})
        if result is not None:
            self._fail_protocol("The daemon returned an invalid SFTP detach result")

    def close_sftp(self, request: CloseSftpRequest) -> None:
        self._require_capability(Capability.SFTP_WRITE)
        result = self._request(
            "sftp.close",
            close_sftp_request_to_wire(request),
            mutation_session_id=SessionId(str(request.service_id)),
            session_mutation=True,
        )
        if result is not None:
            self._fail_protocol("The daemon returned an invalid SFTP close result")

    def sftp_list_directory(self, request: ListDirectoryRequest) -> ListDirectoryResult:
        self._require_capability(Capability.SFTP_READ)
        result = self._request("sftp.list", list_directory_request_to_wire(request))
        try:
            return list_directory_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid directory listing")

    def sftp_stat(self, request: SftpPathRequest) -> RemoteFileEntry:
        self._require_capability(Capability.SFTP_METADATA)
        result = self._request("sftp.stat", sftp_path_request_to_wire(request))
        try:
            return remote_file_entry_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid file entry")

    def sftp_lstat(self, request: SftpPathRequest) -> RemoteFileEntry:
        self._require_capability(Capability.SFTP_METADATA)
        result = self._request("sftp.lstat", sftp_path_request_to_wire(request))
        try:
            return remote_file_entry_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid file entry")

    def sftp_realpath(self, request: SftpPathRequest) -> str:
        self._require_capability(Capability.SFTP_METADATA)
        result = self._request("sftp.realpath", sftp_path_request_to_wire(request))
        if type(result) is not dict or type(result.get("path")) is not str:
            self._fail_protocol("The daemon returned an invalid realpath result")
        return result["path"]

    def sftp_readlink(self, request: SftpPathRequest) -> str:
        self._require_capability(Capability.SFTP_METADATA)
        result = self._request("sftp.readlink", sftp_path_request_to_wire(request))
        if type(result) is not dict or type(result.get("path")) is not str:
            self._fail_protocol("The daemon returned an invalid readlink result")
        return result["path"]

    def sftp_mkdir(self, request: SftpPathRequest) -> None:
        self._require_capability(Capability.SFTP_MUTATE)
        result = self._request("sftp.mkdir", sftp_path_request_to_wire(request))
        if result is not None:
            self._fail_protocol("The daemon returned an invalid mkdir result")

    def sftp_rmdir(self, request: SftpPathRequest) -> None:
        self._require_capability(Capability.SFTP_MUTATE)
        result = self._request("sftp.rmdir", sftp_path_request_to_wire(request))
        if result is not None:
            self._fail_protocol("The daemon returned an invalid rmdir result")

    def sftp_remove(self, request: SftpPathRequest) -> None:
        self._require_capability(Capability.SFTP_MUTATE)
        result = self._request("sftp.remove", sftp_path_request_to_wire(request))
        if result is not None:
            self._fail_protocol("The daemon returned an invalid remove result")

    def sftp_rename(self, request: SftpRenameRequest) -> None:
        self._require_capability(Capability.SFTP_MUTATE)
        result = self._request("sftp.rename", sftp_rename_request_to_wire(request))
        if result is not None:
            self._fail_protocol("The daemon returned an invalid rename result")

    def sftp_chmod(self, request: SftpChmodRequest) -> None:
        self._require_capability(Capability.SFTP_MUTATE)
        result = self._request("sftp.chmod", sftp_chmod_request_to_wire(request))
        if result is not None:
            self._fail_protocol("The daemon returned an invalid chmod result")

    def sftp_symlink(self, request: SftpSymlinkRequest) -> None:
        self._require_capability(Capability.SFTP_MUTATE)
        result = self._request("sftp.symlink", sftp_symlink_request_to_wire(request))
        if result is not None:
            self._fail_protocol("The daemon returned an invalid symlink result")

    # -- transfers --------------------------------------------------------
    def list_transfers(self) -> List[TransferSummary]:
        self._require_capability(Capability.TRANSFERS_READ)
        result = self._request("transfers.list", {})
        if type(result) is not list:
            self._fail_protocol("The daemon returned an invalid transfer list")
        try:
            return [transfer_summary_from_wire(item) for item in result]
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid transfer list")

    def get_transfer(self, transfer_id: TransferId) -> TransferSummary:
        self._require_capability(Capability.TRANSFERS_READ)
        result = self._request("transfers.get", {"transfer_id": transfer_id})
        try:
            return transfer_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid transfer summary")

    def start_transfer(self, request: StartTransferRequest) -> TransferSummary:
        self._require_capability(Capability.TRANSFERS_WRITE)
        result = self._request(
            "transfers.start",
            start_transfer_request_to_wire(request),
            session_mutation=True,
        )
        try:
            return transfer_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid transfer summary")

    def cancel_transfer(self, request: CancelTransferRequest) -> None:
        self._require_capability(Capability.TRANSFERS_WRITE)
        result = self._request(
            "transfers.cancel",
            cancel_transfer_request_to_wire(request),
            session_mutation=True,
        )
        if result is not None:
            self._fail_protocol("The daemon returned an invalid cancel result")

    # -- forwards -----------------------------------------------------
    def list_forwards(self) -> List[ForwardSummary]:
        self._require_capability(Capability.FORWARDS_READ)
        result = self._request("forwards.list", {})
        if type(result) is not list:
            self._fail_protocol("The daemon returned an invalid forward list")
        try:
            return [forward_summary_from_wire(item) for item in result]
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid forward list")

    def get_forward(self, forward_id: ForwardId) -> ForwardSummary:
        self._require_capability(Capability.FORWARDS_READ)
        result = self._request("forwards.get", {"forward_id": forward_id})
        try:
            return forward_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid forward summary")

    def open_forward(self, request: OpenForwardRequest) -> ForwardSummary:
        self._require_capability(Capability.FORWARDS_WRITE)
        result = self._request(
            "forwards.open",
            open_forward_request_to_wire(request),
            session_mutation=True,
        )
        try:
            return forward_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid forward summary")

    def close_forward(self, request: CloseForwardRequest) -> None:
        self._require_capability(Capability.FORWARDS_WRITE)
        result = self._request(
            "forwards.close",
            close_forward_request_to_wire(request),
            mutation_session_id=SessionId(str(request.forward_id)),
            session_mutation=True,
        )
        if result is not None:
            self._fail_protocol("The daemon returned an invalid forward close result")

    def claim_forward(self, request: ClaimForwardRequest) -> ForwardSummary:
        """Claim ownership of an orphaned forward.

        A forward becomes orphaned when its originating client disconnects.
        Any subsequent client may call ``claim_forward`` to become the new
        owner, after which it can close or mutate the forward.
        """
        self._require_capability(Capability.FORWARDS_WRITE)
        result = self._request(
            "forwards.claim",
            claim_forward_request_to_wire(request),
        )
        try:
            return forward_summary_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid forward claim result")

    def send_terminal_input(self, request: TerminalInput) -> None:
        self._require_capability(Capability.TERMINAL_INPUT)
        if type(request) is not TerminalInput:
            raise TypeError("terminal input request is required")
        frame = encode_binary_frame(
            encode_terminal_payload(
                TerminalFrame(
                    kind=TerminalFrameKind.INPUT,
                    session_id=request.session_id,
                    attachment_id=request.attachment_id,
                    sequence=0,
                    data=request.data,
                )
            )
        )
        with self._send_lock:
            with self._state_lock:
                transport = self._socket
                if self._closed or transport is None:
                    raise self._closed_error()
            try:
                transport.sendall(frame)
            except OSError:
                self._fail_transport(
                    SshPilotError(
                        ErrorCode.TRANSPORT_CLOSED,
                        "The daemon transport failed",
                        retryable=True,
                        session_id=request.session_id,
                    )
                )
                raise self._closed_error() from None

    def resize_terminal(self, request: ResizeTerminalRequest) -> None:
        self._require_capability(Capability.TERMINAL_RESIZE)
        result = self._request(
            "terminal.resize",
            resize_terminal_request_to_wire(request),
        )
        if result is not None:
            self._fail_protocol("The daemon returned an invalid resize result")

    def claim_terminal_input(self, request: ClaimTerminalInputRequest) -> None:
        self._require_capability(Capability.TERMINAL_INPUT)
        result = self._request(
            "terminal.claim_input",
            claim_terminal_input_request_to_wire(request),
        )
        if result is not None:
            self._fail_protocol("The daemon returned an invalid claim input result")

    def release_terminal_input(self, request: ReleaseTerminalInputRequest) -> None:
        self._require_capability(Capability.TERMINAL_INPUT)
        result = self._request(
            "terminal.release_input",
            release_terminal_input_request_to_wire(request),
        )
        if result is not None:
            self._fail_protocol("The daemon returned an invalid release input result")

    def replay_terminal(self, request: ReplayRequest) -> ReplayResult:
        self._require_capability(Capability.TERMINAL_REPLAY)
        result = self._request(
            "terminal.replay",
            replay_request_to_wire(request),
        )
        try:
            return replay_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid replay result")

    def subscribe_terminal(
        self,
        session_id: SessionId,
        on_output: TerminalOutputCallback,
        *,
        on_continuity_lost: Optional[TerminalContinuityCallback] = None,
        on_eof: Optional[TerminalEofCallback] = None,
        on_error: Optional[TerminalErrorCallback] = None,
    ) -> TerminalSubscription:
        if not callable(on_output):
            raise TypeError("terminal output callback must be callable")
        with self._state_lock:
            if self._closed:
                raise self._closed_error()
            token = self._next_terminal_subscription
            self._next_terminal_subscription += 1
            self._terminal_subscribers.setdefault(session_id, {})[token] = (
                _TerminalCallbacks(
                    on_output,
                    on_continuity_lost,
                    on_eof,
                    on_error,
                )
            )

        def _cancel() -> None:
            with self._state_lock:
                subscribers = self._terminal_subscribers.get(session_id)
                if subscribers is None:
                    return
                subscribers.pop(token, None)
                if not subscribers:
                    self._terminal_subscribers.pop(session_id, None)

        return TerminalSubscription(_cancel)

    def list_interactions(self) -> List[InteractionSummary]:
        self._require_capability(Capability.INTERACTIONS_READ)
        values = self._request("interactions.list", {})
        if type(values) is not list:
            self._fail_protocol("The daemon returned an invalid interaction list")
        try:
            return [interaction_summary_from_wire(item) for item in values]
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid interaction data")

    def get_interaction(self, interaction_id: InteractionId) -> InteractionSummary:
        self._require_capability(Capability.INTERACTIONS_READ)
        value = self._request(
            "interactions.get",
            {"interaction_id": interaction_id},
        )
        try:
            return interaction_summary_from_wire(value)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid interaction data")

    def claim_interaction(self, interaction_id: InteractionId) -> InteractionClaim:
        self._require_capability(Capability.INTERACTIONS_RESPOND)
        value = self._request(
            "interactions.claim",
            {"interaction_id": interaction_id},
        )
        try:
            return interaction_claim_from_wire(value)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid interaction claim")

    def release_interaction(self, interaction_id: InteractionId) -> None:
        self._require_capability(Capability.INTERACTIONS_RESPOND)
        self._request(
            "interactions.release",
            {"interaction_id": interaction_id},
        )

    def respond_to_interaction(
        self,
        response: InteractionDecisionRequest,
    ) -> None:
        self._require_capability(Capability.INTERACTIONS_RESPOND)
        self._request(
            "interactions.respond",
            interaction_decision_to_wire(response),
        )

    def cancel_interaction(self, interaction_id: InteractionId) -> None:
        self._require_capability(Capability.INTERACTIONS_RESPOND)
        self._request(
            "interactions.cancel",
            {"interaction_id": interaction_id},
        )

    def send_interaction_secret(
        self,
        interaction_id: InteractionId,
        nonce: str,
        secret: bytearray,
    ) -> None:
        self._require_capability(Capability.INTERACTIONS_RESPOND)
        if type(secret) is not bytearray:
            raise TypeError("interaction secret must be a bytearray")
        try:
            nonce_bytes = bytes.fromhex(nonce)
            frame = encode_binary_frame(
                encode_secret_payload(
                    SecretFrame(
                        kind=SecretFrameKind.RESPONSE,
                        interaction_id=interaction_id,
                        nonce=nonce_bytes,
                        secret=secret,
                    )
                )
            )
            with self._send_lock:
                with self._state_lock:
                    transport = self._socket
                    if self._closed or transport is None:
                        raise self._closed_error()
                transport.sendall(frame)
        except ValueError:
            raise ValueError("interaction claim nonce is malformed") from None
        except OSError:
            self._fail_transport(
                SshPilotError(
                    ErrorCode.TRANSPORT_CLOSED,
                    "The daemon transport failed during secret submission",
                    retryable=False,
                )
            )
            raise self._closed_error() from None
        finally:
            secret[:] = b"\0" * len(secret)
            secret.clear()

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        with self._state_lock:
            if self._closed:
                raise self._closed_error()
        try:
            return self._publisher.subscribe(callback)
        except RuntimeError:
            raise self._closed_error() from None

    @property
    def is_closed(self) -> bool:
        """True after intentional close or transport failure."""

        with self._state_lock:
            return self._closed

    @property
    def transport_failed(self) -> bool:
        """True when the Unix transport failed unexpectedly."""

        with self._state_lock:
            return self._transport_failed

    def set_on_transport_lost(
        self, callback: Optional[Callable[[SshPilotError], None]]
    ) -> None:
        """Register a best-effort callback for unexpected transport failure.

        The callback must not block and must not call back into this client
        synchronously. Intentional :meth:`close` does not invoke it.
        """

        with self._state_lock:
            self._on_transport_lost = callback

    def close(self) -> None:
        with self._state_lock:
            if self._close_complete:
                return
            self._closed = True
            transport = self._socket
            self._socket = None
            pending = tuple(self._pending_requests.items())
            self._pending_requests.clear()
        self._close_transport(transport)
        for request_id, request in pending:
            request.error = SshPilotError(
                ErrorCode.TRANSPORT_CLOSED,
                "The daemon client was closed",
                request_id=request_id,
            )
            request.completed.set()
        self._stop_event_dispatch()
        self._stop_terminal_dispatch()
        self._publisher.close()
        self._join_thread(self._reader_thread)
        self._join_thread(self._event_thread)
        self._join_thread(self._terminal_thread)
        with self._state_lock:
            self._terminal_subscribers.clear()
            self._close_complete = True

    def _connect_and_handshake(self) -> None:
        transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        transport.settimeout(self._connect_timeout)
        try:
            transport.connect(str(self._socket_path))
        except (OSError, socket.timeout):
            transport.close()
            raise SshPilotError(
                ErrorCode.DAEMON_UNAVAILABLE,
                "The local sshPilot daemon is unavailable",
                retryable=True,
            ) from None
        transport.settimeout(None)
        self._socket = transport
        self._start_background_threads()
        request = HandshakeRequest(
            client_name=self._client_name,
            client_version=self._client_version,
            supported_protocol_versions=(PROTOCOL_VERSION,),
            client_capabilities=frozenset(
                {
                    Capability.TERMINAL_OUTPUT.value,
                    Capability.TERMINAL_INPUT.value,
                    Capability.TERMINAL_RESIZE.value,
                    Capability.TERMINAL_REPLAY.value,
                    Capability.INTERACTIONS_READ.value,
                    Capability.INTERACTIONS_RESPOND.value,
                    Capability.INTERACTIONS_EVENTS.value,
                    Capability.INTERACTIONS_HOST_KEY.value,
                    Capability.INTERACTIONS_PASSWORD.value,
                    Capability.INTERACTIONS_PASSPHRASE.value,
                }
            ),
            frontend_type=self._frontend_type,
            supported_frame_types=frozenset(
                {"binary-secret-v1", "binary-terminal-v1"}
            ),
        )
        result = self._request(
            "system.handshake",
            handshake_request_to_wire(request),
            protocol_version=PROTOCOL_VERSION,
        )
        try:
            handshake = handshake_result_from_wire(result)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned an invalid handshake")
        if (
            handshake.selected_protocol_version != PROTOCOL_VERSION
            or handshake.compatibility_status != "compatible"
        ):
            self._fail_protocol("The daemon selected an incompatible protocol")
        self._selected_protocol_version = handshake.selected_protocol_version
        self._server_instance_id = handshake.server_instance_id
        self._daemon_version = handshake.daemon_version
        self._daemon_started_at = handshake.daemon_started_at
        self._development_revision = handshake.development_revision
        self._daemon_api_implementation_version = (
            handshake.api_implementation_version
        )
        capabilities = self._request("system.get_capabilities", {})
        try:
            self._capabilities = capabilities_from_wire(capabilities)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid capabilities")
        self._log_daemon_identity()

    def _request(
        self,
        method: str,
        params: dict,
        *,
        protocol_version: Optional[str] = None,
        mutation_connection_id: Optional[ConnectionId] = None,
        mutation_session_id: Optional[SessionId] = None,
        session_mutation: bool = False,
    ):
        with self._request_lock:
            with self._state_lock:
                if self._closed:
                    raise self._closed_error()
                transport = self._socket
                if transport is None:
                    raise self._closed_error()
                request_id = new_request_id()
            request = RequestEnvelope(
                protocol_version=(
                    protocol_version
                    or self._selected_protocol_version
                    or PROTOCOL_VERSION
                ),
                request_id=request_id,
                method=method,
                params=params,
                client_id=self._client_id,
            )
            pending = _PendingRequest(
                completed=threading.Event(),
                method=method,
            )
            mutation_may_have_been_sent = False
            sent = False
            with self._state_lock:
                if self._closed or self._socket is not transport:
                    raise self._closed_error()
                self._pending_requests[request_id] = pending
            try:
                frame = encode_frame(encode_envelope(request))
                with self._send_lock:
                    mutation_may_have_been_sent = (
                        mutation_connection_id is not None
                        or method == "connections.create"
                        or session_mutation
                    )
                    transport.sendall(frame)
                    sent = True
                    pending.sent = True
            except (FramingError, TypeError, ValueError):
                self._fail_transport(
                    SshPilotError(
                        ErrorCode.PROTOCOL_ERROR,
                        "The daemon request could not be encoded",
                        request_id=request_id,
                    )
                )
            except OSError:
                self._fail_transport(
                    SshPilotError(
                        ErrorCode.TRANSPORT_CLOSED,
                        "The daemon transport failed",
                        retryable=True,
                        request_id=request_id,
                    )
                )

            if not pending.completed.wait(self._timeout):
                with self._state_lock:
                    response_arrived = (
                        pending.response is not None or pending.error is not None
                    )
                if not response_arrived:
                    elapsed = time.monotonic() - pending.started_at
                    diagnostics = self._transport_diagnostics(
                        request_id=request_id,
                        method=method,
                        elapsed=elapsed,
                        sent=sent,
                    )
                    logger.warning(
                        "Daemon request timed out: %s",
                        diagnostics,
                    )
                    self._fail_transport(
                        SshPilotError(
                            ErrorCode.TRANSPORT_TIMEOUT,
                            "The daemon request timed out",
                            retryable=True,
                            request_id=request_id,
                        )
                    )
                    pending.completed.wait()

            if pending.error is not None:
                if (
                    mutation_may_have_been_sent
                    and pending.error.code
                    in {
                        ErrorCode.TRANSPORT_CLOSED,
                        ErrorCode.TRANSPORT_TIMEOUT,
                    }
                ):
                    if session_mutation:
                        raise SshPilotError(
                            ErrorCode.MUTATION_AMBIGUOUS,
                            "The session change may have completed",
                            retryable=False,
                            request_id=request_id,
                            session_id=mutation_session_id,
                        )
                    raise SshPilotError(
                        ErrorCode.MUTATION_AMBIGUOUS,
                        "The connection change may have been saved",
                        retryable=False,
                        request_id=request_id,
                        connection_id=mutation_connection_id,
                    )
                raise pending.error
            response = pending.response
            if response is None:
                raise SshPilotError(
                    ErrorCode.TRANSPORT_CLOSED,
                    "The daemon transport closed unexpectedly",
                    retryable=True,
                    request_id=request_id,
                )
            if isinstance(response, ErrorResponseEnvelope):
                raise error_from_wire(response.error)
            return response.result

    def _reader_main(self) -> None:
        while True:
            with self._state_lock:
                if self._closed:
                    return
                transport = self._socket
            if transport is None:
                return
            try:
                incoming = receive_frame(transport)
            except EOFError:
                with self._state_lock:
                    closing = self._closed and not self._transport_failed
                if not closing:
                    self._fail_transport(
                        SshPilotError(
                            ErrorCode.TRANSPORT_CLOSED,
                            "The daemon transport closed unexpectedly",
                            retryable=True,
                        )
                    )
                return
            except FramingError:
                self._fail_transport(
                    SshPilotError(
                        ErrorCode.PROTOCOL_ERROR,
                        "The daemon sent an invalid transport frame",
                    )
                )
                return
            except (TypeError, ValueError):
                self._fail_transport(
                    SshPilotError(
                        ErrorCode.PROTOCOL_ERROR,
                        "The daemon sent an invalid protocol envelope",
                    )
                )
                return
            except OSError:
                with self._state_lock:
                    closing = self._closed and not self._transport_failed
                if not closing:
                    self._fail_transport(
                        SshPilotError(
                            ErrorCode.TRANSPORT_CLOSED,
                            "The daemon transport failed",
                            retryable=True,
                        )
                    )
                return

            if isinstance(incoming, TerminalFrame):
                if not self._receive_terminal(incoming):
                    return
                continue
            try:
                envelope = decode_envelope(incoming)
            except (TypeError, ValueError):
                self._fail_protocol_from_reader(
                    "The daemon sent an invalid protocol envelope"
                )
                return
            if isinstance(envelope, EventEnvelope):
                if not self._receive_event(envelope):
                    return
                continue
            if not isinstance(
                envelope,
                (SuccessResponseEnvelope, ErrorResponseEnvelope),
            ):
                self._fail_protocol_from_reader(
                    "The daemon sent an unexpected envelope type"
                )
                return
            expected_version = self._selected_protocol_version or PROTOCOL_VERSION
            if envelope.protocol_version != expected_version:
                self._fail_protocol_from_reader(
                    "The daemon response uses an unexpected protocol"
                )
                return
            with self._state_lock:
                pending = self._pending_requests.pop(
                    envelope.request_id,
                    None,
                )
            if pending is None:
                logger.debug(
                    "Late or unknown daemon response for cleared request id %s",
                    envelope.request_id,
                )
                self._fail_protocol_from_reader(
                    "The daemon response has an unknown request ID"
                )
                return
            pending.response = envelope
            pending.completed.set()

    def _receive_event(self, envelope: EventEnvelope) -> bool:
        expected_version = self._selected_protocol_version or PROTOCOL_VERSION
        if envelope.protocol_version != expected_version:
            self._fail_protocol_from_reader(
                "The daemon event uses an unexpected protocol"
            )
            return False
        try:
            event = public_event_from_envelope(envelope)
        except (TypeError, ValueError):
            self._fail_protocol_from_reader(
                "The daemon sent an invalid lifecycle event"
            )
            return False
        with self._state_lock:
            previous = self._last_event_sequence
            if previous is not None and event.sequence != previous + 1:
                invalid_sequence = True
            else:
                invalid_sequence = False
                self._last_event_sequence = event.sequence
        if invalid_sequence:
            self._fail_protocol_from_reader(
                "The daemon event sequence lost continuity"
            )
            return False
        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            self._fail_protocol_from_reader(
                "The daemon client event queue lost continuity"
            )
            return False
        return True

    def _receive_terminal(self, frame: TerminalFrame) -> bool:
        if frame.kind is TerminalFrameKind.CONTINUITY_LOST:
            with self._state_lock:
                expected = self._terminal_sequences.get(
                    frame.session_id,
                    frame.sequence,
                )
            self._queue_terminal_item(
                ("continuity", frame.session_id, expected, frame.sequence)
            )
            return True
        if frame.kind is TerminalFrameKind.INPUT_ERROR:
            try:
                code = ErrorCode(frame.data.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                self._fail_protocol_from_reader(
                    "The daemon sent an invalid terminal input error"
                )
                return False
            self._queue_terminal_item(
                SshPilotError(
                    code,
                    "The terminal input was rejected",
                    retryable=code is ErrorCode.TERMINAL_INPUT_BACKPRESSURE,
                    session_id=frame.session_id,
                )
            )
            return True
        if frame.kind is not TerminalFrameKind.OUTPUT:
            self._fail_protocol_from_reader(
                "The daemon sent an unexpected terminal frame"
            )
            return False
        with self._state_lock:
            expected = self._terminal_sequences.get(
                frame.session_id,
                frame.sequence,
            )
        data = frame.data
        sequence = frame.sequence
        if sequence > expected:
            self._queue_terminal_item(
                ("continuity", frame.session_id, expected, sequence)
            )
            expected = sequence
        elif sequence < expected:
            overlap = expected - sequence
            if overlap >= len(data):
                data = b""
            else:
                data = data[overlap:]
                sequence = expected
        next_sequence = sequence + len(data)
        with self._state_lock:
            previous = self._terminal_sequences.get(frame.session_id, 0)
            self._terminal_sequences[frame.session_id] = max(
                previous,
                next_sequence,
            )
        if data or frame.flags & TerminalFrameFlags.EOF:
            self._queue_terminal_item(
                TerminalOutput(
                    session_id=frame.session_id,
                    sequence=sequence,
                    data=data,
                    replay=bool(frame.flags & TerminalFrameFlags.REPLAY),
                    eof=bool(frame.flags & TerminalFrameFlags.EOF),
                )
            )
        if frame.flags & TerminalFrameFlags.TRUNCATED:
            self._queue_terminal_item(
                ("continuity", frame.session_id, frame.sequence, sequence)
            )
        return True

    def _queue_terminal_item(self, item: object) -> None:
        try:
            self._terminal_queue.put_nowait(item)
        except queue.Full:
            if isinstance(item, TerminalOutput):
                session_id = item.session_id
            elif isinstance(item, tuple) and len(item) > 1:
                session_id = SessionId(str(item[1]))
            else:
                return
            with self._state_lock:
                self._terminal_overflow.add(session_id)

    def _terminal_dispatch_main(self) -> None:
        while True:
            item = self._terminal_queue.get()
            if item is _EVENT_STOP:
                return
            with self._state_lock:
                overflow = tuple(self._terminal_overflow)
                self._terminal_overflow.clear()
            for session_id in overflow:
                self._deliver_terminal_continuity(session_id, 0, 0)
            if isinstance(item, TerminalOutput):
                with self._state_lock:
                    callbacks = tuple(
                        self._terminal_subscribers.get(
                            item.session_id,
                            {},
                        ).values()
                    )
                for callback in callbacks:
                    try:
                        callback.on_output(item)
                    except Exception:
                        continue
                    if item.eof and callback.on_eof is not None:
                        try:
                            callback.on_eof(item.session_id, item.next_sequence)
                        except Exception:
                            continue
                continue
            if isinstance(item, SshPilotError):
                with self._state_lock:
                    callbacks = tuple(
                        self._terminal_subscribers.get(
                            item.session_id,
                            {},
                        ).values()
                    )
                for callback in callbacks:
                    if callback.on_error is None:
                        continue
                    try:
                        callback.on_error(item)
                    except Exception:
                        continue
                continue
            if isinstance(item, tuple) and item and item[0] == "continuity":
                self._deliver_terminal_continuity(item[1], item[2], item[3])

    def _deliver_terminal_continuity(
        self,
        session_id: SessionId,
        expected: int,
        available: int,
    ) -> None:
        with self._state_lock:
            callbacks = tuple(
                self._terminal_subscribers.get(session_id, {}).values()
            )
        for callback in callbacks:
            if callback.on_continuity_lost is None:
                continue
            try:
                callback.on_continuity_lost(
                    session_id,
                    expected,
                    available,
                )
            except Exception:
                continue

    def _event_dispatch_main(self) -> None:
        while True:
            item = self._event_queue.get()
            if item is _EVENT_STOP:
                return
            if isinstance(item, _TransportFailureNotice):
                try:
                    self._publisher.publish(
                        EventType.ERROR_OCCURRED,
                        item.error.to_dict(),
                    )
                except RuntimeError:
                    pass
                self._publisher.close()
                return
            try:
                self._publisher.publish_existing(item)
            except RuntimeError:
                return

    def _fail_protocol_from_reader(self, message: str) -> None:
        self._fail_transport(SshPilotError(ErrorCode.PROTOCOL_ERROR, message))

    def _transport_diagnostics(
        self,
        *,
        request_id: Optional[RequestId] = None,
        method: Optional[str] = None,
        elapsed: Optional[float] = None,
        sent: Optional[bool] = None,
    ) -> dict:
        """Return safe fields for transport timeout / failure diagnostics."""
        with self._state_lock:
            transport = self._socket
            pending_count = len(self._pending_requests)
            server_instance_id = self._server_instance_id
            client_id = self._client_id
        if transport is None:
            socket_state = "none"
        else:
            try:
                socket_state = "closed" if transport.fileno() < 0 else "open"
            except OSError:
                socket_state = "closed"
        try:
            event_queue_depth = self._event_queue.qsize()
        except NotImplementedError:
            event_queue_depth = -1
        try:
            terminal_queue_depth = self._terminal_queue.qsize()
        except NotImplementedError:
            terminal_queue_depth = -1
        threads = self.threads_alive()
        return {
            "request_id": request_id,
            "method": method,
            "elapsed": elapsed,
            "client_id": client_id,
            "server_instance_id": server_instance_id,
            "socket_state": socket_state,
            "pending_request_count": pending_count,
            "event_queue_depth": event_queue_depth,
            "terminal_queue_depth": terminal_queue_depth,
            "sent": sent,
            "reader_thread_alive": threads["reader"],
            "event_thread_alive": threads["event"],
            "terminal_thread_alive": threads["terminal"],
            "transport_open": socket_state == "open",
        }

    def _fail_transport(self, error: SshPilotError) -> None:
        timeout_diagnostics: Optional[dict] = None
        with self._state_lock:
            if self._transport_failed or self._close_complete:
                return
            if error.code is ErrorCode.TRANSPORT_TIMEOUT:
                timed_out: Optional[_PendingRequest] = None
                for request_id, request in self._pending_requests.items():
                    if (
                        error.request_id is not None
                        and request_id == error.request_id
                    ):
                        timed_out = request
                        break
                elapsed = None
                method = None
                sent = None
                if timed_out is not None:
                    elapsed = time.monotonic() - timed_out.started_at
                    method = timed_out.method
                    sent = timed_out.sent
                timeout_diagnostics = self._transport_diagnostics(
                    request_id=error.request_id,
                    method=method,
                    elapsed=elapsed,
                    sent=sent,
                )
            self._transport_failed = True
            self._closed = True
            transport = self._socket
            self._socket = None
            pending = tuple(self._pending_requests.items())
            self._pending_requests.clear()
            lost_handler = self._on_transport_lost
            self._on_transport_lost = None
        if timeout_diagnostics is not None:
            logger.debug(
                "Failing daemon transport on timeout: %s",
                timeout_diagnostics,
            )
        self._close_transport(transport)
        for request_id, request in pending:
            request.error = SshPilotError(
                error.code,
                error.message,
                details=error.details,
                retryable=error.retryable,
                request_id=request_id,
            )
            request.completed.set()
        self._replace_event_queue_with_failure(error)
        self._stop_terminal_dispatch()
        if lost_handler is not None:
            try:
                lost_handler(error)
            except Exception:
                logger.debug(
                    "daemon transport-lost handler failed",
                    exc_info=True,
                )

    def _replace_event_queue_with_failure(self, error: SshPilotError) -> None:
        while True:
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._event_queue.put_nowait(_TransportFailureNotice(error))
        except queue.Full:
            pass

    def _start_background_threads(self) -> None:
        self._event_thread = threading.Thread(
            target=self._event_dispatch_main,
            name="sshpilot-daemon-events",
            daemon=True,
        )
        self._reader_thread = threading.Thread(
            target=self._reader_main,
            name="sshpilot-daemon-reader",
            daemon=True,
        )
        self._terminal_thread = threading.Thread(
            target=self._terminal_dispatch_main,
            name="sshpilot-daemon-terminal",
            daemon=True,
        )
        self._event_thread.start()
        self._terminal_thread.start()
        self._reader_thread.start()

    def _stop_event_dispatch(self) -> None:
        while True:
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._event_queue.put_nowait(_EVENT_STOP)
        except queue.Full:
            pass

    def _stop_terminal_dispatch(self) -> None:
        while True:
            try:
                self._terminal_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._terminal_queue.put_nowait(_EVENT_STOP)
        except queue.Full:
            pass

    @staticmethod
    def _join_thread(thread: Optional[threading.Thread]) -> None:
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    @staticmethod
    def _close_transport(transport: Optional[socket.socket]) -> None:
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            transport.close()

    def _fail_protocol(self, message: str) -> NoReturn:
        self._fail_transport(SshPilotError(ErrorCode.PROTOCOL_ERROR, message))
        raise SshPilotError(ErrorCode.PROTOCOL_ERROR, message)

    def _closed_error(self) -> SshPilotError:
        if self._transport_failed:
            return SshPilotError(
                ErrorCode.TRANSPORT_CLOSED,
                "The daemon transport is closed",
                retryable=True,
            )
        return SshPilotError(ErrorCode.INVALID_REQUEST, "The client is closed")

    def _require_capability(self, capability: Capability) -> None:
        if not self.get_capabilities().supports(capability):
            raise unsupported_capability(capability)

    @staticmethod
    def _unsupported(method_name: str) -> SshPilotError:
        return unsupported_capability(
            UNSUPPORTED_CLIENT_METHOD_CAPABILITIES[method_name]
        )

    def store_plugin_secret(self, plugin_id: str, key: str, value: str) -> bool:
        from .models.connections import store_plugin_secret_request_to_wire, StorePluginSecretRequest
        self._require_write_compatibility("store plugin secret")
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        req = StorePluginSecretRequest(plugin_id=plugin_id, key=key, value=value)
        result = self._request(
            "connections.store_plugin_secret",
            store_plugin_secret_request_to_wire(req),
        )
        return bool(result)

    def get_plugin_secret(self, plugin_id: str, key: str):
        from .models.connections import get_plugin_secret_request_to_wire, GetPluginSecretRequest
        req = GetPluginSecretRequest(plugin_id=plugin_id, key=key)
        result = self._request(
            "connections.get_plugin_secret",
            get_plugin_secret_request_to_wire(req),
        )
        return result

    def delete_plugin_secret(self, plugin_id: str, key: str) -> bool:
        from .models.connections import delete_plugin_secret_request_to_wire, DeletePluginSecretRequest
        self._require_write_compatibility("delete plugin secret")
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        req = DeletePluginSecretRequest(plugin_id=plugin_id, key=key)
        result = self._request(
            "connections.delete_plugin_secret",
            delete_plugin_secret_request_to_wire(req),
        )
        return bool(result)
