"""In-process adapter from the public client contract to existing managers."""

import logging
import threading
from typing import Any, Iterable, List, Optional, Tuple

from sshpilot import __version__ as sshpilot_version

from .capabilities import Capabilities, Capability
from .errors import ErrorCode, SshPilotError, unsupported_capability
from .events import CoreEventCallback, EventPublisher, EventType, Subscription
from .models.common import (
    ClientInfo,
    CompatibilityResult,
    ConnectionId,
    CoreInfo,
    ForwardId,
    SessionId,
    SftpServiceId,
    TransferId,
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
    "replay_terminal": Capability.TERMINAL_REPLAY,
    "resize_terminal": Capability.TERMINAL_RESIZE,
    "release_interaction": Capability.INTERACTIONS_RESPOND,
    "respond_to_interaction": Capability.INTERACTIONS_RESPOND,
    "restart_daemon": Capability.DAEMON_CONTROL,
    "send_interaction_secret": Capability.INTERACTIONS_RESPOND,
    "send_terminal_input": Capability.TERMINAL_INPUT,
    "stop_daemon": Capability.DAEMON_CONTROL,
    "subscribe_terminal": Capability.TERMINAL_OUTPUT,
    "list_sftp_services": Capability.SFTP_READ,
    "get_sftp_service": Capability.SFTP_READ,
    "open_sftp": Capability.SFTP_WRITE,
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
    "claim_forward": Capability.FORWARDS_WRITE,
    "close_forward": Capability.FORWARDS_WRITE,
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
        allow_cross_thread_commands: bool = False,
    ) -> None:
        if connection_manager is None:
            raise ValueError("connection_manager is required")
        self._connection_manager = connection_manager
        self._group_manager = group_manager
        self._owner_thread_id = threading.get_ident()
        self._allow_cross_thread_commands = bool(allow_cross_thread_commands)
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

    def prepare_daemon_terminal_launch(
        self,
        connection_id: ConnectionId,
        *,
        interaction_policy: str = "none",
    ) -> tuple:
        """Internal daemon launch hook using the canonical native SSH path."""

        import asyncio
        import shutil

        connection = self._find_connection(connection_id)
        prepared = asyncio.run(
            connection.native_connect(interaction_policy=interaction_policy)
        )
        command = getattr(connection, "ssh_connection_cmd", None)
        if not prepared or command is None:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH session could not be prepared",
                connection_id=connection_id,
            )
        # Classic VTE preload unlocks stored-passphrase keys in ssh-agent
        # (including gnome-keyring/gcr identities that advertise locked keys).
        # Broker policy strips in-process askpass, so without this OpenSSH falls
        # back to the on-disk encrypted key and the user gets a passphrase dialog.
        self._preload_connection_keys(connection)
        argv = tuple(getattr(command, "command", ()) or ())
        environment = dict(getattr(command, "env", {}) or {})
        if (
            not argv
            or getattr(command, "use_askpass", False)
            or environment.get("SSH_ASKPASS")
            or environment.get("SSH_ASKPASS_REQUIRE")
        ):
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH session requires unsupported interaction",
                connection_id=connection_id,
            )
        executable = shutil.which(argv[0], path=environment.get("PATH"))
        if executable is None:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The OpenSSH executable is unavailable",
                connection_id=connection_id,
            )
        return (executable, *argv[1:]), environment

    @staticmethod
    def _preload_connection_keys(connection) -> None:
        """Best-effort agent preload; never raises (mirrors classic VTE path)."""

        preload = getattr(connection, "_preload_keys_into_agent", None)
        if not callable(preload):
            return
        try:
            preload()
        except Exception:
            logger.debug(
                "daemon key preload failed connection=%s",
                getattr(connection, "nickname", None),
                exc_info=True,
            )

    def prepare_daemon_sftp_launch(
        self,
        connection_id: ConnectionId,
        *,
        interaction_policy: str = "broker",
    ) -> tuple:
        """Internal daemon launch hook: ``ssh -s <host>`` (sftp subsystem).

        Mirrors ``prepare_daemon_terminal_launch`` but requests the SFTP
        subsystem instead of an interactive shell. Built directly from
        ``ConnectionContext`` (the same one-off pattern the in-app SFTP file
        manager already uses for ``ssh -s sftp`` — see
        ``OpenSSHSFTPManager._build_argv``) rather than ``native_connect``,
        since a subsystem request is not a login shell. The daemon appends
        the ``sftp`` remote-command argument after host-key pinning (see
        ``InteractionBroker.prepare_launch``'s ``trailing_args``).
        """

        import shutil

        from ..ssh_connection_builder import ConnectionContext, build_ssh_connection

        connection = self._find_connection(connection_id)
        if connection is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        try:
            from ..config import Config

            app_config = Config()
        except Exception:
            app_config = None
        try:
            connection.resolved_identity_files = (
                connection.collect_identity_file_candidates()
            )
        except Exception:
            connection.resolved_identity_files = []
        ctx = ConnectionContext(
            connection=connection,
            connection_manager=self._connection_manager,
            config=app_config,
            command_type="sftp",
            native_mode=True,
            extra_args=["-s"],
            interaction_policy=interaction_policy,
        )
        prepared = build_ssh_connection(ctx)
        self._preload_connection_keys(connection)
        argv = tuple(prepared.command)
        environment = dict(prepared.env)
        if not argv:
            raise SshPilotError(
                ErrorCode.SFTP_SERVICE_NOT_READY,
                "The SFTP session could not be prepared",
                connection_id=connection_id,
            )
        executable = shutil.which(argv[0], path=environment.get("PATH"))
        if executable is None:
            raise SshPilotError(
                ErrorCode.SFTP_SERVICE_NOT_READY,
                "The OpenSSH executable is unavailable",
                connection_id=connection_id,
            )
        return (executable, *argv[1:]), environment

    def prepare_daemon_forward_launch(
        self,
        connection_id: ConnectionId,
        *,
        forward_type: str,
        bind_host: str,
        bind_port: int,
        destination_host: Optional[str],
        destination_port: Optional[int],
        interaction_policy: str = "broker",
    ) -> tuple:
        """Internal daemon launch hook: dedicated ``ssh -N -L/-R/-D`` process.

        Built directly from ``ConnectionContext`` (one-off ``extra_args``,
        the same pattern ``native_connect`` uses for ``force_tty``'s ``-t``)
        so the ad-hoc forward rule never touches the persisted
        ``~/.ssh/config`` for this connection.
        """

        import shutil

        from ..ssh_connection_builder import ConnectionContext, build_ssh_connection

        connection = self._find_connection(connection_id)
        if connection is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        if forward_type == "local":
            rule = f"{bind_host}:{bind_port}:{destination_host}:{destination_port}"
            forward_flag = "-L"
        elif forward_type == "remote":
            rule = f"{bind_host}:{bind_port}:{destination_host}:{destination_port}"
            forward_flag = "-R"
        elif forward_type == "dynamic":
            rule = f"{bind_host}:{bind_port}" if bind_host else str(bind_port)
            forward_flag = "-D"
        else:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "The requested forward type is not supported",
                connection_id=connection_id,
            )
        try:
            from ..config import Config

            app_config = Config()
        except Exception:
            app_config = None
        try:
            connection.resolved_identity_files = (
                connection.collect_identity_file_candidates()
            )
        except Exception:
            connection.resolved_identity_files = []
        ctx = ConnectionContext(
            connection=connection,
            connection_manager=self._connection_manager,
            config=app_config,
            command_type="ssh",
            native_mode=True,
            extra_args=[
                "-N",
                "-T",
                forward_flag,
                rule,
                "-o",
                "ExitOnForwardFailure=yes",
                # Note: do not set ClearAllForwardings=yes here. OpenSSH treats
                # that option as a sticky clear of *all* forwards (including
                # subsequent -L/-R/-D on the same argv), which would leave the
                # dedicated forward process with no listener.
            ],
            interaction_policy=interaction_policy,
        )
        prepared = build_ssh_connection(ctx)
        argv = list(prepared.command)
        environment = dict(prepared.env)
        if not argv:
            raise SshPilotError(
                ErrorCode.FORWARD_STARTUP_FAILED,
                "The forward could not be prepared",
                connection_id=connection_id,
            )
        # OpenSSH's ClearAllForwardings=yes also wipes subsequent -L/-R/-D on
        # the same argv. Isolate instead: launch against a temp config that
        # omits config-static Local/Remote/Dynamic forwards so only the
        # daemon-owned ad-hoc rule applies.
        argv = self._argv_with_forward_isolated_config(argv)
        executable = shutil.which(argv[0], path=environment.get("PATH"))
        if executable is None:
            raise SshPilotError(
                ErrorCode.FORWARD_STARTUP_FAILED,
                "The OpenSSH executable is unavailable",
                connection_id=connection_id,
            )
        return (executable, *argv[1:]), environment

    @staticmethod
    def _argv_with_forward_isolated_config(argv: list) -> list:
        """Rewrite ``-F <config>`` to a sibling file without *Forward lines."""

        try:
            flag_index = argv.index("-F")
        except ValueError:
            return argv
        if flag_index + 1 >= len(argv):
            return argv
        from pathlib import Path

        source = Path(argv[flag_index + 1])
        if not source.is_file():
            return argv
        try:
            original = source.read_text(encoding="utf-8")
        except OSError:
            return argv
        _FORWARD_PREFIXES = (
            "localforward",
            "remoteforward",
            "dynamicforward",
        )
        kept: list[str] = []
        stripped = False
        for line in original.splitlines():
            stripped_line = line.lstrip()
            if stripped_line.startswith("#") or not stripped_line:
                kept.append(line)
                continue
            key = stripped_line.split(None, 1)[0].lower()
            if key in _FORWARD_PREFIXES:
                stripped = True
                continue
            kept.append(line)
        if not stripped:
            return argv
        isolated = source.with_name(f"{source.name}.sshpilot-forward-isolated")
        try:
            isolated.write_text("\n".join(kept) + "\n", encoding="utf-8")
            isolated.chmod(0o600)
        except OSError:
            return argv
        rewritten = list(argv)
        rewritten[flag_index + 1] = str(isolated)
        return rewritten

    def lookup_daemon_password(self, connection_id: ConnectionId) -> Optional[str]:
        connection = self._find_connection(connection_id)
        return self._connection_manager.get_connection_password(connection)

    def store_daemon_password(
        self,
        connection_id: ConnectionId,
        password: str,
    ) -> bool:
        connection = self._find_connection(connection_id)
        return bool(
            self._connection_manager.store_connection_password(
                connection,
                password,
            )
        )

    def lookup_daemon_passphrase(self, key_path: str) -> Optional[str]:
        return self._connection_manager.get_key_passphrase(key_path)

    def store_daemon_passphrase(self, key_path: str, passphrase: str) -> bool:
        return bool(
            self._connection_manager.store_key_passphrase(key_path, passphrase)
        )

    def enable_serialized_command_threads(self) -> None:
        """Allow daemon-owned serialized workers to invoke this adapter."""

        self._allow_cross_thread_commands = True

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
        if self._group_manager is not None:
            forget = getattr(self._group_manager, "forget_connection", None)
            if callable(forget):
                try:
                    forget(connection)
                except Exception as error:
                    logger.error(
                        "Connection group cleanup failed (%s)",
                        type(error).__name__,
                    )
        return DeleteConnectionResult(
            connection_id=self.connection_id_for(connection),
            deleted=True,
        )

    def open_session(self, request: OpenSessionRequest) -> SessionSummary:
        del request
        raise self._unsupported("open_session")

    def list_sessions(self) -> List[SessionSummary]:
        raise self._unsupported("list_sessions")

    def get_daemon_status(self):
        raise self._unsupported("get_daemon_status")

    def get_daemon_diagnostics(self):
        raise self._unsupported("get_daemon_diagnostics")

    def stop_daemon(self, request=None):
        del request
        raise self._unsupported("stop_daemon")

    def restart_daemon(self, request=None):
        del request
        raise self._unsupported("restart_daemon")

    def get_session(self, session_id: SessionId) -> SessionSummary:
        del session_id
        raise self._unsupported("get_session")

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

    def claim_terminal_input(self, request: ClaimTerminalInputRequest) -> None:
        del request
        raise self._unsupported("claim_terminal_input")

    def release_terminal_input(self, request: ReleaseTerminalInputRequest) -> None:
        del request
        raise self._unsupported("release_terminal_input")

    def replay_terminal(self, request: ReplayRequest) -> ReplayResult:
        del request
        raise self._unsupported("replay_terminal")

    def subscribe_terminal(
        self,
        session_id: SessionId,
        on_output: TerminalOutputCallback,
        *,
        on_continuity_lost: Optional[TerminalContinuityCallback] = None,
        on_eof: Optional[TerminalEofCallback] = None,
        on_error: Optional[TerminalErrorCallback] = None,
    ) -> TerminalSubscription:
        del session_id, on_output, on_continuity_lost, on_eof, on_error
        raise self._unsupported("subscribe_terminal")

    def list_interactions(self) -> List[InteractionSummary]:
        raise self._unsupported("list_interactions")

    def get_interaction(self, interaction_id: InteractionId) -> InteractionSummary:
        del interaction_id
        raise self._unsupported("get_interaction")

    def claim_interaction(self, interaction_id: InteractionId) -> InteractionClaim:
        del interaction_id
        raise self._unsupported("claim_interaction")

    def release_interaction(self, interaction_id: InteractionId) -> None:
        del interaction_id
        raise self._unsupported("release_interaction")

    def respond_to_interaction(
        self,
        response: InteractionDecisionRequest,
    ) -> None:
        del response
        raise self._unsupported("respond_to_interaction")

    def cancel_interaction(self, interaction_id: InteractionId) -> None:
        del interaction_id
        raise self._unsupported("cancel_interaction")

    def send_interaction_secret(
        self,
        interaction_id: InteractionId,
        nonce: str,
        secret: bytearray,
    ) -> None:
        del interaction_id, nonce, secret
        raise self._unsupported("send_interaction_secret")

    def list_sftp_services(self) -> List[SftpServiceSummary]:
        raise self._unsupported("list_sftp_services")

    def get_sftp_service(self, service_id: SftpServiceId) -> SftpServiceSummary:
        del service_id
        raise self._unsupported("get_sftp_service")

    def open_sftp(self, request: OpenSftpRequest) -> SftpServiceSummary:
        del request
        raise self._unsupported("open_sftp")

    def attach_sftp(self, request: AttachSftpRequest) -> SftpServiceSummary:
        del request
        raise self._unsupported("attach_sftp")

    def detach_sftp(self, service_id: SftpServiceId) -> None:
        del service_id
        raise self._unsupported("detach_sftp")

    def close_sftp(self, request: CloseSftpRequest) -> None:
        del request
        raise self._unsupported("close_sftp")

    def sftp_list_directory(self, request: ListDirectoryRequest) -> ListDirectoryResult:
        del request
        raise self._unsupported("sftp_list_directory")

    def sftp_stat(self, request: SftpPathRequest) -> RemoteFileEntry:
        del request
        raise self._unsupported("sftp_stat")

    def sftp_lstat(self, request: SftpPathRequest) -> RemoteFileEntry:
        del request
        raise self._unsupported("sftp_lstat")

    def sftp_realpath(self, request: SftpPathRequest) -> str:
        del request
        raise self._unsupported("sftp_realpath")

    def sftp_readlink(self, request: SftpPathRequest) -> str:
        del request
        raise self._unsupported("sftp_readlink")

    def sftp_mkdir(self, request: SftpPathRequest) -> None:
        del request
        raise self._unsupported("sftp_mkdir")

    def sftp_rmdir(self, request: SftpPathRequest) -> None:
        del request
        raise self._unsupported("sftp_rmdir")

    def sftp_remove(self, request: SftpPathRequest) -> None:
        del request
        raise self._unsupported("sftp_remove")

    def sftp_rename(self, request: SftpRenameRequest) -> None:
        del request
        raise self._unsupported("sftp_rename")

    def sftp_chmod(self, request: SftpChmodRequest) -> None:
        del request
        raise self._unsupported("sftp_chmod")

    def sftp_symlink(self, request: SftpSymlinkRequest) -> None:
        del request
        raise self._unsupported("sftp_symlink")

    def list_transfers(self) -> List[TransferSummary]:
        raise self._unsupported("list_transfers")

    def get_transfer(self, transfer_id: TransferId) -> TransferSummary:
        del transfer_id
        raise self._unsupported("get_transfer")

    def start_transfer(self, request: StartTransferRequest) -> TransferSummary:
        del request
        raise self._unsupported("start_transfer")

    def cancel_transfer(self, request: CancelTransferRequest) -> None:
        del request
        raise self._unsupported("cancel_transfer")

    def list_forwards(self) -> List[ForwardSummary]:
        raise self._unsupported("list_forwards")

    def get_forward(self, forward_id: ForwardId) -> ForwardSummary:
        del forward_id
        raise self._unsupported("get_forward")

    def open_forward(self, request: OpenForwardRequest) -> ForwardSummary:
        del request
        raise self._unsupported("open_forward")

    def claim_forward(self, request: ClaimForwardRequest) -> ForwardSummary:
        del request
        raise self._unsupported("claim_forward")

    def close_forward(self, request: CloseForwardRequest) -> None:
        del request
        raise self._unsupported("close_forward")

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
        """Return the stable opaque ID equal to the SSH Host alias."""

        nick = str(getattr(connection, "nickname", None) or getattr(connection, "id", None) or "").strip()
        if not nick:
            raise SshPilotError(
                ErrorCode.INTERNAL_ERROR,
                "A stored connection has no durable identity",
            )
        return ConnectionId(nick)

    def _assert_command_thread(self) -> None:
        if self._closed:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "The client is closed")
        if (
            not self._allow_cross_thread_commands
            and threading.get_ident() != self._owner_thread_id
        ):
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "In-process client commands must run on their owner thread",
            )

    def snapshot_connection_summaries(self) -> Tuple[ConnectionSummary, ...]:
        """Return an immutable concrete-adapter snapshot for daemon reloads."""

        if self._closed:
            return ()
        return tuple(
            self._to_summary(connection)
            for connection in self._manager_connections()
        )

    def publish_connection_reload(
        self,
        *,
        deleted: Iterable[ConnectionSummary] = (),
        created: Iterable[ConnectionSummary] = (),
        updated: Iterable[ConnectionSummary] = (),
    ) -> None:
        """Publish one committed authoritative reload in deterministic order."""

        if self._closed:
            return
        for event_type, summaries in (
            (EventType.CONNECTION_DELETED, deleted),
            (EventType.CONNECTION_CREATED, created),
            (EventType.CONNECTION_UPDATED, updated),
        ):
            for summary in summaries:
                if type(summary) is not ConnectionSummary:
                    raise TypeError("connection reload events require public summaries")
                self._publisher.publish(
                    event_type,
                    summary,
                    connection_id=summary.id,
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
        text = str(connection_id).strip()
        if not text:
            return None
        finder = getattr(self._connection_manager, "find_connection_by_nickname", None)
        if callable(finder):
            found = finder(text)
            if found is not None:
                return found
        for connection in self._manager_connections():
            nick = str(getattr(connection, "nickname", None) or getattr(connection, "id", None) or "").strip()
            if nick == text:
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
