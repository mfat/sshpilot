"""Daemon application service over the headless connection repository.

``ConnectionApplicationService`` is the daemon's command surface for saved
connection state. It no longer owns ``ConnectionManager``/``GroupManager``
instances: every read, mutation, and event flows through a
:class:`~sshpilot.core.connections.repository.ConnectionRepositoryProtocol`
(repository-backed in production, fake in tests).

* GTK receives immutable API snapshots and events only.
* Launch and secret behavior is delegated to injected providers
  (``launch_provider`` / ``secret_provider``); core imports neither
  ``sshpilot.config``, ``ssh_connection_builder``, nor plugin modules.
* Repository changes publish one ``connection_store.changed`` event with the
  full snapshot plus the compatibility ``connection.created`` /
  ``connection.updated`` / ``connection.deleted`` events derived from the
  before/after diff (deterministic order: deleted, created, updated, then
  store-changed), always after persistence commit.
"""

from __future__ import annotations

import logging
import getpass
import ipaddress
import re
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..api.capabilities import Capabilities, Capability
from ..api.errors import ErrorCode, SshPilotError, unsupported_capability
from ..api.events import EventPublisher, EventType, Subscription
from ..api.models.common import ClientInfo, CompatibilityResult, CoreInfo
from ..api.models.connections import (
    AuthenticationMethod,
    ConnectionDetails,
    ConnectionEditorDetails,
    ConnectionId,
    ConnectionMutationResult,
    ConnectionSummary,
    EffectiveConfigComparison,
    UnsavedHostCheckRequest,
    UnsavedHostCheckResult,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    ForwardingRule,
    SaveSshConfigTextRequest,
    SplitConnectionRequest,
    SshConfigText,
    UpdateConnectionRequest,
    UNSET,
    forwarding_rule_from_dict,
)
from ..api.models.settings import (
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
)
from ..api.models.terminal import ExternalTerminalLaunchSpec
from ..api.version import API_IMPLEMENTATION_VERSION, PROTOCOL_VERSION
from .connections.models import ConnectionRecord
from .connections.repository import (
    ConnectionRepositoryProtocol,
    RepositoryChange,
)
from .errors import CoreError

from sshpilot import __version__ as sshpilot_version

logger = logging.getLogger(__name__)

IMPLEMENTED_CLIENT_METHOD_CAPABILITIES = {
    "close": None,
    "get_capabilities": None,
    "get_connection": Capability.CONNECTIONS_READ,
    "get_connection_editor": Capability.CONNECTIONS_CONFIG_READ,
    "get_effective_config": Capability.CONNECTIONS_CONFIG_READ,
    "check_unsaved_host": Capability.CONNECTIONS_READ,
    "get_ssh_config_text": Capability.CONNECTIONS_CONFIG_READ,
    "prepare_external_terminal_launch": Capability.EXTERNAL_TERMINAL_LAUNCH,
    "save_ssh_config_text": Capability.CONNECTIONS_CONFIG_WRITE,
    "list_connections": Capability.CONNECTIONS_READ,
    "create_connection": Capability.CONNECTIONS_WRITE,
    "duplicate_connection": Capability.CONNECTIONS_WRITE,
    "delete_connection": Capability.CONNECTIONS_WRITE,
    "store_connection_password": Capability.CONNECTIONS_SECRETS_WRITE,
    "set_session_connection_password": Capability.CONNECTIONS_SECRETS_WRITE,
    "clear_session_connection_password": Capability.CONNECTIONS_SECRETS_WRITE,
    "delete_connection_password": Capability.CONNECTIONS_SECRETS_WRITE,
    "store_key_passphrase": Capability.CONNECTIONS_SECRETS_WRITE,
    "delete_key_passphrase": Capability.CONNECTIONS_SECRETS_WRITE,
    "update_connection_metadata": Capability.CONNECTIONS_METADATA_WRITE,
    "add_tag_to_connections": Capability.CONNECTIONS_METADATA_WRITE,
    "assign_connection_to_group": Capability.CONNECTIONS_GROUPS,
    "move_connections": Capability.CONNECTIONS_GROUPS,
    "create_group": Capability.CONNECTIONS_GROUPS,
    "delete_group": Capability.CONNECTIONS_GROUPS,
    "rename_group": Capability.CONNECTIONS_GROUPS,
    "split_connection": Capability.CONNECTIONS_SPLIT,
    "subscribe_events": Capability.CONNECTIONS_EVENTS,
    "update_connection": Capability.CONNECTIONS_WRITE,
    "get_global_ssh_overrides": Capability.SSH_OVERRIDES_READ,
    "update_global_ssh_overrides": Capability.SSH_OVERRIDES_WRITE,
    "reset_global_ssh_overrides": Capability.SSH_OVERRIDES_WRITE,
}


