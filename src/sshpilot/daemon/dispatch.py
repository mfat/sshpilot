"""Explicit Protocol v1 daemon method dispatcher."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Hashable, Optional, Set, Union

from sshpilot import __version__ as sshpilot_version
from sshpilot.api.capabilities import Capabilities, Capability
from sshpilot.api.client import SshPilotClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.interaction_identity import interaction_uuid_from_id
from sshpilot.api.models.common import (
    ClientId,
    ClientInfo,
    CompatibilityResult,
    ConnectionId,
    CoreInfo,
    ForwardId,
    SessionId,
    SftpServiceId,
    TransferId,
    InteractionId,
)
from sshpilot.api.transport.codec import (
    attach_session_request_from_wire,
    attach_session_result_to_wire,
    attach_sftp_request_from_wire,
    capabilities_to_wire,
    claim_terminal_input_request_from_wire,
    close_forward_request_from_wire,
    close_session_request_from_wire,
    close_sftp_request_from_wire,
    connection_details_to_wire,
    connection_summary_to_wire,
    create_connection_request_from_wire,
    delete_connection_request_from_wire,
    delete_connection_result_to_wire,
    detach_session_request_from_wire,
    forward_summary_to_wire,
    handshake_request_from_wire,
    handshake_result_to_wire,
    interaction_claim_to_wire,
    interaction_decision_from_wire,
    interaction_summary_to_wire,
    list_directory_request_from_wire,
    list_directory_result_to_wire,
    open_forward_request_from_wire,
    open_session_request_from_wire,
    open_sftp_request_from_wire,
    cancel_transfer_request_from_wire,
    release_terminal_input_request_from_wire,
    remote_file_entry_to_wire,
    replay_request_from_wire,
    replay_result_to_wire,
    resize_terminal_request_from_wire,
    session_summary_to_wire,
    sftp_chmod_request_from_wire,
    sftp_path_request_from_wire,
    sftp_rename_request_from_wire,
    sftp_service_summary_to_wire,
    sftp_symlink_request_from_wire,
    start_transfer_request_from_wire,
    transfer_summary_to_wire,
    update_connection_request_from_wire,
)
from sshpilot.api.transport.envelopes import HandshakeRequest, HandshakeResult, RequestEnvelope
from sshpilot.api.version import API_IMPLEMENTATION_VERSION, PROTOCOL_VERSION
from sshpilot.daemon.config_reload import CONFIGURATION_COMMAND_KEY
from sshpilot.daemon.forward_runtime import ForwardRuntime
from sshpilot.daemon.interaction_broker import InteractionBroker
from sshpilot.daemon.session_runtime import SessionRuntime
from sshpilot.daemon.sftp_runtime import SftpServiceRuntime
from sshpilot.daemon.terminal_stream import ReplaySlice
from sshpilot.daemon.transfer_runtime import TransferRuntime

logger = logging.getLogger(__name__)

DAEMON_METHOD_CAPABILITIES = {
    "connections.get": Capability.CONNECTIONS_READ,
    "connections.list": Capability.CONNECTIONS_READ,
    "connections.create": Capability.CONNECTIONS_WRITE,
    "connections.delete": Capability.CONNECTIONS_WRITE,
    "connections.update": Capability.CONNECTIONS_WRITE,
    "daemon.status": Capability.DAEMON_STATUS,
    "daemon.diagnostics": Capability.DAEMON_STATUS,
    "daemon.stop": Capability.DAEMON_CONTROL,
    "daemon.restart": Capability.DAEMON_CONTROL,
    "interactions.cancel": Capability.INTERACTIONS_RESPOND,
    "interactions.claim": Capability.INTERACTIONS_RESPOND,
    "interactions.get": Capability.INTERACTIONS_READ,
    "interactions.list": Capability.INTERACTIONS_READ,
    "interactions.release": Capability.INTERACTIONS_RESPOND,
    "interactions.respond": Capability.INTERACTIONS_RESPOND,
    "sessions.list": Capability.SESSIONS_READ,
    "sessions.get": Capability.SESSIONS_READ,
    "sessions.open": Capability.SESSIONS_WRITE,
    "sessions.attach": Capability.SESSIONS_WRITE,
    "sessions.detach": Capability.SESSIONS_WRITE,
    "sessions.close": Capability.SESSIONS_WRITE,
    "terminal.replay": Capability.TERMINAL_REPLAY,
    "terminal.resize": Capability.TERMINAL_RESIZE,
    "terminal.claim_input": Capability.TERMINAL_INPUT,
    "terminal.release_input": Capability.TERMINAL_INPUT,
    "sftp.list_services": Capability.SFTP_READ,
    "sftp.get_service": Capability.SFTP_READ,
    "sftp.open": Capability.SFTP_WRITE,
    "sftp.attach": Capability.SFTP_WRITE,
    "sftp.detach": Capability.SFTP_WRITE,
    "sftp.close": Capability.SFTP_WRITE,
    "sftp.list": Capability.SFTP_READ,
    "sftp.stat": Capability.SFTP_METADATA,
    "sftp.lstat": Capability.SFTP_METADATA,
    "sftp.realpath": Capability.SFTP_METADATA,
    "sftp.readlink": Capability.SFTP_METADATA,
    "sftp.mkdir": Capability.SFTP_MUTATE,
    "sftp.rmdir": Capability.SFTP_MUTATE,
    "sftp.rename": Capability.SFTP_MUTATE,
    "sftp.remove": Capability.SFTP_MUTATE,
    "sftp.chmod": Capability.SFTP_MUTATE,
    "sftp.symlink": Capability.SFTP_MUTATE,
    "transfers.list": Capability.TRANSFERS_READ,
    "transfers.get": Capability.TRANSFERS_READ,
    "transfers.start": Capability.TRANSFERS_WRITE,
    "transfers.cancel": Capability.TRANSFERS_WRITE,
    "forwards.list": Capability.FORWARDS_READ,
    "forwards.get": Capability.FORWARDS_READ,
    "forwards.open": Capability.FORWARDS_WRITE,
    "forwards.close": Capability.FORWARDS_WRITE,
    "system.get_capabilities": None,
    "system.handshake": None,
}

# Methods rejected while draining (new work). Close/cancel/status remain.
DRAIN_REJECTED_METHODS = frozenset(
    {
        "connections.create",
        "connections.update",
        "connections.delete",
        "sessions.open",
        "sessions.attach",
        "sftp.open",
        "sftp.attach",
        "sftp.mkdir",
        "sftp.rmdir",
        "sftp.rename",
        "sftp.remove",
        "sftp.chmod",
        "sftp.symlink",
        "transfers.start",
        "forwards.open",
    }
)

DEFERRED_DAEMON_METHODS = frozenset(
    {
        "connections.create",
        "connections.update",
        "connections.delete",
        "sessions.open",
        "sessions.close",
        "sftp.open",
        "sftp.close",
        "sftp.list",
        "sftp.stat",
        "sftp.lstat",
        "sftp.realpath",
        "sftp.readlink",
        "sftp.mkdir",
        "sftp.rmdir",
        "sftp.rename",
        "sftp.remove",
        "sftp.chmod",
        "sftp.symlink",
        "transfers.start",
        "transfers.cancel",
        "forwards.open",
        "forwards.close",
    }
)


@dataclass(frozen=True)
class ImmediateResult:
    """A selector-safe handler result."""

    value: Any
    terminal_replay: Optional[ReplaySlice] = None
    terminal_session_id: Optional[SessionId] = None


@dataclass(frozen=True)
class DeferredResult:
    """A daemon-owned blocking operation for the bounded command executor.

    When ``respond_on_accept`` is true, the selector acknowledges the RPC as
    soon as the executor accepts the command. Completion and background errors
    must not send a second RPC response; they report through session state.
    """

    operation: Callable[[], Any]
    command_key: Hashable
    on_rejected: Callable[[], None]
    session_id: Optional[SessionId] = None
    connection_id: Optional[ConnectionId] = None
    respond_on_accept: bool = False
    accepted_result: Any = None
    on_background_error: Optional[Callable[[BaseException], None]] = None
    on_cancel: Optional[Callable[[], None]] = None


DispatchResult = Union[ImmediateResult, DeferredResult]


@dataclass
class ClientProtocolState:
    """Per-socket handshake and correlation state."""

    handshake_completed: bool = False
    client_id: Optional[ClientId] = None
    client_info: Optional[HandshakeRequest] = None
    selected_protocol_version: Optional[str] = None
    seen_request_ids: Set[str] = field(default_factory=set)


class RequestDispatcher:
    """Route the explicit Protocol v1 request methods implemented by the daemon."""

    def __init__(
        self,
        core_client: SshPilotClient,
        session_runtime: Optional[SessionRuntime] = None,
        interaction_broker: Optional[InteractionBroker] = None,
        sftp_runtime: Optional[SftpServiceRuntime] = None,
        transfer_runtime: Optional[TransferRuntime] = None,
        forward_runtime: Optional[ForwardRuntime] = None,
        *,
        lifecycle_controller: Any = None,
        diagnostics_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._core_client = core_client
        self._session_runtime = session_runtime or SessionRuntime(core_client)
        self._interaction_broker = interaction_broker
        self._sftp_runtime = sftp_runtime
        self._transfer_runtime = transfer_runtime
        self._forward_runtime = forward_runtime
        self._lifecycle = lifecycle_controller
        self._diagnostics_provider = diagnostics_provider
        self.server_instance_id = (
            lifecycle_controller.server_instance_id
            if lifecycle_controller is not None
            and hasattr(lifecycle_controller, "server_instance_id")
            else uuid.uuid4().hex
        )
        if lifecycle_controller is not None and hasattr(
            lifecycle_controller, "_server_instance_id"
        ):
            self.server_instance_id = lifecycle_controller._server_instance_id
        self._daemon_started_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        if lifecycle_controller is not None and hasattr(
            lifecycle_controller, "_started_at"
        ):
            started = lifecycle_controller._started_at
            if isinstance(started, datetime):
                self._daemon_started_at = started.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
        # Opaque development token only — never a filesystem or git path.
        self._development_revision = (
            os.environ.get("SSHPILOT_DEV_REVISION", "").strip()
        )
        self._shutting_down = False
        self.HANDLERS: Dict[str, Callable[[RequestEnvelope, ClientProtocolState], Any]] = {
            "system.handshake": self._handle_handshake,
            "system.get_capabilities": self._handle_get_capabilities,
            "daemon.status": self._handle_daemon_status,
            "daemon.diagnostics": self._handle_daemon_diagnostics,
            "daemon.stop": self._handle_daemon_stop,
            "daemon.restart": self._handle_daemon_restart,
            "connections.list": self._handle_list_connections,
            "connections.get": self._handle_get_connection,
            "connections.create": self._handle_create_connection,
            "connections.update": self._handle_update_connection,
            "connections.delete": self._handle_delete_connection,
            "interactions.list": self._handle_list_interactions,
            "interactions.get": self._handle_get_interaction,
            "interactions.claim": self._handle_claim_interaction,
            "interactions.release": self._handle_release_interaction,
            "interactions.respond": self._handle_respond_to_interaction,
            "interactions.cancel": self._handle_cancel_interaction,
            "sessions.list": self._handle_list_sessions,
            "sessions.get": self._handle_get_session,
            "sessions.open": self._handle_open_session,
            "sessions.attach": self._handle_attach_session,
            "sessions.detach": self._handle_detach_session,
            "sessions.close": self._handle_close_session,
            "terminal.replay": self._handle_replay_terminal,
            "terminal.resize": self._handle_resize_terminal,
            "terminal.claim_input": self._handle_claim_terminal_input,
            "terminal.release_input": self._handle_release_terminal_input,
            "sftp.list_services": self._handle_list_sftp_services,
            "sftp.get_service": self._handle_get_sftp_service,
            "sftp.open": self._handle_open_sftp_service,
            "sftp.attach": self._handle_attach_sftp_service,
            "sftp.detach": self._handle_detach_sftp_service,
            "sftp.close": self._handle_close_sftp_service,
            "sftp.list": self._handle_sftp_list_directory,
            "sftp.stat": self._handle_sftp_stat,
            "sftp.lstat": self._handle_sftp_lstat,
            "sftp.realpath": self._handle_sftp_realpath,
            "sftp.readlink": self._handle_sftp_readlink,
            "sftp.mkdir": self._handle_sftp_mkdir,
            "sftp.rmdir": self._handle_sftp_rmdir,
            "sftp.rename": self._handle_sftp_rename,
            "sftp.remove": self._handle_sftp_remove,
            "sftp.chmod": self._handle_sftp_chmod,
            "sftp.symlink": self._handle_sftp_symlink,
            "transfers.list": self._handle_list_transfers,
            "transfers.get": self._handle_get_transfer,
            "transfers.start": self._handle_start_transfer,
            "transfers.cancel": self._handle_cancel_transfer,
            "forwards.list": self._handle_list_forwards,
            "forwards.get": self._handle_get_forward,
            "forwards.open": self._handle_open_forward,
            "forwards.close": self._handle_close_forward,
        }

    def begin_shutdown(self) -> None:
        self._shutting_down = True

    def dispatch(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DispatchResult:
        if self._shutting_down and request.method in DRAIN_REJECTED_METHODS:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The daemon is shutting down",
                retryable=True,
                request_id=request.request_id,
            )
        if self._shutting_down and request.method == "system.handshake":
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The daemon is shutting down",
                retryable=True,
                request_id=request.request_id,
            )
        if request.request_id in state.seen_request_ids:
            raise SshPilotError(
                ErrorCode.PROTOCOL_ERROR,
                "The request ID has already been used on this connection",
                request_id=request.request_id,
            )
        state.seen_request_ids.add(request.request_id)

        if request.method != "system.handshake":
            if not state.handshake_completed:
                raise SshPilotError(
                    ErrorCode.HANDSHAKE_REQUIRED,
                    "A protocol handshake is required before ordinary requests",
                    request_id=request.request_id,
                )
            if request.client_id != state.client_id:
                raise SshPilotError(
                    ErrorCode.PROTOCOL_ERROR,
                    "The request client ID does not match the handshake",
                    request_id=request.request_id,
                )
            if request.protocol_version != state.selected_protocol_version:
                raise SshPilotError(
                    ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                    "The request protocol version is not negotiated",
                    request_id=request.request_id,
                )
        handler = self.HANDLERS.get(request.method)
        if handler is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_METHOD,
                "The requested daemon method is not supported",
                request_id=request.request_id,
            )
        try:
            result = handler(request, state)
            if isinstance(result, ImmediateResult):
                return result
            if isinstance(result, DeferredResult):
                if request.method not in DEFERRED_DAEMON_METHODS:
                    raise RuntimeError("immediate daemon method returned deferred work")
                return result
            if request.method in DEFERRED_DAEMON_METHODS:
                return ImmediateResult(result)
            return ImmediateResult(result)
        except SshPilotError:
            raise
        except (TypeError, ValueError):
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "The daemon request is malformed",
                request_id=request.request_id,
            ) from None
        except Exception as error:
            logger.error(
                "Daemon method %s failed (%s)",
                request.method,
                type(error).__name__,
            )
            raise SshPilotError(
                ErrorCode.INTERNAL_ERROR,
                "The daemon could not complete the request",
                retryable=True,
                request_id=request.request_id,
            ) from None

    def _handle_handshake(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> dict:
        if state.handshake_completed:
            raise SshPilotError(
                ErrorCode.HANDSHAKE_ALREADY_COMPLETED,
                "The protocol handshake is already complete",
                request_id=request.request_id,
            )
        metadata = handshake_request_from_wire(request.params)
        if PROTOCOL_VERSION not in metadata.supported_protocol_versions:
            raise SshPilotError(
                ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                "No supported protocol version could be negotiated",
                details={"supported_protocol_versions": [PROTOCOL_VERSION]},
                request_id=request.request_id,
            )
        state.handshake_completed = True
        state.client_id = ClientId(request.client_id)
        state.client_info = metadata
        state.selected_protocol_version = PROTOCOL_VERSION
        core_capabilities = self._core_client.get_capabilities()
        result = HandshakeResult(
            daemon_version=sshpilot_version,
            core_version=core_capabilities.core.version,
            selected_protocol_version=PROTOCOL_VERSION,
            daemon_capabilities=self._safe_capabilities(
                core_capabilities.supported,
                terminal_frames=(
                    "binary-terminal-v1" in metadata.supported_frame_types
                    and self._session_runtime.terminal_supported
                ),
                secret_frames=(
                    self._interaction_broker is not None
                    and "binary-secret-v1" in metadata.supported_frame_types
                ),
                sftp=self._sftp_runtime is not None,
                transfers=self._transfer_runtime is not None,
                forwards=self._forward_runtime is not None,
            ),
            compatibility_status="compatible",
            server_instance_id=self.server_instance_id,
            daemon_started_at=self._daemon_started_at,
            development_revision=self._development_revision,
            api_implementation_version=API_IMPLEMENTATION_VERSION,
        )
        return handshake_result_to_wire(result)

    def _handle_get_capabilities(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> dict:
        self._require_empty_params(request)
        return capabilities_to_wire(self._capabilities_for(state))

    def _handle_daemon_status(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        from sshpilot.api.transport.codec import daemon_status_to_wire

        self._require_empty_params(request)
        if self._lifecycle is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Daemon lifecycle status is unavailable",
            )
        return daemon_status_to_wire(self._lifecycle.status())

    def _handle_daemon_diagnostics(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        from sshpilot.api.transport.codec import daemon_diagnostics_to_wire

        self._require_empty_params(request)
        if self._diagnostics_provider is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Daemon diagnostics are unavailable",
            )
        return daemon_diagnostics_to_wire(self._diagnostics_provider())

    def _handle_daemon_stop(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        from sshpilot.api.transport.codec import (
            daemon_stop_result_to_wire,
            stop_daemon_request_from_wire,
        )

        if self._lifecycle is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Daemon control is unavailable",
            )
        result = self._lifecycle.request_stop(
            stop_daemon_request_from_wire(dict(request.params))
        )
        return daemon_stop_result_to_wire(result)

    def _handle_daemon_restart(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        from sshpilot.api.transport.codec import (
            daemon_stop_result_to_wire,
            restart_daemon_request_from_wire,
        )

        if self._lifecycle is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Daemon control is unavailable",
            )
        result = self._lifecycle.request_restart(
            restart_daemon_request_from_wire(dict(request.params))
        )
        return daemon_stop_result_to_wire(result)

    def _handle_list_connections(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> list:
        self._require_empty_params(request)
        return [
            connection_summary_to_wire(item)
            for item in self._core_client.list_connections()
        ]

    def _handle_get_connection(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        if set(request.params) != {"connection_id"}:
            raise ValueError("connections.get requires only connection_id")
        connection_id = request.params["connection_id"]
        if type(connection_id) is not str or not connection_id.strip():
            raise ValueError("connection_id must be a non-empty string")
        return connection_details_to_wire(
            self._core_client.get_connection(ConnectionId(connection_id))
        )

    def _handle_create_connection(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> DeferredResult:
        mutation = create_connection_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: connection_details_to_wire(
                self._core_client.create_connection(mutation)
            ),
            command_key=CONFIGURATION_COMMAND_KEY,
            on_rejected=lambda: None,
        )

    def _handle_update_connection(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> DeferredResult:
        if set(request.params) != {"connection_id", "update"}:
            raise ValueError(
                "connections.update requires connection_id and update"
            )
        connection_id = request.params["connection_id"]
        if type(connection_id) is not str or not connection_id.strip():
            raise ValueError("connection_id must be a non-empty string")
        mutation = update_connection_request_from_wire(request.params["update"])
        typed_id = ConnectionId(connection_id)
        return DeferredResult(
            operation=lambda: connection_details_to_wire(
                self._core_client.update_connection(typed_id, mutation)
            ),
            command_key=CONFIGURATION_COMMAND_KEY,
            on_rejected=lambda: None,
            connection_id=typed_id,
        )

    def _handle_delete_connection(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> DeferredResult:
        mutation = delete_connection_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: delete_connection_result_to_wire(
                self._core_client.delete_connection(mutation)
            ),
            command_key=CONFIGURATION_COMMAND_KEY,
            on_rejected=lambda: None,
            connection_id=mutation.connection_id,
        )

    def _handle_list_sessions(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> list:
        self._require_empty_params(request)
        return [
            session_summary_to_wire(item)
            for item in self._session_runtime.list_sessions()
        ]

    def _handle_list_interactions(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> list:
        self._require_empty_params(request)
        broker = self._required_interaction_broker()
        return [
            interaction_summary_to_wire(item)
            for item in broker.list(self._required_client_id(state))
        ]

    def _handle_get_interaction(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> dict:
        interaction_id = self._interaction_id_param(request)
        return interaction_summary_to_wire(
            self._required_interaction_broker().get(
                interaction_id,
                self._required_client_id(state),
            )
        )

    def _handle_claim_interaction(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> dict:
        interaction_id = self._interaction_id_param(request)
        if (
            state.client_info is None
            or "binary-secret-v1" not in state.client_info.supported_frame_types
        ):
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Binary secret transport was not negotiated",
            )
        return interaction_claim_to_wire(
            self._required_interaction_broker().claim(
                interaction_id,
                self._required_client_id(state),
            )
        )

    def _handle_release_interaction(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> None:
        self._required_interaction_broker().release(
            self._interaction_id_param(request),
            self._required_client_id(state),
        )
        return None

    def _handle_respond_to_interaction(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> None:
        self._required_interaction_broker().respond(
            interaction_decision_from_wire(request.params),
            self._required_client_id(state),
        )
        return None

    def _handle_cancel_interaction(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> None:
        self._required_interaction_broker().cancel(
            self._interaction_id_param(request),
            client_id=self._required_client_id(state),
        )
        return None

    def _handle_get_session(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        if set(request.params) != {"session_id"}:
            raise ValueError("sessions.get requires only session_id")
        session_id = request.params["session_id"]
        if type(session_id) is not str or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        return session_summary_to_wire(
            self._session_runtime.get_session(SessionId(session_id))
        )

    def _handle_open_session(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        session_request = open_session_request_from_wire(request.params)
        prepared = self._session_runtime.prepare_open_session(
            session_request,
            client_id=client_id,
        )
        prepared_wire = session_summary_to_wire(prepared)

        return DeferredResult(
            operation=lambda: self._session_runtime.start_session(prepared.id),
            command_key=prepared.id,
            session_id=prepared.id,
            on_rejected=lambda: self._session_runtime.reject_pending_start(
                prepared.id
            ),
            respond_on_accept=True,
            accepted_result=prepared_wire,
            on_background_error=lambda error: (
                self._session_runtime.fail_pending_start(prepared.id, error)
            ),
            on_cancel=lambda: self._session_runtime.reject_pending_start(
                prepared.id
            ),
        )

    def _handle_attach_session(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> ImmediateResult:
        client_id = self._required_client_id(state)
        session_request = attach_session_request_from_wire(request.params)
        if (
            session_request.want_terminal_output
            and (
                state.client_info is None
                or "binary-terminal-v1"
                not in state.client_info.supported_frame_types
            )
        ):
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Binary terminal transport was not negotiated",
                session_id=session_request.session_id,
            )
        result, replay = self._session_runtime.attach_replay(
            session_request,
            client_id=client_id,
        )
        return ImmediateResult(
            attach_session_result_to_wire(result),
            terminal_replay=replay if session_request.want_terminal_output else None,
            terminal_session_id=(
                session_request.session_id
                if session_request.want_terminal_output
                else None
            ),
        )

    def _handle_detach_session(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> None:
        client_id = self._required_client_id(state)
        session_request = detach_session_request_from_wire(request.params)
        self._session_runtime.detach_session(
            session_request,
            client_id=client_id,
        )
        return None

    def _handle_close_session(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> Optional[DeferredResult]:
        session_request = close_session_request_from_wire(request.params)
        if not self._session_runtime.prepare_close_session(session_request):
            return None

        def _close() -> None:
            self._session_runtime.finish_close_session(session_request.session_id)
            return None

        return DeferredResult(
            operation=_close,
            command_key=session_request.session_id,
            session_id=session_request.session_id,
            on_rejected=lambda: self._session_runtime.reject_pending_close(
                session_request.session_id
            ),
        )

    def _handle_resize_terminal(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> None:
        self._session_runtime.resize_terminal(
            resize_terminal_request_from_wire(request.params),
            client_id=self._required_client_id(state),
        )
        return None

    def _handle_claim_terminal_input(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> None:
        claim_request = claim_terminal_input_request_from_wire(request.params)
        self._session_runtime.claim_input(
            session_id=claim_request.session_id,
            attachment_id=claim_request.attachment_id,
            client_id=self._required_client_id(state),
        )
        return None

    def _handle_release_terminal_input(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> None:
        release_request = release_terminal_input_request_from_wire(request.params)
        self._session_runtime.release_input(
            session_id=release_request.session_id,
            attachment_id=release_request.attachment_id,
            client_id=self._required_client_id(state),
        )
        return None

    def _handle_replay_terminal(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> ImmediateResult:
        if (
            state.client_info is None
            or "binary-terminal-v1" not in state.client_info.supported_frame_types
        ):
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Binary terminal transport was not negotiated",
            )
        result, replay = self._session_runtime.replay_terminal(
            replay_request_from_wire(request.params),
            client_id=self._required_client_id(state),
        )
        return ImmediateResult(
            replay_result_to_wire(result),
            terminal_replay=replay,
            terminal_session_id=result.session_id,
        )

    # -- SFTP services --------------------------------------------------
    def _handle_list_sftp_services(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> list:
        self._require_empty_params(request)
        return [
            sftp_service_summary_to_wire(item)
            for item in self._required_sftp_runtime().list_services()
        ]

    def _handle_get_sftp_service(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        if set(request.params) != {"service_id"}:
            raise ValueError("sftp.get_service requires only service_id")
        service_id = request.params["service_id"]
        if type(service_id) is not str or not service_id.strip():
            raise ValueError("service_id must be a non-empty string")
        return sftp_service_summary_to_wire(
            self._required_sftp_runtime().get_service(SftpServiceId(service_id))
        )

    def _handle_open_sftp_service(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        sftp_request = open_sftp_request_from_wire(request.params)
        prepared = runtime.prepare_open_service(sftp_request, client_id=client_id)
        prepared_wire = sftp_service_summary_to_wire(prepared)
        return DeferredResult(
            operation=lambda: runtime.start_service(prepared.id),
            command_key=prepared.id,
            session_id=SessionId(str(prepared.id)),
            connection_id=prepared.connection_id,
            on_rejected=lambda: runtime.reject_pending_start(prepared.id),
            respond_on_accept=True,
            accepted_result=prepared_wire,
            on_background_error=lambda error: runtime.fail_pending_start(
                prepared.id, error
            ),
            on_cancel=lambda: runtime.reject_pending_start(prepared.id),
        )

    def _handle_attach_sftp_service(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> dict:
        client_id = self._required_client_id(state)
        sftp_request = attach_sftp_request_from_wire(request.params)
        return sftp_service_summary_to_wire(
            self._required_sftp_runtime().attach_service(
                sftp_request, client_id=client_id
            )
        )

    def _handle_detach_sftp_service(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> None:
        client_id = self._required_client_id(state)
        if set(request.params) != {"service_id"}:
            raise ValueError("sftp.detach requires only service_id")
        service_id = request.params["service_id"]
        if type(service_id) is not str or not service_id.strip():
            raise ValueError("service_id must be a non-empty string")
        self._required_sftp_runtime().detach_service(
            SftpServiceId(service_id),
            client_id=client_id,
        )
        return None

    def _handle_close_sftp_service(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> Optional[DeferredResult]:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        close_request = close_sftp_request_from_wire(request.params)
        if not runtime.prepare_close_service(close_request, client_id=client_id):
            return None

        def _close() -> None:
            runtime.finish_close_service(close_request.service_id)
            return None

        return DeferredResult(
            operation=_close,
            command_key=close_request.service_id,
            session_id=SessionId(str(close_request.service_id)),
            on_rejected=lambda: runtime.reject_pending_close(
                close_request.service_id
            ),
        )

    def _handle_sftp_list_directory(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        list_request = list_directory_request_from_wire(request.params)
        command_key = list_request.service_id or list_request.connection_id
        return DeferredResult(
            operation=lambda: list_directory_result_to_wire(
                runtime.list_directory(list_request, client_id=client_id)
            ),
            command_key=command_key,
            connection_id=list_request.connection_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_stat(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        path_request = sftp_path_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: remote_file_entry_to_wire(
                runtime.stat_path(path_request, client_id=client_id)
            ),
            command_key=path_request.service_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_lstat(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        path_request = sftp_path_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: remote_file_entry_to_wire(
                runtime.lstat_path(path_request, client_id=client_id)
            ),
            command_key=path_request.service_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_realpath(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        path_request = sftp_path_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: {
                "path": runtime.realpath(path_request, client_id=client_id)
            },
            command_key=path_request.service_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_readlink(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        path_request = sftp_path_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: {
                "path": runtime.readlink(path_request, client_id=client_id)
            },
            command_key=path_request.service_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_mkdir(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        path_request = sftp_path_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: runtime.mkdir(path_request, client_id=client_id),
            command_key=path_request.service_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_rmdir(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        path_request = sftp_path_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: runtime.rmdir(path_request, client_id=client_id),
            command_key=path_request.service_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_remove(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        path_request = sftp_path_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: runtime.remove(path_request, client_id=client_id),
            command_key=path_request.service_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_rename(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        rename_request = sftp_rename_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: runtime.rename(rename_request, client_id=client_id),
            command_key=rename_request.service_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_chmod(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        chmod_request = sftp_chmod_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: runtime.chmod(chmod_request, client_id=client_id),
            command_key=chmod_request.service_id,
            on_rejected=lambda: None,
        )

    def _handle_sftp_symlink(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_sftp_runtime()
        symlink_request = sftp_symlink_request_from_wire(request.params)
        return DeferredResult(
            operation=lambda: runtime.symlink(symlink_request, client_id=client_id),
            command_key=symlink_request.service_id,
            on_rejected=lambda: None,
        )

    # -- transfers --------------------------------------------------------
    def _handle_list_transfers(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> list:
        self._require_empty_params(request)
        return [
            transfer_summary_to_wire(item)
            for item in self._required_transfer_runtime().list_transfers()
        ]

    def _handle_get_transfer(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        if set(request.params) != {"transfer_id"}:
            raise ValueError("transfers.get requires only transfer_id")
        transfer_id = request.params["transfer_id"]
        if type(transfer_id) is not str or not transfer_id.strip():
            raise ValueError("transfer_id must be a non-empty string")
        return transfer_summary_to_wire(
            self._required_transfer_runtime().get_transfer(TransferId(transfer_id))
        )

    def _handle_start_transfer(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_transfer_runtime()
        transfer_request = start_transfer_request_from_wire(request.params)
        prepared = runtime.prepare_start_transfer(transfer_request, client_id=client_id)
        prepared_wire = transfer_summary_to_wire(prepared)
        return DeferredResult(
            operation=lambda: runtime.run_transfer(prepared.id),
            command_key=prepared.id,
            connection_id=prepared.connection_id,
            on_rejected=lambda: runtime.reject_pending_start(prepared.id),
            respond_on_accept=True,
            accepted_result=prepared_wire,
            on_background_error=lambda error: runtime.fail_pending_start(
                prepared.id, error
            ),
            on_cancel=lambda: runtime.reject_pending_start(prepared.id),
        )

    def _handle_cancel_transfer(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_transfer_runtime()
        cancel_request = cancel_transfer_request_from_wire(request.params)

        def _cancel() -> None:
            runtime.prepare_cancel_transfer(cancel_request, client_id=client_id)

        return DeferredResult(
            operation=_cancel,
            command_key=cancel_request.transfer_id,
            on_rejected=lambda: None,
        )

    # -- forwards -----------------------------------------------------
    def _handle_list_forwards(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> list:
        self._require_empty_params(request)
        return [
            forward_summary_to_wire(item)
            for item in self._required_forward_runtime().list_forwards()
        ]

    def _handle_get_forward(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        if set(request.params) != {"forward_id"}:
            raise ValueError("forwards.get requires only forward_id")
        forward_id = request.params["forward_id"]
        if type(forward_id) is not str or not forward_id.strip():
            raise ValueError("forward_id must be a non-empty string")
        return forward_summary_to_wire(
            self._required_forward_runtime().get_forward(ForwardId(forward_id))
        )

    def _handle_open_forward(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DeferredResult:
        client_id = self._required_client_id(state)
        runtime = self._required_forward_runtime()
        forward_request = open_forward_request_from_wire(request.params)
        prepared = runtime.prepare_open_forward(forward_request, client_id=client_id)
        prepared_wire = forward_summary_to_wire(prepared)
        return DeferredResult(
            operation=lambda: runtime.start_forward(prepared.id),
            command_key=prepared.id,
            session_id=SessionId(str(prepared.id)),
            connection_id=prepared.connection_id,
            on_rejected=lambda: runtime.reject_pending_start(prepared.id),
            respond_on_accept=True,
            accepted_result=prepared_wire,
            on_background_error=lambda error: runtime.fail_pending_start(
                prepared.id, error
            ),
            on_cancel=lambda: runtime.reject_pending_start(prepared.id),
        )

    def _handle_close_forward(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> Optional[DeferredResult]:
        client_id = self._required_client_id(state)
        runtime = self._required_forward_runtime()
        close_request = close_forward_request_from_wire(request.params)
        if not runtime.prepare_close_forward(close_request, client_id=client_id):
            return None

        def _close() -> None:
            runtime.finish_close_forward(close_request.forward_id)
            return None

        return DeferredResult(
            operation=_close,
            command_key=close_request.forward_id,
            session_id=SessionId(str(close_request.forward_id)),
            on_rejected=lambda: runtime.reject_pending_close(
                close_request.forward_id
            ),
        )

    def _capabilities_for(self, state: ClientProtocolState) -> Capabilities:
        metadata = state.client_info
        if metadata is None or state.client_id is None:
            raise SshPilotError(
                ErrorCode.HANDSHAKE_REQUIRED,
                "A protocol handshake is required before capability discovery",
            )
        core = self._core_client.get_capabilities()
        return Capabilities(
            protocol_version=PROTOCOL_VERSION,
            api_implementation_version=API_IMPLEMENTATION_VERSION,
            client=ClientInfo(
                name=metadata.client_name,
                version=metadata.client_version,
                client_id=state.client_id,
            ),
            core=CoreInfo(
                name=core.core.name,
                version=core.core.version,
                implementation="daemon",
            ),
            supported=self._safe_capabilities(
                core.supported,
                terminal_frames=(
                    "binary-terminal-v1" in metadata.supported_frame_types
                    and self._session_runtime.terminal_supported
                ),
                secret_frames=(
                    self._interaction_broker is not None
                    and "binary-secret-v1" in metadata.supported_frame_types
                ),
                sftp=self._sftp_runtime is not None,
                transfers=self._transfer_runtime is not None,
                forwards=self._forward_runtime is not None,
            ),
            compatibility=CompatibilityResult(
                compatible=True,
                protocol_version=PROTOCOL_VERSION,
            ),
        )

    @staticmethod
    def _safe_capabilities(
        supported: FrozenSet[Capability],
        *,
        terminal_frames: bool = False,
        secret_frames: bool = False,
        sftp: bool = False,
        transfers: bool = False,
        forwards: bool = False,
    ) -> FrozenSet[Capability]:
        # Protocol v1 exposes connection CRUD/events and daemon session lifecycle.
        connection_capabilities = frozenset(
            item
            for item in supported
            if item
            in {
                Capability.CONNECTIONS_READ,
                Capability.CONNECTIONS_EVENTS,
                Capability.CONNECTIONS_WRITE,
            }
        )
        daemon_capabilities = connection_capabilities | frozenset(
            {
                Capability.SESSIONS_READ,
                Capability.SESSIONS_WRITE,
                Capability.SESSIONS_EVENTS,
                Capability.DAEMON_STATUS,
                Capability.DAEMON_CONTROL,
                Capability.DAEMON_EVENTS,
            }
        )
        if terminal_frames:
            daemon_capabilities |= frozenset(
                {
                    Capability.TERMINAL_OUTPUT,
                    Capability.TERMINAL_INPUT,
                    Capability.TERMINAL_RESIZE,
                    Capability.TERMINAL_REPLAY,
                }
            )
        if secret_frames:
            daemon_capabilities |= frozenset(
                {
                    Capability.INTERACTIONS_READ,
                    Capability.INTERACTIONS_RESPOND,
                    Capability.INTERACTIONS_EVENTS,
                    Capability.INTERACTIONS_HOST_KEY,
                    Capability.INTERACTIONS_PASSWORD,
                    Capability.INTERACTIONS_PASSPHRASE,
                }
            )
        if sftp:
            daemon_capabilities |= frozenset(
                {
                    Capability.SFTP_READ,
                    Capability.SFTP_WRITE,
                    Capability.SFTP_EVENTS,
                    Capability.SFTP_METADATA,
                    Capability.SFTP_MUTATE,
                }
            )
        if transfers:
            daemon_capabilities |= frozenset(
                {
                    Capability.TRANSFERS_READ,
                    Capability.TRANSFERS_WRITE,
                    Capability.TRANSFERS_EVENTS,
                    Capability.TRANSFERS_UPLOAD,
                    Capability.TRANSFERS_DOWNLOAD,
                }
            )
        if forwards:
            daemon_capabilities |= frozenset(
                {
                    Capability.FORWARDS_READ,
                    Capability.FORWARDS_WRITE,
                    Capability.FORWARDS_EVENTS,
                    Capability.FORWARDS_LOCAL,
                    Capability.FORWARDS_REMOTE,
                    Capability.FORWARDS_DYNAMIC,
                }
            )
        return daemon_capabilities

    def _required_interaction_broker(self) -> InteractionBroker:
        if self._interaction_broker is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Typed interactions are unavailable",
            )
        return self._interaction_broker

    def _required_sftp_runtime(self) -> SftpServiceRuntime:
        if self._sftp_runtime is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "SFTP services are unavailable",
            )
        return self._sftp_runtime

    def _required_transfer_runtime(self) -> TransferRuntime:
        if self._transfer_runtime is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "File transfers are unavailable",
            )
        return self._transfer_runtime

    def _required_forward_runtime(self) -> ForwardRuntime:
        if self._forward_runtime is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Port forwards are unavailable",
            )
        return self._forward_runtime

    @staticmethod
    def _interaction_id_param(request: RequestEnvelope) -> InteractionId:
        if set(request.params) != {"interaction_id"}:
            raise ValueError("interaction request requires only interaction_id")
        value = request.params["interaction_id"]
        if type(value) is not str:
            raise ValueError("interaction ID must be a string")
        interaction_id = InteractionId(value)
        interaction_uuid_from_id(interaction_id)
        return interaction_id

    @staticmethod
    def _require_empty_params(request: RequestEnvelope) -> None:
        if request.params:
            raise ValueError("method does not accept parameters")

    @staticmethod
    def _required_client_id(state: ClientProtocolState) -> ClientId:
        if state.client_id is None:
            raise SshPilotError(
                ErrorCode.HANDSHAKE_REQUIRED,
                "A protocol handshake is required before session operations",
            )
        return state.client_id
