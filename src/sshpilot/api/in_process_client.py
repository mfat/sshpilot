"""In-process adapter from the public client contract to existing managers."""

import hashlib
import logging
import threading
from typing import Any, List, Optional, Tuple

from sshpilot import __version__ as sshpilot_version

from .capabilities import Capabilities, Capability
from .errors import ErrorCode, SshPilotError, unsupported_capability
from .events import CoreEventCallback, EventPublisher, EventType, Subscription
from .models.common import (
    ClientInfo,
    CompatibilityResult,
    ConnectionId,
    CoreInfo,
)
from .models.connections import (
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
from .version import API_IMPLEMENTATION_VERSION, PROTOCOL_VERSION

logger = logging.getLogger(__name__)

IMPLEMENTED_CLIENT_METHOD_CAPABILITIES = {
    "close": None,
    "get_capabilities": None,
    "get_connection": Capability.CONNECTIONS_READ,
    "list_connections": Capability.CONNECTIONS_READ,
    "subscribe_events": None,
}

UNSUPPORTED_CLIENT_METHOD_CAPABILITIES = {
    "attach_session": Capability.TERMINAL_ATTACH,
    "close_session": Capability.TERMINAL,
    "create_connection": Capability.CONNECTIONS_WRITE,
    "delete_connection": Capability.CONNECTIONS_WRITE,
    "detach_session": Capability.TERMINAL_ATTACH,
    "open_session": Capability.TERMINAL,
    "replay_terminal": Capability.TERMINAL_REPLAY,
    "resize_terminal": Capability.TERMINAL,
    "respond_to_interaction": Capability.INTERACTIONS,
    "send_terminal_input": Capability.TERMINAL,
    "update_connection": Capability.CONNECTIONS_WRITE,
}


class InProcessClient:
    """Expose existing connection-manager reads through ``SshPilotClient``.

    Command methods must be invoked on the thread that constructed the adapter,
    matching the current GObject manager's GTK-main-thread ownership. Event
    subscription itself is thread-safe. The first active publisher thread
    serially drains concurrent/re-entrant events, so callbacks must marshal when
    that dispatcher is unsuitable for their frontend.
    """

    def __init__(
        self,
        connection_manager: Any,
        *,
        group_manager: Any = None,
        client_name: str = "gtk",
        client_version: str = sshpilot_version,
    ) -> None:
        if connection_manager is None:
            raise ValueError("connection_manager is required")
        self._connection_manager = connection_manager
        self._group_manager = group_manager
        self._owner_thread_id = threading.get_ident()
        self._publisher = EventPublisher()
        self._signal_handlers: List[int] = []
        self._closed = False
        self._capabilities = Capabilities(
            protocol_version=PROTOCOL_VERSION,
            api_implementation_version=API_IMPLEMENTATION_VERSION,
            client=ClientInfo(name=client_name, version=client_version),
            core=CoreInfo(
                name="sshPilot Core",
                version=sshpilot_version,
                implementation="in-process",
            ),
            supported=frozenset({Capability.CONNECTIONS_READ}),
            compatibility=CompatibilityResult(
                compatible=True,
                protocol_version=PROTOCOL_VERSION,
            ),
        )
        self._connect_manager_events()

    def get_capabilities(self) -> Capabilities:
        return self._capabilities

    def list_connections(self) -> List[ConnectionSummary]:
        self._assert_command_thread()
        return [self._to_summary(connection) for connection in self._manager_connections()]

    def get_connection(self, connection_id: ConnectionId) -> ConnectionDetails:
        self._assert_command_thread()
        connection = self._find_connection(connection_id)
        if connection is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        return self._to_details(connection)

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
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "The client is closed")
        try:
            return self._publisher.subscribe(callback)
        except RuntimeError:
            # A concurrent close may win after the optimistic lifecycle check.
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "The client is closed",
            ) from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        disconnect = getattr(self._connection_manager, "disconnect", None)
        if callable(disconnect):
            for handler_id in self._signal_handlers:
                try:
                    disconnect(handler_id)
                except Exception:
                    logger.debug(
                        "Failed to disconnect connection-manager signal %s",
                        handler_id,
                        exc_info=True,
                    )
        self._signal_handlers.clear()
        self._publisher.close()

    @staticmethod
    def connection_id_for(connection: Any) -> ConnectionId:
        """Return the transitional opaque ID for an existing connection.

        Persistence currently has no immutable connection UUID. Protocol v1
        therefore hashes ``protocol + nickname``. The ID is stable across reloads
        but changes on rename; the daemon phase should add persisted UUIDs with a
        migration.
        """

        protocol = str(getattr(connection, "protocol", "ssh") or "ssh")
        nickname = str(getattr(connection, "nickname", "") or "")
        digest = hashlib.sha256(f"{protocol}\0{nickname}".encode()).hexdigest()
        return ConnectionId(f"connection:v1:{digest[:32]}")

    def _assert_command_thread(self) -> None:
        if self._closed:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "The client is closed")
        if threading.get_ident() != self._owner_thread_id:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "In-process client commands must run on their owner thread",
            )

    @staticmethod
    def _unsupported(method_name: str) -> SshPilotError:
        return unsupported_capability(
            UNSUPPORTED_CLIENT_METHOD_CAPABILITIES[method_name]
        )

    def _manager_connections(self) -> List[Any]:
        getter = getattr(self._connection_manager, "get_connections", None)
        try:
            if callable(getter):
                return list(getter())
            return list(getattr(self._connection_manager, "connections", ()) or ())
        except SshPilotError:
            raise
        except Exception:
            logger.exception("Connection manager failed while listing connections")
            raise SshPilotError(
                ErrorCode.INTERNAL_ERROR,
                "Connections could not be loaded",
                retryable=True,
            ) from None

    def _find_connection(self, connection_id: ConnectionId) -> Optional[Any]:
        for connection in self._manager_connections():
            if self.connection_id_for(connection) == connection_id:
                return connection
        return None

    def _group_references(self, connection: Any) -> Tuple[GroupReference, ...]:
        manager = self._group_manager
        if manager is None:
            return ()
        nickname = str(getattr(connection, "nickname", "") or "")
        try:
            group_ids = manager.get_connection_groups(nickname)
        except Exception:
            logger.debug("Failed to resolve groups for %s", nickname, exc_info=True)
            return ()
        references = []
        for group_id in group_ids or ():
            try:
                info = getattr(manager, "groups", {}).get(group_id, {})
                references.append(
                    GroupReference(id=str(group_id), name=str(info.get("name", "") or ""))
                )
            except (TypeError, ValueError):
                logger.debug("Ignoring invalid group reference %r", group_id)
        return tuple(references)

    @staticmethod
    def _port(connection: Any) -> int:
        try:
            port = int(getattr(connection, "port", 22) or 22)
        except (TypeError, ValueError):
            port = 22
        return port if 1 <= port <= 65535 else 22

    def _to_summary(self, connection: Any) -> ConnectionSummary:
        nickname = str(getattr(connection, "nickname", "") or "")
        if not nickname:
            logger.error("Connection manager returned a connection without a nickname")
            raise SshPilotError(
                ErrorCode.INTERNAL_ERROR,
                "A stored connection is invalid",
            )
        return ConnectionSummary(
            id=self.connection_id_for(connection),
            nickname=nickname,
            host=str(getattr(connection, "host", "") or ""),
            hostname=str(getattr(connection, "hostname", "") or ""),
            username=str(getattr(connection, "username", "") or ""),
            port=self._port(connection),
            protocol=str(getattr(connection, "protocol", "ssh") or "ssh"),
            # Existing ConnectionState is terminal-session-derived, not a
            # reachability check. Do not mislabel it as persistent health.
            health=ConnectionHealth.UNKNOWN,
            groups=self._group_references(connection),
        )

    def _to_details(self, connection: Any) -> ConnectionDetails:
        summary = self._to_summary(connection)
        aliases = self._string_tuple(getattr(connection, "aliases", ()))
        proxy_jump = self._string_tuple(getattr(connection, "proxy_jump", ()))
        try:
            auth_method = (
                AuthenticationMethod.PASSWORD
                if int(getattr(connection, "auth_method", 0) or 0) == 1
                else AuthenticationMethod.KEY
            )
        except (TypeError, ValueError):
            auth_method = AuthenticationMethod.KEY
        forwarding_rules = getattr(connection, "forwarding_rules", ()) or ()
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
            authentication_method=auth_method,
            identity_configured=bool(
                getattr(connection, "keyfile", "")
                or getattr(connection, "identity_files", ())
            ),
            certificate_configured=bool(
                getattr(connection, "certificate", "")
                or getattr(connection, "certificate_files", ())
            ),
            x11_forwarding=bool(getattr(connection, "x11_forwarding", False)),
            forwarding_rule_count=len(forwarding_rules),
            proxy_jump=proxy_jump,
        )

    @staticmethod
    def _string_tuple(value: Any) -> Tuple[str, ...]:
        if isinstance(value, str):
            return (value,) if value else ()
        try:
            return tuple(str(item) for item in value if item)
        except TypeError:
            return ()

    def _connect_manager_events(self) -> None:
        connect = getattr(self._connection_manager, "connect", None)
        if not callable(connect):
            return
        for signal_name, callback in (
            ("connection-added", self._on_connection_added),
            ("connection-updated", self._on_connection_updated),
            ("connection-removed", self._on_connection_removed),
        ):
            try:
                handler_id = connect(signal_name, callback)
                if handler_id is not None:
                    self._signal_handlers.append(handler_id)
            except Exception:
                logger.debug(
                    "Connection manager does not expose %s",
                    signal_name,
                    exc_info=True,
                )

    def _publish_connection_event(
        self,
        event_type: EventType,
        connection: Any,
    ) -> None:
        if self._closed:
            return
        try:
            summary = self._to_summary(connection)
            self._publisher.publish(
                event_type,
                summary,
                connection_id=summary.id,
            )
        except Exception:
            logger.exception("Failed to adapt %s connection event", event_type.value)

    def _on_connection_added(self, _manager: Any, connection: Any) -> None:
        self._publish_connection_event(EventType.CONNECTION_CREATED, connection)

    def _on_connection_updated(self, _manager: Any, connection: Any) -> None:
        self._publish_connection_event(EventType.CONNECTION_UPDATED, connection)

    def _on_connection_removed(self, _manager: Any, connection: Any) -> None:
        self._publish_connection_event(EventType.CONNECTION_DELETED, connection)