class ConnectionApplicationService:
    """Provide daemon application operations over the connection repository.

    Command methods must be invoked on the thread that constructed the service
    (matching the repository's serialized mutation model); event subscription
    is thread-safe.
    """

    def __init__(
        self,
        repository: ConnectionRepositoryProtocol,
        *,
        launch_provider: Any = None,
        secret_provider: Any = None,
        ssh_overrides: Any = None,
        client_name: str = "sshpilotd",
        client_version: str = sshpilot_version,
        allow_cross_thread_commands: bool = False,
    ) -> None:
        if repository is None:
            raise ValueError("repository is required")
        self._repository = repository
        self._launch_provider = launch_provider
        self._secret_provider = secret_provider
        self._ssh_overrides_service = ssh_overrides
        self._owner_thread_id = threading.get_ident()
        self._allow_cross_thread_commands = bool(allow_cross_thread_commands)
        self._publisher = EventPublisher()
        self._closed = False
        supported = {
            Capability.CONNECTIONS_READ,
            Capability.CONNECTIONS_WRITE,
            Capability.CONNECTIONS_EVENTS,
            Capability.CONNECTIONS_CONFIG_READ,
            Capability.CONNECTIONS_CONFIG_WRITE,
            Capability.CONNECTIONS_SECRETS_WRITE,
            Capability.CONNECTIONS_METADATA_WRITE,
            Capability.CONNECTIONS_GROUPS,
            Capability.CONNECTIONS_SPLIT,
        }
        if callable(getattr(launch_provider, "prepare_terminal_launch", None)):
            supported.add(Capability.EXTERNAL_TERMINAL_LAUNCH)
        if ssh_overrides is not None:
            supported.update(
                {
                    Capability.SSH_OVERRIDES_READ,
                    Capability.SSH_OVERRIDES_WRITE,
                }
            )
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
        self._repository.add_listener(self._on_repository_change)

    def get_capabilities(self) -> Capabilities:
        return self._capabilities

    @property
    def supports_external_terminal_launch(self) -> bool:
        """Whether the composed daemon has a real external-launch provider."""
        return callable(
            getattr(self._launch_provider, "prepare_terminal_launch", None)
        )

    # ------------------------------------------------------------------
    # Launch / secret providers (implementation lives outside core)
    # ------------------------------------------------------------------

    def _provider_call(self, provider: Any, method: str, *args, **kwargs):
        if provider is None:
            return None
        func = getattr(provider, method, None)
        if not callable(func):
            return None
        return func(*args, **kwargs)

    def prepare_daemon_terminal_launch(
        self,
        connection_id: ConnectionId,
        *,
        interaction_policy: str = "none",
        remote_command: Optional[str] = None,
        force_tty: bool = False,
    ) -> tuple:
        self._assert_command_thread()
        result = self._provider_call(
            self._launch_provider,
            "prepare_terminal_launch",
            connection_id,
            interaction_policy=interaction_policy,
            remote_command=remote_command,
            force_tty=force_tty,
        )
        if result is None:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH session could not be prepared",
                connection_id=connection_id,
            )
        return result

    def prepare_external_terminal_launch(
        self, connection_id: ConnectionId
    ) -> ExternalTerminalLaunchSpec:
        """Prepare external-terminal SSH semantics without returning secrets."""
        self._assert_command_thread()
        argv, environment = self.prepare_daemon_terminal_launch(
            connection_id,
            interaction_policy="none",
        )
        approved_environment = tuple(
            sorted(
                (name, value)
                for name, value in environment.items()
                if name == "SSH_AUTH_SOCK"
            )
        )
        details = self.get_connection(connection_id)
        return ExternalTerminalLaunchSpec(
            argv=tuple(argv),
            environment=approved_environment,
            display_name=details.nickname,
            secret_autofill_supported=False,
        )

    def prepare_daemon_scp_target(self, connection_id: ConnectionId) -> str:
        self._assert_command_thread()
        result = self._provider_call(
            self._launch_provider,
            "scp_target",
            connection_id,
        )
        if result is None:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SCP target could not be prepared",
                connection_id=connection_id,
            )
        return str(result)

    def prepare_daemon_scp_launch(
        self,
        connection_id: ConnectionId,
        *,
        extra_args=None,
        interaction_policy: str = "broker",
        target_override=None,
    ) -> tuple:
        self._assert_command_thread()
        result = self._provider_call(
            self._launch_provider,
            "prepare_scp_launch",
            connection_id,
            extra_args=extra_args,
            interaction_policy=interaction_policy,
            target_override=target_override,
        )
        if result is None:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SCP transfer could not be prepared",
                connection_id=connection_id,
            )
        return result

    def prepare_daemon_sftp_launch(
        self,
        connection_id: ConnectionId,
        *,
        interaction_policy: str = "broker",
    ) -> tuple:
        self._assert_command_thread()
        result = self._provider_call(
            self._launch_provider,
            "prepare_sftp_launch",
            connection_id,
            interaction_policy=interaction_policy,
        )
        if result is None:
            raise SshPilotError(
                ErrorCode.SFTP_SERVICE_NOT_READY,
                "The SFTP session could not be prepared",
                connection_id=connection_id,
            )
        return result

    def prepare_daemon_forward_launch(
        self,
        connection_id: ConnectionId,
        *,
        forward_type: str = "local",
        bind_host: str = "localhost",
        bind_port: int = 0,
        destination_host: Optional[str] = None,
        destination_port: Optional[int] = None,
        interaction_policy: str = "broker",
    ) -> tuple:
        self._assert_command_thread()
        result = self._provider_call(
            self._launch_provider,
            "prepare_forward_launch",
            connection_id,
            forward_type=forward_type,
            bind_host=bind_host,
            bind_port=bind_port,
            destination_host=destination_host,
            destination_port=destination_port,
            interaction_policy=interaction_policy,
        )
        if result is None:
            raise SshPilotError(
                ErrorCode.FORWARD_STARTUP_FAILED,
                "The forward could not be prepared",
                connection_id=connection_id,
            )
        return result

    def lookup_daemon_password(self, connection_id: ConnectionId) -> Optional[str]:
        return self._provider_call(
            self._secret_provider, "lookup_connection_password", connection_id
        )

    def has_connection_password(self, connection_id: ConnectionId) -> bool:
        return bool(self._provider_call(
            self._secret_provider, "has_connection_password", connection_id
        ))

    def reveal_connection_password(self, connection_id: ConnectionId) -> bytearray:
        value = self.lookup_daemon_password(connection_id)
        return bytearray(value.encode("utf-8")) if value is not None else bytearray()

    def store_daemon_password(
        self,
        connection_id: ConnectionId,
        password: str,
        *,
        previous_hostname: str = "",
        previous_host: str = "",
        previous_username: str = "",
    ) -> bool:
        result = self._provider_call(
            self._secret_provider,
            "store_connection_password",
            connection_id,
            password,
            previous_hostname=previous_hostname,
            previous_host=previous_host,
            previous_username=previous_username,
        )
        return bool(result)

    def delete_daemon_password(
        self,
        connection_id: ConnectionId,
        *,
        previous_hostname: str = "",
        previous_host: str = "",
        previous_username: str = "",
    ) -> bool:
        result = self._provider_call(
            self._secret_provider,
            "delete_connection_password",
            connection_id,
            previous_hostname=previous_hostname,
            previous_host=previous_host,
            previous_username=previous_username,
        )
        return bool(result)

    def store_connection_password_rpc(
        self, request: Any
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

    def set_session_connection_password_rpc(self, request: Any, password: bytearray) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return bool(self._provider_call(
            self._secret_provider,
            "set_session_connection_password",
            request.connection_id,
            password,
        ))

    def clear_session_connection_password(self, connection_id: ConnectionId) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return bool(self._provider_call(
            self._secret_provider,
            "clear_session_connection_password",
            connection_id,
        ))

    def delete_connection_password_rpc(
        self, request: Any
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
        return self._provider_call(
            self._secret_provider, "lookup_key_passphrase", key_path
        )

    def has_key_passphrase(self, key_path: str) -> bool:
        return bool(self._provider_call(
            self._secret_provider, "has_key_passphrase", key_path
        ))

    def reveal_key_passphrase(self, key_path: str) -> bytearray:
        value = self.lookup_daemon_passphrase(key_path)
        return bytearray(value.encode("utf-8")) if value is not None else bytearray()

    def store_daemon_passphrase(self, key_path: str, passphrase: str) -> bool:
        return bool(
            self._provider_call(
                self._secret_provider,
                "store_key_passphrase",
                key_path,
                passphrase,
            )
        )

    def delete_daemon_passphrase(self, key_path: str) -> bool:
        return bool(
            self._provider_call(
                self._secret_provider, "delete_key_passphrase", key_path
            )
        )

    def store_key_passphrase_rpc(self, request: Any) -> bool:
        self._assert_command_thread()
        raise unsupported_capability(Capability.CONNECTIONS_SECRETS_WRITE)

    def delete_key_passphrase_rpc(self, request: Any) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return self.delete_daemon_passphrase(request.key_path)

    def store_plugin_secret_rpc(self, request) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return bool(
            self._provider_call(
                self._secret_provider,
                "store_plugin_secret",
                request.plugin_id,
                request.key,
                request.value,
            )
        )

    def get_plugin_secret_rpc(self, request):
        self._assert_command_thread()
        value = self._provider_call(
            self._secret_provider, "get_plugin_secret", request.plugin_id, request.key
        )
        return bytearray(value.encode("utf-8")) if value is not None else bytearray()

    def delete_plugin_secret_rpc(self, request) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SECRETS_WRITE)
        return bool(
            self._provider_call(
                self._secret_provider,
                "delete_plugin_secret",
                request.plugin_id,
                request.key,
            )
        )

    # ------------------------------------------------------------------
    # Metadata / group operations
    # ------------------------------------------------------------------

    def update_connection_metadata(
        self, connection_id: ConnectionId, meta: Dict[str, Any]
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_METADATA_WRITE)
        try:
            self._repository.update_connection_metadata(connection_id, meta)
            return True
        except SshPilotError:
            raise
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to update connection metadata via daemon RPC")
            raise self._persistence_error(connection_id) from error

    def add_tag_to_connections_rpc(self, request: Any) -> int:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_METADATA_WRITE)
        try:
            count = self._repository.add_tag_to_connections(request)
            if type(count) is not int or count < 0:
                raise TypeError("repository returned an invalid tag add count")
            return count
        except SshPilotError:
            raise
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to add a tag via daemon RPC")
            raise self._persistence_error() from error

    def move_connections_rpc(self, request: Any) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            self._repository.move_connections(request)
            return True
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to move connections via daemon RPC")
            raise self._persistence_error() from error

    def assign_connection_to_group(
        self, connection_id: ConnectionId, group_id: str
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            self._repository.assign_connection_to_group(
                connection_id, group_id or None
            )
            return True
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to assign connection to group via daemon RPC")
            raise self._persistence_error(connection_id) from error

    def create_group_rpc(
        self, name: str, parent_id: str = "", color: str = ""
    ) -> Optional[str]:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            group = self._repository.create_group(
                name,
                parent_id=parent_id or None,
                color=color or "",
            )
            return group.id if group is not None else None
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to create group via daemon RPC")
            raise self._persistence_error() from error

    def delete_group_rpc(self, group_id: str) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            self._repository.delete_group(group_id)
            return True
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to delete group via daemon RPC")
            raise self._persistence_error() from error

    def rename_group_rpc(self, group_id: str, new_name: str) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            self._repository.rename_group(group_id, new_name)
            return True
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to rename group via daemon RPC")
            raise self._persistence_error() from error

    def set_group_color_rpc(self, group_id: str, color: str) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            self._repository.set_group_color(group_id, color)
            return True
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to set group color via daemon RPC")
            raise self._persistence_error() from error

    def place_group_rpc(
        self,
        group_id: str,
        parent_id: Optional[str],
        index: int,
        *,
        expected_generation: Optional[int] = None,
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            self._repository.place_group(
                group_id,
                parent_id,
                index,
                expected_generation=expected_generation,
            )
            return True
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to place group via daemon RPC")
            raise self._persistence_error() from error

    def copy_connection_to_group_rpc(
        self, connection_id: ConnectionId, group_id: str
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            self._repository.copy_connection_to_group(connection_id, group_id)
            return True
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to copy connection to group via daemon RPC")
            raise self._persistence_error(connection_id) from error

    def remove_connection_from_group_rpc(
        self, connection_id: ConnectionId, group_id: str
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            self._repository.remove_connection_from_group(connection_id, group_id)
            return True
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to remove connection from group via daemon RPC")
            raise self._persistence_error(connection_id) from error

    def reorder_connection_rpc(
        self,
        connection_id: ConnectionId,
        target_connection_id: ConnectionId,
        group_id: Optional[str],
        position: str,
    ) -> bool:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_GROUPS)
        try:
            self._repository.reorder_connection(
                connection_id, target_connection_id, group_id, position
            )
            return True
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to reorder connection via daemon RPC")
            raise self._persistence_error(connection_id) from error

    def rename_tag_rpc(self, old_tag: str, new_tag: str) -> int:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_METADATA_WRITE)
        try:
            count = self._repository.rename_tag(old_tag, new_tag)
            if type(count) is not int or count < 0:
                raise TypeError("repository returned an invalid tag rename count")
            return count
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Failed to rename tag via daemon RPC")
            raise self._persistence_error() from error

    def split_connection(self, request: SplitConnectionRequest) -> ConnectionMutationResult:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_SPLIT)
        if type(request) is not SplitConnectionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A split connection request is required",
            )
        if self._repository.get_record(request.connection_id) is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=request.connection_id,
            )
        data: Dict[str, Any] = dict(request.config_patch)
        data["nickname"] = request.nickname
        data["protocol"] = "ssh"
        if request.hostname is not None:
            data["hostname"] = request.hostname
        if request.username is not None:
            data["username"] = request.username
        if request.port is not None:
            data["port"] = request.port
        data["source_config_path"] = request.source_config_path
        try:
            record = self._repository.split_connection(
                request.connection_id,
                request.original_host_token,
                data,
                expected_generation=request.expected_generation,
            )
        except SshPilotError:
            raise
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Repository split failed")
            raise self._persistence_error(request.connection_id) from error
        return ConnectionMutationResult(
            connection_id=record.id,
            nickname=record.nickname,
            generation=record.generation,
            display_name=self._record_to_summary(record).display_name,
        )

    def enable_serialized_command_threads(self) -> None:
        self._allow_cross_thread_commands = True

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_connections(self) -> List[ConnectionSummary]:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_READ)
        return list(self._repository.snapshot().connections)

    def get_connection(self, connection_id: ConnectionId) -> ConnectionDetails:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_READ)
        record = self._repository.get_record(connection_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        return self._record_to_details(record)

    def get_connection_editor(
        self, connection_id: ConnectionId
    ) -> ConnectionEditorDetails:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_CONFIG_READ)
        record = self._repository.get_editor_record(connection_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        return self._record_to_editor_details(record)

    def get_effective_config(
        self, connection_id: ConnectionId
    ) -> EffectiveConfigComparison:
        """Resolve authored and effective OpenSSH configuration in the daemon."""
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_CONFIG_READ)
        record = self._repository.get_editor_record(connection_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        from ..ssh_config_formatter import format_ssh_config_entry
        from .ssh_config_effective import collect_host_block_lines, diff_effective_config

        before_generation = self._repository.snapshot().generation
        root = str(self._repository.root_config_path)
        logger.debug(
            "Effective-config resolve start: id=%s host=%s mode=%s root=%s "
            "root_file=%s generation=%s",
            connection_id,
            record.nickname,
            "isolated" if self._repository.ssh_config_isolated else "default",
            root,
            self._repository.root_config_path.is_file(),
            before_generation,
        )
        own_lines = collect_host_block_lines(record.nickname, root)
        own_block = "\n".join(own_lines)
        if not own_block:
            own_block = format_ssh_config_entry(record.to_dict())
        logger.debug(
            "Effective-config authored block: id=%s host=%s own_lines=%d fallback=%s",
            connection_id,
            record.nickname,
            len(own_lines),
            not bool(own_lines),
        )
        try:
            result = diff_effective_config(
                record.nickname,
                root,
                own_block,
                include_system_defaults_in_display=not self._repository.ssh_config_isolated,
            )
            logger.debug(
                "Effective-config resolved: id=%s host=%s has_diff=%s "
                "changes=%d own=%d full=%d",
                connection_id,
                record.nickname,
                bool(result and result.get("has_diff")),
                len(result.get("changes") or ()) if result else -1,
                len(result.get("own") or ()) if result else -1,
                len(result.get("full") or ()) if result else -1,
            )
        except Exception:
            logger.debug("Daemon effective-config resolution failed", exc_info=True)
            result = None
        after_generation = self._repository.snapshot().generation
        # A reload/mode transition raced the resolver.  Never publish a
        # comparison tagged with the pre-transition snapshot.
        if before_generation != after_generation:
            logger.debug(
                "Effective-config generation raced: id=%s generation=%s->%s, "
                "discarding result",
                connection_id,
                before_generation,
                after_generation,
            )
            result = None
        if not result:
            return EffectiveConfigComparison(
                connection_id=connection_id,
                host=record.nickname,
                available=False,
                generation=max(0, int(after_generation)),
            )
        return EffectiveConfigComparison(
            connection_id=connection_id,
            host=record.nickname,
            available=True,
            has_diff=bool(result.get("has_diff")),
            changes=tuple(dict(item) for item in (result.get("changes") or ())),
            own=tuple(result.get("own") or ()),
            full=tuple(result.get("full") or ()),
            generation=max(0, int(after_generation)),
        )

    def check_unsaved_host(
        self, request: UnsavedHostCheckRequest
    ) -> UnsavedHostCheckResult:
        """Compare a destination against daemon-owned saved identities.

        A destination is saved when its internal id exists, or when the
        normalized destination token (hostname, authored host alias, or
        connection alias) and explicit username match a saved record. This
        rule intentionally does not rerun OpenSSH in GTK or infer identity
        from presentation-store paths.
        """
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_READ)
        if type(request) is not UnsavedHostCheckRequest:
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "A destination check request is required")
        hostname = request.hostname.strip()
        username = request.username.strip()
        snapshot = self._repository.snapshot()
        saved = bool(
            request.connection_id
            and any(item.id == request.connection_id for item in snapshot.connections)
        )
        if not saved and request.protocol.strip().lower() == "ssh":
            requested_identity = self._destination_identity(
                hostname,
                username,
                request.port,
                request.proxy_jump,
            )
            for record in self._repository.list_records():
                if (record.protocol or "ssh").strip().lower() != "ssh":
                    continue
                # The requested destination is resolved once.  Saved records
                # contribute their daemon-owned semantic fields without
                # spawning ``ssh -G`` for every nickname/alias in the store.
                # This keeps the optional save-prompt check bounded and does
                # not hold configuration serialization across an O(N) probe.
                if requested_identity in self._saved_destination_identities(
                    record, requested_identity
                ):
                    saved = True
                    break
        return UnsavedHostCheckResult(
            saved=saved,
            hostname=hostname.casefold(),
            username=username,
            generation=max(0, int(snapshot.generation)),
        )

    def _active_config_file(self) -> Optional[str]:
        root = getattr(self._repository, "root_config_path", None)
        isolated = bool(getattr(self._repository, "ssh_config_isolated", False))
        return str(root) if root is not None and isolated else None

    def _destination_identity(
        self,
        hostname: str,
        username: str,
        port: Optional[int],
        proxy_jump: Tuple[str, ...],
    ) -> tuple:
        from .ssh_config_effective import get_effective_ssh_config

        proxy = ",".join(proxy_jump)
        effective = get_effective_ssh_config(
            hostname,
            self._active_config_file(),
            user=username or None,
            port=port,
            proxy_jump=proxy or None,
        )
        resolved_host = str(effective.get("hostname") or hostname).strip()
        resolved_user = str(effective.get("user") or username or getpass.getuser()).strip()
        try:
            resolved_port = int(str(effective.get("port") or port or 22))
        except (TypeError, ValueError):
            resolved_port = port or 22
        proxy_value = effective.get("proxyjump") or proxy
        if isinstance(proxy_value, list):
            proxy_value = ",".join(proxy_value)
        return (
            self._normalize_host(resolved_host),
            resolved_user,
            resolved_port,
            str(proxy_value or "none").casefold(),
        )

    def _saved_destination_identities(
        self, record: ConnectionRecord, requested: Optional[tuple] = None
    ) -> set[tuple]:
        proxy_jump = tuple(record.data.get("proxy_jump") or ()) if record.data else ()
        # ``hostname`` is the saved destination identity.  Nicknames and
        # aliases are presentation/configuration labels; resolving every one
        # with a subprocess made this optional check scale with the entire
        # repository.  The requested side is resolved by OpenSSH, so an alias
        # used by the ad-hoc connection still matches a saved concrete target.
        values = {record.hostname, record.host, *(record.aliases or ())}
        # A directive the Host block never authored is inherited, and only the
        # requested side has been resolved through OpenSSH (resolving every
        # saved record would mean an ``ssh -G`` per alias). Let an unauthored
        # component match whatever the requested destination resolved to, so an
        # already-saved host that inherits its account or port is not reported
        # as unsaved. Guessing the local username here produced exactly that
        # false "unsaved" prompt.
        # An empty authorship set means "no evidence" (a record that did not
        # come from a parsed Host block), never "authored nothing" — in that
        # case keep the record's own values so matching stays exact.
        authored = record.authored_directives
        inherits = (
            (lambda directive: directive not in authored)
            if authored and requested
            else (lambda directive: False)
        )
        user = str(record.username or "").strip()
        if not user:
            user = str(requested[1]) if inherits("user") else getpass.getuser()
        port = int(requested[2]) if inherits("port") else int(record.port or 22)
        proxy = ",".join(proxy_jump).casefold() or "none"
        return {
            (self._normalize_host(str(value)), user, port, proxy)
            for value in values
            if str(value or "").strip()
        }

    @staticmethod
    def _normalize_host(value: str) -> str:
        value = value.strip().casefold()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        try:
            return ipaddress.ip_address(value).compressed.casefold()
        except ValueError:
            return value.rstrip(".")

    def get_ssh_config_text(self) -> SshConfigText:
        """Return the daemon-selected active SSH config text for the raw editor."""

        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_CONFIG_READ)
        try:
            return self._repository.get_ssh_config_text()
        except CoreError as error:
            raise _map_core_error(error) from error

    def save_ssh_config_text(
        self, request: SaveSshConfigTextRequest
    ) -> SshConfigText:
        """Save raw SSH config text through the daemon's hardened write path.

        The repository reloads the SSH configuration synchronously after the
        write, so normal connection update events fire before the RPC responds
        (the polling watcher never has to notice the daemon's own write).
        """

        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_CONFIG_WRITE)
        if type(request) is not SaveSshConfigTextRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A save SSH config text request is required",
            )
        try:
            return self._repository.save_ssh_config_text(request)
        except SshPilotError:
            raise
        except CoreError as error:
            logger.warning(
                "SSH config text save rejected code=%s category=%s reason=%s message=%s",
                error.code.value,
                error.diagnostic_category,
                error.diagnostic_reason,
                error.message,
            )
            logger.debug("SSH config text save failure details", exc_info=True)
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Repository SSH config text save failed")
            raise self._persistence_error() from error

    def snapshot_connection_store(self) -> Any:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_READ)
        return self._repository.snapshot()

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
        data = self._build_create_data(request)
        try:
            record = self._repository.create_connection(data)
        except SshPilotError:
            raise
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Repository create failed")
            raise self._persistence_error() from error
        return ConnectionMutationResult(
            connection_id=record.id,
            nickname=record.nickname,
            generation=record.generation,
            display_name=self._record_to_summary(record).display_name,
        )

    def _build_create_data(self, request: CreateConnectionRequest) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "nickname": request.nickname,
            "hostname": request.hostname,
            "username": request.username,
            "port": request.port,
            "protocol": request.protocol,
            "display_name": request.display_name,
        }
        if request.config_patch:
            self._apply_config_patch(data, request.config_patch)
        if request.plugin_data:
            data.update(dict(request.plugin_data))
        return data

    def _apply_config_patch(self, data: Dict[str, Any], patch: Dict[str, Any]) -> None:
        for key, value in patch.items():
            if key in ("proxy_jump", "identity_files", "certificate_files"):
                if isinstance(value, str):
                    if key == "proxy_jump":
                        data[key] = [
                            h.strip() for h in re.split(r"[\s,]+", value) if h.strip()
                        ]
                    else:
                        data[key] = [
                            f.strip()
                            for f in re.split(r"[\r\n,]+", value)
                            if f.strip()
                        ]
                else:
                    data[key] = list(value) if value else []
            elif key == "forwarding_rules":
                from ..api.models.connections import forwarding_rule_to_dict

                try:
                    data[key] = [forwarding_rule_to_dict(r) for r in value]
                except ValueError as error:
                    raise SshPilotError(
                        ErrorCode.VALIDATION_FAILED,
                        str(error),
                    ) from error
            else:
                data[key] = value

    def duplicate_connection(
        self, connection_id: ConnectionId
    ) -> ConnectionMutationResult:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_WRITE)
        if self._repository.get_record(connection_id) is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        try:
            record = self._repository.duplicate_connection(connection_id)
        except SshPilotError:
            raise
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Repository duplicate failed")
            raise self._persistence_error(connection_id) from error
        return ConnectionMutationResult(
            connection_id=record.id,
            nickname=record.nickname,
            generation=record.generation,
            display_name=self._record_to_summary(record).display_name,
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
        record = self._repository.get_record(connection_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=connection_id,
            )
        protocol = record.protocol or "ssh"
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
        display_only = (
            request.display_name is not UNSET
            and request.display_name is not None
            and not request.config_patch
            and not request.plugin_data
            and all(
                getattr(request, name) is UNSET
                for name in ("nickname", "hostname", "username", "port")
            )
        )
        if display_only:
            try:
                previous_display_name = self._record_to_summary(record).display_name
                if request.expected_generation is None:
                    updated = self._repository.set_display_name(
                        connection_id, request.display_name
                    )
                else:
                    updated = self._repository.set_display_name(
                        connection_id,
                        request.display_name,
                        expected_generation=request.expected_generation,
                    )
            except CoreError as error:
                raise _map_core_error(error) from error
            summary = self._record_to_summary(updated)
            return ConnectionMutationResult(
                connection_id=updated.id,
                nickname=updated.nickname,
                generation=updated.generation,
                changed=previous_display_name != request.display_name.strip(),
                changed_fields=(
                    ("display_name",)
                    if previous_display_name != request.display_name.strip()
                    else ()
                ),
                display_name=summary.display_name,
            )
        before_data = dict(record.data or {})
        data = dict(record.data or {})
        compared_fields = set(request.config_patch or {})
        compared_fields.update(
            name for name in ("nickname", "hostname", "username", "port")
            if getattr(request, name) is not None and getattr(request, name) is not UNSET
        )
        if request.display_name is not UNSET and request.display_name is not None:
            data["display_name"] = request.display_name
            compared_fields.add("display_name")
        compared_fields.update(request.plugin_data or {})
        for name in ("nickname", "hostname", "username", "port"):
            value = getattr(request, name)
            if value is not None and value is not UNSET:
                data[name] = value
        if request.config_patch:
            self._apply_config_patch(data, request.config_patch)
        if request.plugin_data:
            data.update(dict(request.plugin_data))
        try:
            updated = self._repository.update_connection(
                connection_id,
                data,
                expected_generation=request.expected_generation,
            )
        except SshPilotError:
            raise
        except CoreError as error:
            logger.warning(
                "Repository update rejected code=%s category=%s reason=%s",
                error.code.value,
                error.diagnostic_category,
                error.diagnostic_reason,
            )
            logger.debug("Repository update failure details", exc_info=True)
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Repository update failed")
            raise self._persistence_error(connection_id) from error
        after_data = dict(updated.data or {})
        changed_fields = tuple(sorted(
            field for field in compared_fields
            if field not in {"id", "source", "generation"}
            and before_data.get(field) != after_data.get(field)
        ))
        return ConnectionMutationResult(
            connection_id=updated.id,
            nickname=updated.nickname,
            generation=updated.generation,
            changed=bool(changed_fields),
            changed_fields=changed_fields,
            display_name=self._record_to_summary(updated).display_name,
        )

    def delete_connection(self, request: DeleteConnectionRequest) -> DeleteConnectionResult:
        self._assert_command_thread()
        self._require_capability(Capability.CONNECTIONS_WRITE)
        if type(request) is not DeleteConnectionRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "A delete connection request is required",
            )
        if self._repository.get_record(request.connection_id) is None:
            raise SshPilotError(
                ErrorCode.CONNECTION_NOT_FOUND,
                "The requested connection does not exist",
                connection_id=request.connection_id,
            )
        try:
            self._repository.delete_connection(request.connection_id)
        except SshPilotError:
            raise
        except CoreError as error:
            raise _map_core_error(error) from error
        except Exception as error:
            logger.exception("Repository delete failed")
            raise self._persistence_error(request.connection_id) from error
        self._provider_call(
            self._secret_provider,
            "clear_session_connection_password",
            request.connection_id,
        )
        return DeleteConnectionResult(
            connection_id=request.connection_id,
            deleted=True,
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def subscribe_events(self, callback) -> Subscription:
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
        try:
            self._repository.remove_listener(self._on_repository_change)
        except Exception:
            logger.debug("Failed to detach repository listener", exc_info=True)
        self._publisher.close()

    def _on_repository_change(self, change: RepositoryChange) -> None:
        """Publish one coherent event set for a committed repository change."""
        if self._closed:
            return
        before = {c.id: c for c in change.before.connections}
        after = {c.id: c for c in change.after.connections}
        deleted = [before[cid] for cid in before if cid not in after]
        created = [after[cid] for cid in after if cid not in before]
        updated = [
            after[cid]
            for cid in after
            if cid in before and before[cid] != after[cid]
        ]
        for event_type, summaries in (
            (EventType.CONNECTION_DELETED, deleted),
            (EventType.CONNECTION_CREATED, created),
            (EventType.CONNECTION_UPDATED, updated),
        ):
            for summary in summaries:
                try:
                    self._publisher.publish(
                        event_type,
                        summary,
                        connection_id=summary.id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to publish %s connection event", event_type.value
                    )
        try:
            self._publisher.publish(
                EventType.CONNECTION_STORE_CHANGED,
                change.after,
            )
        except Exception:
            logger.exception("Failed to publish connection-store event")

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
                    raise TypeError(
                        "connection reload events require public summaries"
                    )
                self._publisher.publish(
                    event_type,
                    summary,
                    connection_id=summary.id,
                )

    def _assert_command_thread(self) -> None:
        if self._closed:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST, "The application service is closed"
            )
        if (
            not self._allow_cross_thread_commands
            and threading.get_ident() != self._owner_thread_id
        ):
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                "Application service commands must run on their owner thread",
            )

    def snapshot_connection_summaries(self) -> Tuple[ConnectionSummary, ...]:
        if self._closed:
            return ()
        return tuple(self._repository.snapshot().connections)

    def _require_capability(self, capability: Capability) -> None:
        if not self._capabilities.supports(capability):
            raise unsupported_capability(capability)

    # -- SSH overrides (in-process parity with the daemon service) --------

    def get_global_ssh_overrides(self) -> GlobalSshOverrides:
        self._require_capability(Capability.SSH_OVERRIDES_READ)
        service = self._ssh_overrides_service
        if service is None:
            raise unsupported_capability(Capability.SSH_OVERRIDES_READ)
        return service.get()

    def update_global_ssh_overrides(
        self, request: UpdateGlobalSshOverridesRequest
    ) -> GlobalSshOverrides:
        self._require_capability(Capability.SSH_OVERRIDES_WRITE)
        if type(request) is not UpdateGlobalSshOverridesRequest:
            raise TypeError(
                "request must be an UpdateGlobalSshOverridesRequest instance"
            )
        service = self._ssh_overrides_service
        if service is None:
            raise unsupported_capability(Capability.SSH_OVERRIDES_WRITE)
        return service.update(request)

    def reset_global_ssh_overrides(
        self, expected_revision: Optional[str] = None
    ) -> GlobalSshOverrides:
        self._require_capability(Capability.SSH_OVERRIDES_WRITE)
        service = self._ssh_overrides_service
        if service is None:
            raise unsupported_capability(Capability.SSH_OVERRIDES_WRITE)
        return service.reset(expected_revision=expected_revision)

    # -- daemon-only protocol surface (canonical unsupported errors) ------

    def list_keys(self, request: Any) -> None:
        raise unsupported_capability(Capability.KEYS_READ)

    def delete_key(self, request: Any) -> None:
        raise unsupported_capability(Capability.KEYS_WRITE)

    def read_public_key(self, request: Any) -> None:
        raise unsupported_capability(Capability.KEYS_READ)

    def generate_key(self, request: Any) -> None:
        raise unsupported_capability(Capability.KEYS_WRITE)

    def verify_key_passphrase(self, request: Any) -> None:
        raise unsupported_capability(Capability.KEYS_WRITE)

    # ------------------------------------------------------------------
    # DTO conversion (ConnectionRecord → public DTOs)
    # ------------------------------------------------------------------

    @staticmethod
    def _string_tuple(value: Any) -> Tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (tuple, list)):
            return tuple(str(v) for v in value if v is not None)
        return (str(value),)

    def _record_to_summary(self, record: ConnectionRecord) -> ConnectionSummary:
        """Convert one record to its public, secret-free summary.

        Group membership is derived from a single repository snapshot so the
        result matches the authoritative store. No fallback path: a missing
        record or failed snapshot raises rather than silently guessing groups.
        """
        snapshot = self._repository.snapshot()
        for summary in snapshot.connections:
            if summary.id == record.id:
                return summary
        raise CoreError(
            ErrorCode.CONNECTION_NOT_FOUND,
            "The connection is not present in the current snapshot",
        )

    def _record_to_details(self, record: ConnectionRecord) -> ConnectionDetails:
        data = record.data or {}
        summary = self._record_to_summary(record)
        try:
            auth_method = (
                AuthenticationMethod.PASSWORD
                if int(data.get("auth_method", 0) or 0) == 1
                else AuthenticationMethod.KEY
            )
        except (TypeError, ValueError):
            auth_method = AuthenticationMethod.KEY
        keyfile = str(data.get("keyfile") or "").strip()
        certificate = str(data.get("certificate") or "").strip()
        forwarding_rules = data.get("forwarding_rules") or ()
        proxy_jump = data.get("proxy_jump") or ()
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
            display_name=summary.display_name,
            aliases=record.aliases or self._string_tuple(data.get("aliases")),
            authentication_method=auth_method,
            identity_configured=bool(keyfile) or bool(data.get("identity_files")),
            certificate_configured=bool(certificate)
            or bool(data.get("certificate_files")),
            x11_forwarding=bool(data.get("x11_forwarding")),
            forwarding_rule_count=len(forwarding_rules)
            if isinstance(forwarding_rules, (list, tuple))
            else 0,
            proxy_jump=self._string_tuple(proxy_jump),
        )

    def _record_to_editor_details(
        self, record: ConnectionRecord
    ) -> ConnectionEditorDetails:
        data = record.data or {}
        details = self._record_to_details(record)
        forwarding_rules: Tuple[ForwardingRule, ...] = ()
        raw_rules = data.get("forwarding_rules") or ()
        if isinstance(raw_rules, (list, tuple)):
            converted = []
            for item in raw_rules:
                if isinstance(item, ForwardingRule):
                    converted.append(item)
                    continue
                if isinstance(item, dict):
                    try:
                        converted.append(forwarding_rule_from_dict(item))
                    except (TypeError, ValueError):
                        continue
            forwarding_rules = tuple(converted)
        preferred = data.get("preferred_authentications")
        if isinstance(preferred, (list, tuple)):
            preferred_text = ",".join(str(p) for p in preferred)
        else:
            preferred_text = str(preferred or "")
        return ConnectionEditorDetails(
            id=details.id,
            nickname=details.nickname,
            host=details.host,
            hostname=details.hostname,
            username=details.username,
            port=details.port,
            protocol=details.protocol,
            health=details.health,
            groups=details.groups,
            display_name=details.display_name,
            aliases=details.aliases,
            authentication_method=details.authentication_method,
            identity_configured=details.identity_configured,
            certificate_configured=details.certificate_configured,
            x11_forwarding=details.x11_forwarding,
            forwarding_rule_count=details.forwarding_rule_count,
            proxy_jump=details.proxy_jump,
            key_select_mode=int(data.get("key_select_mode", 0) or 0),
            identity_files=self._string_tuple(data.get("identity_files")),
            certificate_files=self._string_tuple(data.get("certificate_files")),
            identity_agent=str(data.get("identity_agent") or ""),
            add_keys_to_agent=str(data.get("add_keys_to_agent") or ""),
            pkcs11_provider=str(data.get("pkcs11_provider") or ""),
            security_key_provider=str(data.get("security_key_provider") or ""),
            pubkey_auth_no=bool(data.get("pubkey_auth_no")),
            forward_agent=bool(data.get("forward_agent")),
            forward_agent_explicit_no=bool(data.get("forward_agent_explicit_no")),
            forward_agent_target=str(data.get("forward_agent_target") or ""),
            proxy_command=str(data.get("proxy_command") or ""),
            forwarding_rules=forwarding_rules,
            pre_command=str(data.get("pre_command") or ""),
            local_command=str(data.get("local_command") or ""),
            remote_command=str(data.get("remote_command") or ""),
            request_tty=str(data.get("request_tty") or ""),
            extra_ssh_config=str(data.get("extra_ssh_config") or ""),
            identity_file_none=bool(data.get("identity_file_none")),
            x11_forwarding_explicit_no=bool(data.get("x11_forwarding_explicit_no")),
            identities_only_explicit_no=bool(
                data.get("identities_only_explicit_no")
            ),
            preferred_authentications=preferred_text,
            source=record.source or str(data.get("source") or ""),
            generation=record.generation,
            authored_directives=tuple(sorted(record.authored_directives)),
        )

    @staticmethod
    def _persistence_error(
        connection_id: Optional[ConnectionId] = None,
    ) -> SshPilotError:
        return SshPilotError(
            ErrorCode.PERSISTENCE_FAILED,
            "The connection change could not be saved",
            connection_id=connection_id,
        )


