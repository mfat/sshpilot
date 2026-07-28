"""Synchronous Protocol v1 client for the local sshPilot daemon."""

from __future__ import annotations

import collections
import os
import socket
import threading
import uuid
from pathlib import Path
from typing import Deque, List, NoReturn, Optional

from sshpilot import __version__ as sshpilot_version

from .capabilities import Capabilities
from .errors import ErrorCode, SshPilotError, unsupported_capability
from .events import CoreEventCallback, EventPublisher, Subscription
from .in_process_client import UNSUPPORTED_CLIENT_METHOD_CAPABILITIES
from .models.common import ClientId, ConnectionId, RequestId
from .models.connections import (
    ConnectionDetails,
    ConnectionSummary,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    UpdateConnectionRequest,
)
from .models.interactions import InteractionResponse
from .models.sessions import (
    AttachSessionRequest,
    AttachSessionResult,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionSummary,
)
from .models.terminal import (
    ReplayRequest,
    ReplayResult,
    ResizeTerminalRequest,
    TerminalInput,
)
from .transport.codec import (
    capabilities_from_wire,
    connection_details_from_wire,
    connection_summary_from_wire,
    decode_envelope,
    encode_envelope,
    error_from_wire,
    handshake_request_to_wire,
    handshake_result_from_wire,
)
from .transport.envelopes import (
    ErrorResponseEnvelope,
    EventEnvelope,
    HandshakeRequest,
    RequestEnvelope,
    SuccessResponseEnvelope,
)
from .transport.framing import FramingError, encode_frame, receive_frame
from .version import PROTOCOL_VERSION

DEFAULT_REQUEST_TIMEOUT = 5.0


