"""Explicit Protocol v1 daemon method dispatcher."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
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
    SessionId,
    InteractionId,
)
from sshpilot.api.transport.codec import (
    attach_session_request_from_wire,
    attach_session_result_to_wire,
    capabilities_to_wire,
    claim_terminal_input_request_from_wire,
    close_session_request_from_wire,
    connection_details_to_wire,
    connection_summary_to_wire,
    create_connection_request_from_wire,
    delete_connection_request_from_wire,
    delete_connection_result_to_wire,
    detach_session_request_from_wire,
    handshake_request_from_wire,
    handshake_result_to_wire,
    interaction_claim_to_wire,
    interaction_decision_from_wire,
    interaction_summary_to_wire,
    open_session_request_from_wire,
    release_terminal_input_request_from_wire,
    replay_request_from_wire,
    replay_result_to_wire,
    resize_terminal_request_from_wire,
    session_summary_to_wire,
    update_connection_request_from_wire,
)
from sshpilot.api.transport.envelopes import HandshakeRequest, HandshakeResult, RequestEnvelope
from sshpilot.api.version import API_IMPLEMENTATION_VERSION, PROTOCOL_VERSION
from sshpilot.daemon.config_reload import CONFIGURATION_COMMAND_KEY
from sshpilot.daemon.interaction_broker import InteractionBroker
from sshpilot.daemon.session_runtime import SessionRuntime
from sshpilot.daemon.terminal_stream import ReplaySlice

logger = logging.getLogger(__name__)

DAEMON_METHOD_CAPABILITIES = {
    "connections.get": Capability.CONNECTIONS_READ,
    "connections.list": Capability.CONNECTIONS_READ,
    "connections.create": Capability.CONNECTIONS_WRITE,
    "connections.delete": Capability.CONNECTIONS_WRITE,
    "connections.update": Capability.CONNECTIONS_WRITE,
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
    "system.get_capabilities": None,
    "system.handshake": None,
}

DEFERRED_DAEMON_METHODS = frozenset(
    {
        "connections.create",
        "connections.update",
        "connections.delete",
        "sessions.open",
        "sessions.close",
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
    """A daemon-owned blocking operation for the bounded command executor."""

    operation: Callable[[], Any]
    command_key: Hashable
    on_rejected: Callable[[], None]
    session_id: Optional[SessionId] = None
    connection_id: Optional[ConnectionId] = None


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
    ) -> None:
        self._core_client = core_client
        self._session_runtime = session_runtime or SessionRuntime(core_client)
        self._interaction_broker = interaction_broker
        self.server_instance_id = uuid.uuid4().hex
        self._shutting_down = False
        self.HANDLERS: Dict[str, Callable[[RequestEnvelope, ClientProtocolState], Any]] = {
            "system.handshake": self._handle_handshake,
            "system.get_capabilities": self._handle_get_capabilities,
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
        }

    def begin_shutdown(self) -> None:
        self._shutting_down = True

    def dispatch(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> DispatchResult:
        if self._shutting_down:
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
            ),
            compatibility_status="compatible",
            server_instance_id=self.server_instance_id,
        )
        return handshake_result_to_wire(result)

    def _handle_get_capabilities(
        self,
        request: RequestEnvelope,
        state: ClientProtocolState,
    ) -> dict:
        self._require_empty_params(request)
        return capabilities_to_wire(self._capabilities_for(state))

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

        def _start() -> dict:
            self._session_runtime.start_session(prepared.id)
            return prepared_wire

        return DeferredResult(
            operation=_start,
            command_key=prepared.id,
            session_id=prepared.id,
            on_rejected=lambda: self._session_runtime.reject_pending_start(
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
        return daemon_capabilities

    def _required_interaction_broker(self) -> InteractionBroker:
        if self._interaction_broker is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Typed interactions are unavailable",
            )
        return self._interaction_broker

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
