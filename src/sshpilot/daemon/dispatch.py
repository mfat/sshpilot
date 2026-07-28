"""Explicit Protocol v1 daemon method dispatcher."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Optional, Set

from sshpilot import __version__ as sshpilot_version
from sshpilot.api.capabilities import Capabilities, Capability
from sshpilot.api.client import SshPilotClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import (
    ClientId,
    ClientInfo,
    CompatibilityResult,
    ConnectionId,
    CoreInfo,
)
from sshpilot.api.transport.codec import (
    capabilities_to_wire,
    connection_details_to_wire,
    connection_summary_to_wire,
    create_connection_request_from_wire,
    delete_connection_request_from_wire,
    delete_connection_result_to_wire,
    handshake_request_from_wire,
    handshake_result_to_wire,
    update_connection_request_from_wire,
)
from sshpilot.api.transport.envelopes import HandshakeRequest, HandshakeResult, RequestEnvelope
from sshpilot.api.version import API_IMPLEMENTATION_VERSION, PROTOCOL_VERSION

logger = logging.getLogger(__name__)

DAEMON_METHOD_CAPABILITIES = {
    "connections.get": Capability.CONNECTIONS_READ,
    "connections.list": Capability.CONNECTIONS_READ,
    "connections.create": Capability.CONNECTIONS_WRITE,
    "connections.delete": Capability.CONNECTIONS_WRITE,
    "connections.update": Capability.CONNECTIONS_WRITE,
    "system.get_capabilities": None,
    "system.handshake": None,
}


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

    def __init__(self, core_client: SshPilotClient) -> None:
        self._core_client = core_client
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
        }

    def begin_shutdown(self) -> None:
        self._shutting_down = True

    def dispatch(self, request: RequestEnvelope, state: ClientProtocolState) -> Any:
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
            return handler(request, state)
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
            daemon_capabilities=self._safe_capabilities(core_capabilities.supported),
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
    ) -> dict:
        mutation = create_connection_request_from_wire(request.params)
        return connection_details_to_wire(
            self._core_client.create_connection(mutation)
        )

    def _handle_update_connection(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        if set(request.params) != {"connection_id", "update"}:
            raise ValueError(
                "connections.update requires connection_id and update"
            )
        connection_id = request.params["connection_id"]
        if type(connection_id) is not str or not connection_id.strip():
            raise ValueError("connection_id must be a non-empty string")
        mutation = update_connection_request_from_wire(request.params["update"])
        return connection_details_to_wire(
            self._core_client.update_connection(
                ConnectionId(connection_id),
                mutation,
            )
        )

    def _handle_delete_connection(
        self,
        request: RequestEnvelope,
        _state: ClientProtocolState,
    ) -> dict:
        mutation = delete_connection_request_from_wire(request.params)
        return delete_connection_result_to_wire(
            self._core_client.delete_connection(mutation)
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
            supported=self._safe_capabilities(core.supported),
            compatibility=CompatibilityResult(
                compatible=True,
                protocol_version=PROTOCOL_VERSION,
            ),
        )

    @staticmethod
    def _safe_capabilities(
        supported: FrozenSet[Capability],
    ) -> FrozenSet[Capability]:
        # Protocol v1 currently exposes snapshots plus typed connection events.
        return frozenset(
            item
            for item in supported
            if item
            in {
                Capability.CONNECTIONS_READ,
                Capability.CONNECTIONS_EVENTS,
                Capability.CONNECTIONS_WRITE,
            }
        )

    @staticmethod
    def _require_empty_params(request: RequestEnvelope) -> None:
        if request.params:
            raise ValueError("method does not accept parameters")