class DaemonClient:
    """One-at-a-time synchronous RPC client over one persistent Unix socket.

    A lock serialises request/response pairs. Calls have a finite timeout; a
    timeout or protocol failure closes the transport so late responses cannot
    be mistaken for later requests. Phase 1 receives and validates unsolicited
    event envelopes while awaiting responses, but the daemon does not emit
    runtime events yet.
    """

    def __init__(
        self,
        *,
        socket_path: Optional[os.PathLike] = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        client_name: str = "daemon-client",
        client_version: str = sshpilot_version,
        client_id: Optional[str] = None,
        frontend_type: Optional[str] = None,
    ) -> None:
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("daemon request timeout must be positive")
        self._timeout = float(timeout)
        self._socket_path = (
            Path(socket_path) if socket_path is not None else self.default_socket_path()
        )
        self._client_id = ClientId(client_id or f"client:{uuid.uuid4().hex}")
        self._client_name = client_name
        self._client_version = client_version
        self._frontend_type = frontend_type
        self._request_lock = threading.Lock()
        self._publisher = EventPublisher()
        self._pending_event_envelopes: Deque[EventEnvelope] = collections.deque(maxlen=100)
        self._last_event_sequence: Optional[int] = None
        self._closed = False
        self._transport_failed = False
        self._socket: Optional[socket.socket] = None
        self._capabilities: Optional[Capabilities] = None
        self._selected_protocol_version: Optional[str] = None
        self._server_instance_id: Optional[str] = None
        try:
            self._connect_and_handshake()
        except BaseException:
            self._closed = True
            self._close_socket()
            self._publisher.close()
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

    def create_connection(self, request: CreateConnectionRequest) -> ConnectionDetails:
        del request
        raise self._unsupported("create_connection")

    def update_connection(
        self,
        connection_id: ConnectionId,
        request: UpdateConnectionRequest,
    ) -> ConnectionDetails:
        del connection_id, request
        raise self._unsupported("update_connection")

    def delete_connection(self, request: DeleteConnectionRequest) -> DeleteConnectionResult:
        del request
        raise self._unsupported("delete_connection")

    def open_session(self, request: OpenSessionRequest) -> SessionSummary:
        del request
        raise self._unsupported("open_session")

    def attach_session(self, request: AttachSessionRequest) -> AttachSessionResult:
        del request
        raise self._unsupported("attach_session")

    def detach_session(self, request: DetachSessionRequest) -> None:
        del request
        raise self._unsupported("detach_session")

    def close_session(self, request: CloseSessionRequest) -> None:
        del request
        raise self._unsupported("close_session")

    def send_terminal_input(self, request: TerminalInput) -> None:
        del request
        raise self._unsupported("send_terminal_input")

    def resize_terminal(self, request: ResizeTerminalRequest) -> None:
        del request
        raise self._unsupported("resize_terminal")

    def replay_terminal(self, request: ReplayRequest) -> ReplayResult:
        del request
        raise self._unsupported("replay_terminal")

    def respond_to_interaction(self, response: InteractionResponse) -> None:
        del response
        raise self._unsupported("respond_to_interaction")

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        if self._closed:
            raise self._closed_error()
        try:
            return self._publisher.subscribe(callback)
        except RuntimeError:
            raise self._closed_error() from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_socket()
        self._publisher.close()
        self._pending_event_envelopes.clear()

    def _connect_and_handshake(self) -> None:
        transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        transport.settimeout(self._timeout)
        try:
            transport.connect(str(self._socket_path))
        except (OSError, socket.timeout):
            transport.close()
            raise SshPilotError(
                ErrorCode.DAEMON_UNAVAILABLE,
                "The local sshPilot daemon is unavailable",
                retryable=True,
            ) from None
        self._socket = transport
        request = HandshakeRequest(
            client_name=self._client_name,
            client_version=self._client_version,
            supported_protocol_versions=(PROTOCOL_VERSION,),
            client_capabilities=frozenset(),
            frontend_type=self._frontend_type,
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
        capabilities = self._request("system.get_capabilities", {})
        try:
            self._capabilities = capabilities_from_wire(capabilities)
        except (TypeError, ValueError):
            self._fail_protocol("The daemon returned invalid capabilities")

    def _request(
        self,
        method: str,
        params: dict,
        *,
        protocol_version: Optional[str] = None,
    ):
        if self._closed:
            raise self._closed_error()
        with self._request_lock:
            if self._closed:
                raise self._closed_error()
            transport = self._socket
            if transport is None:
                raise self._closed_error()
            request_id = RequestId(uuid.uuid4().hex)
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
            try:
                transport.sendall(encode_frame(encode_envelope(request)))
                while True:
                    response = decode_envelope(receive_frame(transport))
                    if isinstance(response, EventEnvelope):
                        self._accept_event_envelope(response)
                        continue
                    if not isinstance(
                        response,
                        (SuccessResponseEnvelope, ErrorResponseEnvelope),
                    ):
                        self._fail_protocol(
                            "The daemon sent an unexpected envelope type"
                        )
                    expected_version = (
                        self._selected_protocol_version or PROTOCOL_VERSION
                    )
                    if response.protocol_version != expected_version:
                        self._fail_protocol(
                            "The daemon response uses an unexpected protocol"
                        )
                    if response.request_id != request_id:
                        self._fail_protocol(
                            "The daemon response has an unknown request ID"
                        )
                    if isinstance(response, ErrorResponseEnvelope):
                        raise error_from_wire(response.error)
                    return response.result
            except SshPilotError:
                raise
            except socket.timeout:
                self._transport_failed = True
                self._close_socket()
                raise SshPilotError(
                    ErrorCode.TRANSPORT_TIMEOUT,
                    "The daemon request timed out",
                    retryable=True,
                    request_id=request_id,
                ) from None
            except EOFError:
                self._transport_failed = True
                self._close_socket()
                raise SshPilotError(
                    ErrorCode.TRANSPORT_CLOSED,
                    "The daemon transport closed unexpectedly",
                    retryable=True,
                    request_id=request_id,
                ) from None
            except FramingError:
                self._transport_failed = True
                self._close_socket()
                raise SshPilotError(
                    ErrorCode.PROTOCOL_ERROR,
                    "The daemon sent an invalid transport frame",
                    request_id=request_id,
                ) from None
            except (TypeError, ValueError):
                self._transport_failed = True
                self._close_socket()
                raise SshPilotError(
                    ErrorCode.PROTOCOL_ERROR,
                    "The daemon sent an invalid protocol envelope",
                    request_id=request_id,
                ) from None
            except OSError:
                self._transport_failed = True
                self._close_socket()
                raise SshPilotError(
                    ErrorCode.TRANSPORT_CLOSED,
                    "The daemon transport failed",
                    retryable=True,
                    request_id=request_id,
                ) from None

    def _accept_event_envelope(self, event: EventEnvelope) -> None:
        if (
            event.protocol_version
            != (self._selected_protocol_version or PROTOCOL_VERSION)
        ):
            self._fail_protocol("The daemon event uses an unexpected protocol")
        if (
            self._last_event_sequence is not None
            and event.sequence <= self._last_event_sequence
        ):
            self._fail_protocol("The daemon event sequence is not monotonic")
        self._last_event_sequence = event.sequence
        self._pending_event_envelopes.append(event)

    def _fail_protocol(self, message: str) -> NoReturn:
        self._transport_failed = True
        self._close_socket()
        raise SshPilotError(ErrorCode.PROTOCOL_ERROR, message)

    def _closed_error(self) -> SshPilotError:
        if self._transport_failed:
            return SshPilotError(
                ErrorCode.TRANSPORT_CLOSED,
                "The daemon transport is closed",
                retryable=True,
            )
        return SshPilotError(ErrorCode.INVALID_REQUEST, "The client is closed")

    def _close_socket(self) -> None:
        transport = self._socket
        self._socket = None
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            transport.close()

    @staticmethod
    def _unsupported(method_name: str) -> SshPilotError:
        return unsupported_capability(
            UNSUPPORTED_CLIENT_METHOD_CAPABILITIES[method_name]
        )
