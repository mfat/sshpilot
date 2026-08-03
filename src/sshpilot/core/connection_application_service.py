"""Daemon-owned application services over persistent connection managers.

This module is GTK-free and is instantiated only by the daemon composition root.
"""

import logging
import re
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple


from sshpilot import __version__ as sshpilot_version

from sshpilot.api.connection_identity import connection_id_for
from sshpilot.api.capabilities import Capabilities, Capability
from sshpilot.api.errors import ErrorCode, SshPilotError, unsupported_capability
from sshpilot.api.events import CoreEventCallback, EventPublisher, EventType, Subscription
from sshpilot.api.models.common import (
    ClientInfo,
    CompatibilityResult,
    ConnectionId,
    CoreInfo,
    ForwardId,
    SessionId,
    SftpServiceId,
    TransferId,
)
from sshpilot.api.models.connections import (
    AuthenticationMethod,
    ConnectionDetails,
    ConnectionEditorDetails,
    ConnectionHealth,
    ConnectionMutationResult,
    ConnectionSummary,
    CreateConnectionRequest,
    DeleteConnectionPasswordRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    ForwardingRule,
    GroupReference,
    LookupKeyPassphraseRequest,
    StoreConnectionPasswordRequest,
    StoreKeyPassphraseRequest,
    SplitConnectionRequest,
    UNSET,
    UpdateConnectionRequest,
    forwarding_rule_from_dict,
)
from sshpilot.api.models.interactions import (
    InteractionClaim,
    InteractionDecisionRequest,
    InteractionId,
    InteractionSummary,
)
from sshpilot.api.models.operations import (
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
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    AttachSessionResult,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionSummary,
)
from sshpilot.api.models.terminal import (
    ClaimTerminalInputRequest,
    ReleaseTerminalInputRequest,
    ReplayRequest,
    ReplayResult,
    ResizeTerminalRequest,
    TerminalInput,
)
from sshpilot.api.models.transfers import (
    CancelTransferRequest,
    StartTransferRequest,
    TransferSummary,
)
from sshpilot.api.terminal_events import (
    TerminalContinuityCallback,
    TerminalEofCallback,
    TerminalErrorCallback,
    TerminalOutputCallback,
    TerminalSubscription,
)
from sshpilot.api.version import API_IMPLEMENTATION_VERSION, PROTOCOL_VERSION

logger = logging.getLogger(__name__)

IMPLEMENTED_CLIENT_METHOD_CAPABILITIES = {
    "close": None,
    "get_capabilities": None,
    "get_connection": Capability.CONNECTIONS_READ,
    "get_connection_editor": Capability.CONNECTIONS_CONFIG_READ,
    "list_connections": Capability.CONNECTIONS_READ,
    "create_connection": Capability.CONNECTIONS_WRITE,
    "duplicate_connection": Capability.CONNECTIONS_WRITE,
    "delete_connection": Capability.CONNECTIONS_WRITE,
    "store_connection_password": Capability.CONNECTIONS_SECRETS_WRITE,
    "lookup_connection_password": Capability.CONNECTIONS_SECRETS_WRITE,
    "delete_connection_password": Capability.CONNECTIONS_SECRETS_WRITE,
    "store_key_passphrase": Capability.CONNECTIONS_SECRETS_WRITE,
    "delete_key_passphrase": Capability.CONNECTIONS_SECRETS_WRITE,
    "lookup_key_passphrase": Capability.CONNECTIONS_SECRETS_WRITE,
    "update_connection_metadata": Capability.CONNECTIONS_METADATA_WRITE,
    "assign_connection_to_group": Capability.CONNECTIONS_GROUPS,
    "create_group": Capability.CONNECTIONS_GROUPS,
    "delete_group": Capability.CONNECTIONS_GROUPS,
    "rename_group": Capability.CONNECTIONS_GROUPS,
    "split_connection": Capability.CONNECTIONS_SPLIT,
    "subscribe_events": Capability.CONNECTIONS_EVENTS,
    "update_connection": Capability.CONNECTIONS_WRITE,
}