def _map_core_error(error: Any) -> SshPilotError:
    """Translate a repository ``CoreError`` into the API error vocabulary."""
    from ..core.errors import ErrorCode as CoreErrorCode

    code = getattr(error, "code", None)
    if code is CoreErrorCode.CONNECTION_NOT_FOUND:
        return SshPilotError(
            ErrorCode.CONNECTION_NOT_FOUND,
            "The requested connection does not exist",
        )
    if code is CoreErrorCode.CONNECTION_ALREADY_EXISTS:
        return SshPilotError(
            ErrorCode.CONNECTION_ALREADY_EXISTS,
            "A connection with this nickname already exists",
        )
    if code is CoreErrorCode.STALE_CONNECTION_STATE:
        return SshPilotError(
            ErrorCode.STALE_EDITOR,
            "The connection has been modified since it was last read",
        )
    if code is CoreErrorCode.VALIDATION_ERROR:
        return SshPilotError(
            ErrorCode.VALIDATION_FAILED,
            "The requested connection-store change is invalid",
        )
    if code is CoreErrorCode.CONFIG_PARSE_ERROR:
        return SshPilotError(
            ErrorCode.VALIDATION_FAILED,
            error.message or "The SSH configuration is invalid",
        )
    if code is CoreErrorCode.MUTATION_AMBIGUOUS:
        return SshPilotError(
            ErrorCode.MUTATION_AMBIGUOUS,
            "The connection-store change may have completed",
        )
    if code is CoreErrorCode.CONNECTION_STATE_IO_ERROR:
        return SshPilotError(
            ErrorCode.PERSISTENCE_FAILED,
            "The connection state could not be stored",
        )
    return SshPilotError(
        ErrorCode.PERSISTENCE_FAILED,
        "The connection change could not be saved",
    )
