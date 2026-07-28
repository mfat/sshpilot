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
    "create_connection": Capability.CONNECTIONS_WRITE,
    "delete_connection": Capability.CONNECTIONS_WRITE,
    "subscribe_events": Capability.CONNECTIONS_EVENTS,
    "update_connection": Capability.CONNECTIONS_WRITE,
}

UNSUPPORTED_CLIENT_METHOD_CAPABILITIES = {
    "attach_session": Capability.TERMINAL_ATTACH,
    "close_session": Capability.TERMINAL,
    "detach_session": Capability.TERMINAL_ATTACH,
    "open_session": Capability.TERMINAL,
    "replay_terminal": Capability.TERMINAL_REPLAY,
    "resize_terminal": Capability.TERMINAL,
    "respond_to_interaction": Capability.INTERACTIONS,
    "send_terminal_input": Capability.TERMINAL,
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
        supported = {
            Capability.CONNECTIONS_READ,
            Capability.CONNECTIONS_EVENTS,
        }
        if all(
            callable(getattr(connection_manager, method_name, None))
            for method_name in (
                "create_connection",
                "update_connection",
                "remove_connection",
            )
        ):
            supported.add(Capability.CONNECTIONS_WRITE)
        self._capabilities = Capabilities(
            protocol_version=PROTOCOL_VERSION,
            api_implementation_version=API_IMPLEMENTATION_VERSION,
            client=ClientInfo(name=client_name, version=client_version),
            core=CoreInfo(
                name="sshPilot Core",
                version=sshpilot_version,
                implementation="in-process",
            ),
            supported=frozenset(supported),
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
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_WRITE)
        if type(request) is not CreateConnectionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A create connection request is required",
            )
        if request.protocol != "ssh":
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The requested connection protocol is not supported",
                details={"field": "protocol"},
            )
        if self._connection_with_nickname(request.nickname) is not None:
            raise SshPilotError(
                ErrorCode.CONNECTION_ALREADY_EXISTS,
                "A connection with this nickname already exists",
            )
        creator = getattr(self._connection_manager, "create_connection", None)
        if not callable(creator):
            raise self._persistence_error()
        data = {
            "nickname": request.nickname,
            "hostname": request.hostname,
            "username": request.username,
            "port": request.port,
            "protocol": request.protocol,
        }
        try:
            connection = creator(data)
        except ValueError:
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The connection data is invalid",
            ) from None
        except SshPilotError:
            raise
        except Exception as error:
            logger.error(
                "Connection manager create failed (%s)",
                type(error).__name__,
            )
            raise self._persistence_error() from None
        if connection is None:
            raise self._persistence_error()
        return self._to_details(connection)

    def update_connection(
        self,
        connection_id: ConnectionId,
        request: UpdateConnectionRequest,
    ) -> ConnectionDetails:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_WRITE)
        if type(request) is not UpdateConnectionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "An update connection request is required",
                connection_id=connection_id,
            )
        connection = self._find_connection(connection_id)
        if connection is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        if getattr(connection, "protocol", "ssh") != "ssh":
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The requested connection protocol is not supported",
                connection_id=connection_id,
            )
        old_nickname = str(getattr(connection, "nickname", "") or "")
        if (
            request.nickname is not None
            and request.nickname != old_nickname
            and self._connection_with_nickname(request.nickname) is not None
        ):
            raise SshPilotError(
                ErrorCode.CONNECTION_ALREADY_EXISTS,
                "A connection with this nickname already exists",
                connection_id=connection_id,
            )

        data = self._safe_internal_update_data(connection)
        for name in ("nickname", "hostname", "username", "port"):
            value = getattr(request, name)
            if value is not None:
                data[name] = value
        updater = getattr(self._connection_manager, "update_connection", None)
        if not callable(updater):
            raise self._persistence_error(connection_id)
        try:
            updated = updater(connection, data, emit_signal=False)
        except SshPilotError:
            raise
        except Exception as error:
            logger.error(
                "Connection manager update failed (%s)",
                type(error).__name__,
            )
            raise self._persistence_error(connection_id) from None
        if not updated:
            raise self._persistence_error(connection_id)

        new_nickname = str(getattr(connection, "nickname", "") or "")
        if old_nickname != new_nickname and self._group_manager is not None:
            rename = getattr(self._group_manager, "rename_connection", None)
            if callable(rename):
                try:
                    rename(old_nickname, new_nickname)
                except Exception as error:
                    logger.error(
                        "Connection group rename failed (%s)",
                        type(error).__name__,
                    )
        self._emit_manager_event("connection-updated", connection)
        return self._to_details(connection)

    def delete_connection(self, request: DeleteConnectionRequest) -> DeleteConnectionResult:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_WRITE)
        if type(request) is not DeleteConnectionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A delete connection request is required",
            )
        connection = self._find_connection(request.connection_id)
        if connection is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=request.connection_id,
            )
        remover = getattr(self._connection_manager, "remove_connection", None)
        if not callable(remover):
            raise self._persistence_error(request.connection_id)
        try:
            deleted = remover(connection)
        except SshPilotError:
            raise
        except Exception as error:
            logger.error(
                "Connection manager delete failed (%s)",
                type(error).__name__,
            )
            raise self._persistence_error(request.connection_id) from None
        if not deleted:
            raise self._persistence_error(request.connection_id)
        return DeleteConnectionResult(
            connection_id=request.connection_id,
            deleted=True,
        )

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

    def _require_capability(self, capability: Capability) -> None:
        if not self._capabilities.supports(capability):
            raise unsupported_capability(capability)

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
        except Exception as error:
            logger.error(
                "Connection manager failed while listing connections (%s)",
                type(error).__name__,
            )
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

    def _connection_with_nickname(self, nickname: str) -> Optional[Any]:
        finder = getattr(
            self._connection_manager,
            "find_connection_by_nickname",
            None,
        )
        if callable(finder):
            try:
                found = finder(nickname)
                if found is not None:
                    return found
            except Exception as error:
                logger.error(
                    "Connection manager nickname lookup failed (%s)",
                    type(error).__name__,
                )
                raise SshPilotError(
                    ErrorCode.INTERNAL_ERROR,
                    "Connections could not be checked",
                    retryable=True,
                ) from None
        folded = nickname.casefold()
        return next(
            (
                connection
                for connection in self._manager_connections()
                if str(getattr(connection, "nickname", "") or "").casefold()
                == folded
            ),
            None,
        )

    @staticmethod
    def _safe_internal_update_data(connection: Any) -> dict:
        """Copy persisted metadata without carrying secret/control values."""

        current = getattr(connection, "data", None)
        data = dict(current) if isinstance(current, dict) else {}
        for key in tuple(data):
            lowered = str(key).lower()
            if (
                str(key).startswith("__")
                or "password" in lowered
                or "passphrase" in lowered
                or "secret" in lowered
                or "token" in lowered
                or "credential" in lowered
                or "cookie" in lowered
                or "private_key" in lowered
                or callable(data[key])
            ):
                data.pop(key, None)
        data.update(
            {
                "nickname": str(getattr(connection, "nickname", "") or ""),
                "hostname": str(getattr(connection, "hostname", "") or ""),
                "username": str(getattr(connection, "username", "") or ""),
                "port": InProcessClient._port(connection),
                "protocol": str(getattr(connection, "protocol", "ssh") or "ssh"),
            }
        )
        return data

    def _emit_manager_event(self, signal_name: str, connection: Any) -> None:
        emit = getattr(self._connection_manager, "emit", None)
        if not callable(emit):
            raise self._persistence_error(self.connection_id_for(connection))
        try:
            emit(signal_name, connection)
        except Exception as error:
            logger.error(
                "Connection manager event emission failed (%s)",
                type(error).__name__,
            )
            raise SshPilotError(
                ErrorCode.INTERNAL_ERROR,
                "The connection changed but its event could not be published",
                connection_id=self.connection_id_for(connection),
            ) from None

    @staticmethod
    def _persistence_error(
        connection_id: Optional[ConnectionId] = None,
    ) -> SshPilotError:
        return SshPilotError(
            ErrorCode.PERSISTENCE_FAILED,
            "The connection change could not be saved",
            connection_id=connection_id,
        )

    def _group_references(self, connection: Any) -> Tuple[GroupReference, ...]:
        manager = self._group_manager
        if manager is None:
            return ()
        nickname = str(getattr(connection, "nickname", "") or "")
        try:
            group_ids = manager.get_connection_groups(nickname)
        except Exception as error:
            logger.debug(
                "Failed to resolve groups for %s (%s)",
                nickname,
                type(error).__name__,
            )
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