class ConnectionApplicationService:
    """Provide daemon application operations over configuration managers.

    Command methods must be invoked on the thread that constructed the service,
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
        client_name: str = "sshpilotd",
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
            Capability.CONNECTIONS_CONFIG_READ,
            Capability.CONNECTIONS_SECRETS_WRITE,
            Capability.CONNECTIONS_METADATA_WRITE,
            Capability.CONNECTIONS_GROUPS,
            Capability.CONNECTIONS_SPLIT,
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
            supported.add(Capability.CONNECTIONS_CONFIG_WRITE)
        self._capabilities = Capabilities(
            protocol_version=PROTOCOL_VERSION,
            api_implementation_version=API_IMPLEMENTATION_VERSION,
            client=ClientInfo(name=client_name, version=client_version),
            core=CoreInfo(
                name="sshPilot Core",
                version=sshpilot_version,
                implementation="daemon-core",
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
        """Build a daemon-owned terminal process for any registered protocol.

        SSH retains its interaction-broker preparation below.  Other protocols
        use the same stateless ``ProtocolBackend.build_spawn`` contract that
        previously only the GTK process consumed; the daemon is now the process
        owner for those commands too.
        """

        import asyncio
        import shutil

        connection = self._find_connection(connection_id)
        protocol = str(getattr(connection, "protocol", "ssh") or "ssh")
        if protocol != "ssh":
            return self._prepare_protocol_terminal_launch(connection, protocol)
        resolver_policy = "normal" if interaction_policy == "broker" else interaction_policy
        prepared = asyncio.run(
            connection.native_connect(interaction_policy=resolver_policy)
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
        # Broker policy strips frontend askpass, so without this OpenSSH falls
        # back to the on-disk encrypted key and the user gets a passphrase dialog.
        self._preload_connection_keys(connection)
        argv = tuple(getattr(command, "command", ()) or ())
        environment = dict(getattr(command, "env", {}) or {})
        if not argv:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH session requires unsupported interaction",
                connection_id=connection_id,
            )
        # Preserve the normal resolver's askpass activation decision, but never
        # pass the frontend helper endpoint (or its staged secrets) to the
        # daemon.  The broker replaces the transport with its private helper.
        askpass_active = bool(
            getattr(command, "use_askpass", False)
            or environment.get("SSH_ASKPASS")
        )
        for key in tuple(environment):
            if key.startswith("SSHPILOT_ASKPASS") or key.startswith("SSHPILOT_SESSION_"):
                environment.pop(key, None)
        environment.pop("SSH_ASKPASS", None)
        environment.pop("SSH_ASKPASS_REQUIRE", None)
        if askpass_active:
            environment["SSHPILOT_DAEMON_ASKPASS_ACTIVE"] = "1"

        # Keep daemon launches on the canonical ``ssh Host`` path, but carry the
        # parsed LocalCommand explicitly.  Unlike the frontend terminal, the
        # daemon owns a separately prepared process and must not depend on a
        # frontend-side Connection object retaining this directive indirectly.
        # Command-line options precede ssh_config, so PermitLocalCommand cannot
        # be lost to a differing daemon-side config/default during launch.
        connection_data = getattr(connection, "data", None)
        data_local_command = (
            connection_data.get("local_command", "")
            if isinstance(connection_data, dict)
            else ""
        )
        local_command = str(
            getattr(connection, "local_command", "") or data_local_command or ""
        ).strip()
        if local_command:
            argv = (
                *argv[:-1],
                "-o",
                "PermitLocalCommand=yes",
                "-o",
                f"LocalCommand={local_command}",
                argv[-1],
            )
        executable = shutil.which(argv[0], path=environment.get("PATH"))
        if executable is None:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The OpenSSH executable is unavailable",
                connection_id=connection_id,
            )
        return (executable, *argv[1:]), environment

    def _prepare_protocol_terminal_launch(self, connection, protocol: str) -> tuple:
        """Resolve a non-SSH protocol through the headless plugin registry."""
        import shutil

        from sshpilot.plugins.api import PluginContext, ProtocolError
        from sshpilot.plugins.registry import protocol_registry

        registry = protocol_registry()
        backend = registry.get_or_none(protocol)
        if backend is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_SESSION_PROTOCOL,
                f"No terminal launch provider is registered for {protocol!r}",
                connection_id=connection_id_for(connection),
            )
        plugin_id = registry.plugin_id_for(protocol) or protocol
        config = getattr(self._connection_manager, "config", None)
        ctx = PluginContext.for_spawn(
            plugin_id=plugin_id,
            app_config=config,
            connection_manager=self._connection_manager,
            protocol_registry=registry,
        )
        try:
            spawn = backend.build_spawn(connection, ctx)
        except ProtocolError as exc:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                str(exc),
                connection_id=connection_id_for(connection),
            ) from exc
        argv = tuple(spawn.argv or ())
        environment = dict(spawn.env or {})
        if not argv:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                f"The {protocol} launch provider returned an empty command",
                connection_id=connection_id_for(connection),
            )
        executable = shutil.which(argv[0], path=environment.get("PATH"))
        if executable is None:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                f"The executable required by the {protocol} provider is unavailable",
                connection_id=connection_id_for(connection),
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
        executable = shutil.which(argv[0], path=environment.get("PATH"))
        if executable is None:
            raise SshPilotError(
                ErrorCode.FORWARD_STARTUP_FAILED,
                "The OpenSSH executable is unavailable",
                connection_id=connection_id,
            )
        return (executable, *argv[1:]), environment

    def lookup_connection_password(
        self, connection_id: ConnectionId
    ) -> Optional[str]:
        connection = self._find_connection(connection_id)
        if connection is None:
            return None
        return self._connection_manager.get_connection_password(connection)

    def lookup_daemon_password(self, connection_id: ConnectionId) -> Optional[str]:
        """Compatibility alias for the daemon dispatch layer."""

        return self.lookup_connection_password(connection_id)

    def store_daemon_password(
        self,
        connection_id: ConnectionId,
        password: str,
        *,
        previous_hostname: str = "",
        previous_host: str = "",
        previous_username: str = "",
    ) -> bool:
        connection = self._find_connection(connection_id)
        if connection is None:
            return False
        previous_connection = None
        if previous_hostname or previous_host or previous_username:
            previous_connection = {
                "hostname": previous_hostname,
                "host": previous_host,
                "username": previous_username,
            }
        return bool(
            self._connection_manager.store_connection_password(
                connection,
                password,
                previous_connection=previous_connection,
            )
        )

    def delete_daemon_password(
        self,
        connection_id: ConnectionId,
        *,
        previous_hostname: str = "",
        previous_host: str = "",
        previous_username: str = "",
    ) -> bool:
        connection = self._find_connection(connection_id)
        removed = False
        if connection is not None:
            removed = self._connection_manager.delete_connection_passwords(connection)
        if previous_hostname or previous_host or previous_username:
            previous_connection = {
                "hostname": previous_hostname,
                "host": previous_host,
                "username": previous_username,
            }
            username = previous_username or getattr(connection, "username", "") or ""
            removed_prev = self._connection_manager.delete_connection_passwords(
                previous_connection, username=username,
            )
            removed = removed or removed_prev
        # Deletion is idempotent: an already absent credential satisfies the
        # requested final state. Backend exceptions still propagate as errors.
        return True

    def store_connection_password_rpc(
        self, request: StoreConnectionPasswordRequest
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return self.store_daemon_password(
            request.connection_id,
            request.password,
            previous_hostname=request.previous_hostname,
            previous_host=request.previous_host,
            previous_username=request.previous_username,
        )

    def delete_connection_password_rpc(
        self, request: DeleteConnectionPasswordRequest
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return self.delete_daemon_password(
            request.connection_id,
            previous_hostname=request.previous_hostname,
            previous_host=request.previous_host,
            previous_username=request.previous_username,
        )

    def lookup_daemon_passphrase(self, key_path: str) -> Optional[str]:
        return self._connection_manager.get_key_passphrase(key_path)

    def store_daemon_passphrase(self, key_path: str, passphrase: str) -> bool:
        return bool(
            self._connection_manager.store_key_passphrase(key_path, passphrase)
        )

    def delete_daemon_passphrase(self, key_path: str) -> bool:
        # Preserve idempotence without masking a failed backend deletion: a
        # missing value is already in the requested state, while False for a
        # value known to exist is a real persistence failure.
        lookup = getattr(self._connection_manager, "get_key_passphrase", None)
        if not callable(lookup):
            self._connection_manager.delete_key_passphrase(key_path)
            return True
        existing = lookup(key_path)
        if existing is None:
            return True
        return bool(self._connection_manager.delete_key_passphrase(key_path))

    def store_key_passphrase_rpc(
        self, request: StoreKeyPassphraseRequest
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return self.store_daemon_passphrase(request.key_path, request.passphrase)

    def store_plugin_secret_rpc(self, request) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return self._connection_manager.store_plugin_secret(request.plugin_id, request.key, request.value)

    def get_plugin_secret_rpc(self, request):
        self._assert_command_thread()
        return self._connection_manager.get_plugin_secret(request.plugin_id, request.key)

    def delete_plugin_secret_rpc(self, request) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return self._connection_manager.delete_plugin_secret(request.plugin_id, request.key)

    def delete_key_passphrase_rpc(
        self, request: DeleteKeyPassphraseRequest
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return self.delete_daemon_passphrase(request.key_path)

    def lookup_key_passphrase_rpc(
        self, request: LookupKeyPassphraseRequest
    ) -> Optional[str]:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return self.lookup_daemon_passphrase(request.key_path)

    def update_connection_metadata(
        self, connection_id: ConnectionId, meta: Dict[str, Any]
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_METADATA_WRITE)
        connection = self._find_connection(connection_id)
        if connection is None:
            return False
        nickname = getattr(connection, "nickname", "")
        if not nickname:
            return False
        try:
            existing = self._connection_manager.config.get_connection_meta(nickname)
            existing.update(meta)
            self._connection_manager.config.set_connection_meta(nickname, existing)
            return True
        except Exception:
            logger.exception("Failed to update connection metadata via daemon RPC")
            return False

    def assign_connection_to_group(
        self, connection_id: ConnectionId, group_id: str
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        connection = self._find_connection(connection_id)
        if connection is None:
            return False
        nickname = getattr(connection, "nickname", "")
        if not nickname:
            return False
        try:
            group_manager = getattr(self._connection_manager, "group_manager", None)
            if group_manager is None:
                return False
            target = group_id if group_id else None
            group_manager.move_connection(nickname, target)
            return True
        except Exception:
            logger.exception("Failed to assign connection to group via daemon RPC")
            return False

    def create_group_rpc(
        self, name: str, parent_id: str = "", color: str = ""
    ) -> Optional[str]:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            group_manager = getattr(self._connection_manager, "group_manager", None)
            if group_manager is None:
                return None
            return group_manager.create_group(name, parent_id=parent_id or None, color=color or None)
        except Exception:
            logger.exception("Failed to create group via daemon RPC")
            return None

    def delete_group_rpc(self, group_id: str) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            group_manager = getattr(self._connection_manager, "group_manager", None)
            if group_manager is None:
                return False
            group_manager.delete_group(group_id)
            return True
        except Exception:
            logger.exception("Failed to delete group via daemon RPC")
            return False

    def rename_group_rpc(self, group_id: str, new_name: str) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            group_manager = getattr(self._connection_manager, "group_manager", None)
            if group_manager is None:
                return False
            group_manager.rename_group(group_id, new_name)
            return True
        except Exception:
            logger.exception("Failed to rename group via daemon RPC")
            return False

    def split_connection(self, request: SplitConnectionRequest) -> 'ConnectionMutationResult':
        """Split a host out of a multi-host SSH config block.

        Removes ``request.original_host_token`` from its block in
        ``request.source_config_path`` and appends a new standalone
        ``Host`` block built from ``request.config_patch``.
        """
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SPLIT)
        connection = self._find_connection(request.connection_id)
        if connection is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=request.connection_id,
            )
        # Stale-editor guard for split operations.
        if request.expected_generation is not None and request.expected_generation != getattr(connection, 'generation', 0):
            raise SshPilotError(
                ErrorCode.STALE_EDITOR,
                "The connection has been modified since it was last read",
                connection_id=request.connection_id,
            )
        # The connection_manager's _split_host_block does the real work.
        manager = self._connection_manager
        
        # Build complete dataset for format_ssh_config_entry
        new_data = dict(request.config_patch)
        new_data["nickname"] = request.nickname
        if request.hostname is not None:
            new_data["hostname"] = request.hostname
        if request.username is not None:
            new_data["username"] = request.username
        if request.port is not None:
            new_data["port"] = request.port
            
        try:
            success = manager._split_host_block(
                request.original_host_token,
                new_data,
                request.source_config_path,
            )
            if not success:
                raise SshPilotError(ErrorCode.PERSISTENCE_FAILED, "Failed to split host block")
            
            manager.load_ssh_config()
            
            new_nickname = request.nickname
            new_conn = manager.find_connection_by_nickname(new_nickname)
            if new_conn is None:
                # If we cannot find it under new_nickname, try finding by ID or fallback
                # but usually it should be found by nickname.
                raise SshPilotError(ErrorCode.CONNECTION_NOT_FOUND, "Failed to locate split connection")

            from sshpilot.api.models.connections import ConnectionMutationResult
            return ConnectionMutationResult(
                connection_id=getattr(new_conn, "id", new_conn.nickname),
                nickname=new_conn.nickname,
                generation=getattr(new_conn, "generation", 0),
            )
        except SshPilotError:
            raise
        except Exception:
            logger.exception("Failed to split host block via daemon RPC")
            raise SshPilotError(ErrorCode.PERSISTENCE_FAILED, "Failed to split host block")

    def enable_serialized_command_threads(self) -> None:
        """Allow daemon-owned serialized workers to invoke this service."""

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

    def get_connection_editor(
        self, connection_id: ConnectionId
    ) -> ConnectionEditorDetails:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_CONFIG_READ)
        connection = self._find_connection(connection_id)
        if connection is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        return self._to_editor_details(connection)

    def create_connection(self, request: CreateConnectionRequest) -> ConnectionMutationResult:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_WRITE)
        if request.config_patch:
            self._require_capability(Capability.CONNECTIONS_CONFIG_WRITE)
        if type(request) is not CreateConnectionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A create connection request is required",
            )
        if request.protocol == "ssh" and request.plugin_data:
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "SSH connections cannot contain plugin data",
                details={"field": "plugin_data"},
            )
        if request.protocol != "ssh" and request.config_patch:
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "Plugin connections cannot contain SSH configuration",
                details={"field": "config_patch"},
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
        if request.config_patch:
            for key, value in request.config_patch.items():
                if key in ("proxy_jump", "identity_files", "certificate_files"):
                    if isinstance(value, str):
                        if key == "proxy_jump":
                            data[key] = [h.strip() for h in re.split(r'[\s,]+', value) if h.strip()]
                        else:
                            data[key] = [f.strip() for f in re.split(r'[\r\n,]+', value) if f.strip()]
                    else:
                        data[key] = list(value) if value else []
                elif key == "forwarding_rules":
                    from sshpilot.api.models.connections import forwarding_rule_to_dict
                    try:
                        data[key] = [forwarding_rule_to_dict(r) for r in value]
                    except ValueError as e:
                        raise SshPilotError(
                            ErrorCode.VALIDATION_FAILED,
                            str(e),
                        )
                else:
                    data[key] = value
        if request.plugin_data:
            data.update(dict(request.plugin_data))
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
        return ConnectionMutationResult(
            connection_id=self.connection_id_for(connection),
            nickname=connection.nickname,
            generation=getattr(connection, "generation", 0),
        )

    def duplicate_connection(
        self, connection_id: ConnectionId
    ) -> ConnectionMutationResult:
        """Duplicate a daemon-owned connection through its sole persistence owner."""

        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_WRITE)
        connection = self._find_connection(connection_id)
        if connection is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        duplicate = getattr(self._connection_manager, "duplicate_connection", None)
        try:
            if callable(duplicate):
                created = duplicate(connection, self._group_manager)
            else:
                import copy
                from .connections.models import generate_duplicate_nickname

                creator = getattr(self._connection_manager, "create_connection", None)
                if not callable(creator):
                    raise self._persistence_error(connection_id)
                names = {
                    str(getattr(item, "nickname", "") or "")
                    for item in self._connection_manager.get_connections()
                }
                data = copy.deepcopy(
                    getattr(connection, "data", None) or {}
                )
                data.pop("uuid", None)
                data.pop("aliases", None)
                data["nickname"] = generate_duplicate_nickname(
                    str(getattr(connection, "nickname", "") or "Connection"),
                    names,
                )
                created = creator(data)
        except SshPilotError:
            raise
        except Exception as error:
            logger.error(
                "Connection manager duplicate failed (%s)",
                type(error).__name__,
            )
            raise self._persistence_error(connection_id) from None
        return ConnectionMutationResult(
            connection_id=self.connection_id_for(created),
            nickname=created.nickname,
            generation=getattr(created, "generation", 0),
        )

    def update_connection(
        self,
        connection_id: ConnectionId,
        request: UpdateConnectionRequest,
    ) -> ConnectionMutationResult:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_WRITE)
        if request.config_patch:
            self._require_capability(Capability.CONNECTIONS_CONFIG_WRITE)
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
        protocol = getattr(connection, "protocol", "ssh")
        if protocol == "ssh" and request.plugin_data:
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "SSH connections cannot contain plugin data",
                connection_id=connection_id,
            )
        if protocol != "ssh" and request.config_patch:
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "Plugin connections cannot contain SSH configuration",
                connection_id=connection_id,
            )
        # Stale-editor guard: if the caller provides an expected generation,
        # reject the update when the connection was modified since the editor
        # last read it. None means "no generation supplied" (skip the check);
        # zero is a legitimate first-generation value and is enforced.
        if request.expected_generation is not None and request.expected_generation != getattr(connection, 'generation', 0):
            raise SshPilotError(
                ErrorCode.STALE_EDITOR,
                "The connection has been modified since it was last read",
                connection_id=connection_id,
            )
        old_nickname = str(getattr(connection, "nickname", "") or "")
        if (
            request.nickname is not None
            and request.nickname is not UNSET
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
            if value is not None and value is not UNSET:
                data[name] = value
        if request.config_patch:
            for key, value in request.config_patch.items():
                if key in ("proxy_jump", "identity_files", "certificate_files"):
                    if isinstance(value, str):
                        if key == "proxy_jump":
                            data[key] = [h.strip() for h in re.split(r'[\s,]+', value) if h.strip()]
                        else:
                            data[key] = [f.strip() for f in re.split(r'[\r\n,]+', value) if f.strip()]
                    else:
                        data[key] = list(value) if value else []

                elif key == "forwarding_rules":
                    from sshpilot.api.models.connections import forwarding_rule_to_dict
                    try:
                        data[key] = [forwarding_rule_to_dict(r) for r in value]
                    except ValueError as e:
                        raise SshPilotError(
                            ErrorCode.VALIDATION_FAILED,
                            str(e),
                            connection_id=connection_id,
                        )
                else:
                    data[key] = value
        if request.plugin_data:
            data.update(dict(request.plugin_data))
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
        return ConnectionMutationResult(
            connection_id=self.connection_id_for(connection),
            nickname=connection.nickname,
            generation=getattr(connection, "generation", 0),
        )

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

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        """Subscribe the daemon event bridge to connection mutations."""
        if self._closed:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST, "The application service is closed"
            )
        try:
            return self._publisher.subscribe(callback)
        except RuntimeError:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST, "The application service is closed"
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

    connection_id_for = staticmethod(connection_id_for)

    def _assert_command_thread(self) -> None:
        if self._closed:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "The application service is closed")
        if (
            not self._allow_cross_thread_commands
            and threading.get_ident() != self._owner_thread_id
        ):
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "Application service commands must run on their owner thread",
            )

    def snapshot_connection_summaries(self) -> Tuple[ConnectionSummary, ...]:
        """Return an immutable application-service snapshot for daemon reloads."""

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
                "port": ConnectionApplicationService._port(connection),
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

    def _to_editor_details(self, connection: Any) -> ConnectionEditorDetails:
        summary = self._to_summary(connection)
        details = self._to_details(connection)

        data = getattr(connection, "data", None)
        data = data if isinstance(data, dict) else {}

        def _get_val(attr_name: str, default: Any = None) -> Any:
            if hasattr(connection, attr_name):
                return getattr(connection, attr_name)
            if attr_name in data:
                return data[attr_name]
            return default


        def _str(val: Any, default: str = "") -> str:
            v = str(val) if val is not None else default
            return v

        def _int(val: Any, default: int = 0) -> int:
            try:
                return int(val)
            except (TypeError, ValueError):
                return default

        def _bool(val: Any) -> bool:
            return bool(val)

        def _tuple_str(val: Any) -> Tuple[str, ...]:
            return self._string_tuple(val)

        def _tuple_fw(val: Any) -> Tuple[ForwardingRule, ...]:
            if not val:
                return ()
            rules = []
            for item in val:
                if isinstance(item, dict):
                    try:
                        rules.append(forwarding_rule_from_dict(item))
                    except (ValueError, TypeError):
                        pass
                elif isinstance(item, ForwardingRule):
                    rules.append(item)
            return tuple(rules)

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
            aliases=details.aliases,
            authentication_method=details.authentication_method,
            identity_configured=details.identity_configured,
            certificate_configured=details.certificate_configured,
            x11_forwarding=details.x11_forwarding,
            forwarding_rule_count=details.forwarding_rule_count,
            proxy_jump=details.proxy_jump,
            key_select_mode=_int(_get_val("key_select_mode", 0)),
            identity_files=_tuple_str(_get_val("identity_files", ())),
            certificate_files=_tuple_str(_get_val("certificate_files", ())),
            identity_agent=_str(_get_val("identity_agent", "")),
            add_keys_to_agent=_str(_get_val("add_keys_to_agent", "")),
            pkcs11_provider=_str(_get_val("pkcs11_provider", "")),
            security_key_provider=_str(_get_val("security_key_provider", "")),
            pubkey_auth_no=_bool(_get_val("pubkey_auth_no", False)),
            forward_agent=_bool(_get_val("forward_agent", False)),
            forward_agent_explicit_no=_bool(_get_val("forward_agent_explicit_no", False)),
            forward_agent_target=_str(_get_val("forward_agent_target", "")),
            proxy_command=_str(_get_val("proxy_command", "")),
            forwarding_rules=_tuple_fw(_get_val("forwarding_rules", ())),
            pre_command=_str(_get_val("pre_command", "")),
            local_command=_str(_get_val("local_command", "")),
            remote_command=_str(_get_val("remote_command", "")),
            request_tty=_str(_get_val("request_tty", "")),
            extra_ssh_config=_str(_get_val("extra_ssh_config", "")),
            identity_file_none=_bool(_get_val("identity_file_none", False)),
            x11_forwarding_explicit_no=_bool(_get_val("x11_forwarding_explicit_no", False)),
            identities_only_explicit_no=_bool(_get_val("identities_only_explicit_no", False)),
            preferred_authentications=_str(_get_val("preferred_authentications", "")),
            source=_str(_get_val("source", "")),
            generation=_int(_get_val("generation", 0)),
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
