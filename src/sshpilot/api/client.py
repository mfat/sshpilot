"""Typed frontend-independent sshPilot client contract."""

from typing import Any, Dict, List, Optional, Protocol

from .capabilities import Capabilities
from .events import CoreEventCallback, Subscription
from .terminal_events import (
    TerminalContinuityCallback,
    TerminalEofCallback,
    TerminalErrorCallback,
    TerminalOutputCallback,
    TerminalSubscription,
)
from .models.connections import (
    ConnectionDetails,
    ConnectionEditorDetails,
    ConnectionMutationResult,
    ConnectionSummary,
    CreateConnectionRequest,
    DeleteConnectionPasswordRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    StoreConnectionPasswordRequest,
    DeleteKeyPassphraseRequest,
    SaveSshConfigTextRequest,
    SplitConnectionRequest,
    SshConfigText,
    StoreKeyPassphraseRequest,
    UpdateConnectionRequest,
)
from .models.connection_store import (
    AddTagToConnectionsRequest,
    ConnectionStoreSnapshot,
    SetGroupColorRequest,
    PlaceGroupRequest,
    CopyConnectionToGroupRequest,
    MoveConnectionsRequest,
    RemoveConnectionFromGroupRequest,
    ReorderConnectionRequest,
    RenameTagRequest,
)
from .models.identity import (
    AgentKeyList,
    AgentKeyMutationRequest,
    AuthorizedKeyList,
    DeployKeyRequest,
    IdentityProviderRegistry,
    IdentityState,
    ListAuthorizedKeysRequest,
    RemoveAuthorizedKeyRequest,
    UpdateIdentityConfigurationRequest,
    UpdateIdentitySelectionRequest,
)
from .models.interactions import (
    InteractionClaim,
    InteractionDecisionRequest,
    InteractionId,
    InteractionSummary,
)
from .models.keys import (
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyList,
    ListKeysRequest,
    PublicKeyResult,
    ReadPublicKeyRequest,
)
from .models.secrets import (
    BitwardenStatus,
    RbwStatus,
    SecretBackendRegistry,
    SecretBackendState,
    SecretConfiguration,
    SecretOperationResult,
    SecretTransferResult,
    SecretUnlockResult,
    UpdateSecretConfigurationRequest,
)
from .models.known_hosts import (
    KnownHostsMutationResult,
    KnownHostsSnapshot,
    RemoveKnownHostEntriesRequest,
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
    BroadcastTerminalInputRequest,
    ClaimTerminalInputRequest,
    ReleaseTerminalInputRequest,
    ReplayRequest,
    ReplayResult,
    ResizeTerminalRequest,
    TerminalInput,
)
from .models.common import (
    ConnectionId,
    ForwardId,
    SessionId,
    SftpServiceId,
    TransferId,
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
    OperationId,
    OperationSummary,
    RemoteFileEntry,
    SftpChmodRequest,
    SftpCopyRequest,
    SftpDirectorySizeRequest,
    SftpPathRequest,
    SftpReadFileRequest,
    SftpReadFileResult,
    SftpRenameRequest,
    SftpReplaceFileRequest,
    SftpReplaceFileResult,
    SftpServiceSummary,
    SftpSymlinkRequest,
)
from .models.transfers import (
    CancelTransferRequest,
    StartScpTransferRequest,
    StartTransferRequest,
    TransferSummary,
)
from .models.daemon import (
    DaemonDiagnostics,
    DaemonStatus,
    DaemonStopResult,
    RestartDaemonRequest,
    StopDaemonRequest,
)
from .models.settings import (
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
)


class SshPilotClient(Protocol):
    """Synchronous commands plus a frontend-neutral event subscription."""

    def get_capabilities(self) -> Capabilities:
        ...

    def start_broadcast_command(self, request):
        ...

    def get_broadcast_command(self, operation_id):
        ...

    def cancel_broadcast_command(self, operation_id):
        ...

    def list_connections(self) -> List[ConnectionSummary]:
        ...

    def get_connection_store_snapshot(self) -> ConnectionStoreSnapshot:
        ...

    def get_connection(self, connection_id: ConnectionId) -> ConnectionDetails:
        ...

    def get_connection_editor(
        self, connection_id: ConnectionId
    ) -> ConnectionEditorDetails:
        ...

    def get_ssh_config_text(self) -> SshConfigText:
        ...

    def save_ssh_config_text(
        self, request: SaveSshConfigTextRequest
    ) -> SshConfigText:
        ...

    def create_connection(self, request: CreateConnectionRequest) -> ConnectionMutationResult:
        ...

    def update_connection(
        self,
        connection_id: ConnectionId,
        request: UpdateConnectionRequest,
    ) -> ConnectionMutationResult:
        ...

    def duplicate_connection(
        self, connection_id: ConnectionId
    ) -> ConnectionMutationResult:
        ...

    def delete_connection(self, request: DeleteConnectionRequest) -> DeleteConnectionResult:
        ...

    def store_connection_password(self, request: StoreConnectionPasswordRequest) -> bool:
        ...

    def has_connection_password(self, connection_id: ConnectionId) -> bool:
        ...

    def reveal_connection_password(self, connection_id: ConnectionId) -> bytearray:
        ...

    def delete_connection_password(self, request: DeleteConnectionPasswordRequest) -> bool:
        ...

    def store_key_passphrase(self, request: StoreKeyPassphraseRequest) -> bool:
        ...

    def delete_key_passphrase(self, request: DeleteKeyPassphraseRequest) -> bool:
        ...

    def has_key_passphrase(self, key_path: str) -> bool:
        ...

    def reveal_key_passphrase(self, key_path: str) -> bytearray:
        ...

    def get_plugin_secret(self, plugin_id: str, key: str) -> Optional[str]:
        ...

    def update_connection_metadata(
        self, connection_id: ConnectionId, meta: 'Dict[str, Any]'
    ) -> bool:
        ...

    def set_group_color(self, request: SetGroupColorRequest) -> bool:
        ...

    def place_group(self, request: PlaceGroupRequest) -> bool:
        ...

    def copy_connection_to_group(self, request: CopyConnectionToGroupRequest) -> bool:
        ...

    def remove_connection_from_group(
        self, request: RemoveConnectionFromGroupRequest
    ) -> bool:
        ...

    def reorder_connection(self, request: ReorderConnectionRequest) -> bool:
        ...

    def move_connections(self, request: MoveConnectionsRequest) -> bool:
        ...

    def rename_tag(self, request: RenameTagRequest) -> int:
        ...

    def add_tag_to_connections(self, request: AddTagToConnectionsRequest) -> int:
        ...

    def assign_connection_to_group(
        self, connection_id: ConnectionId, group_id: str
    ) -> bool:
        ...

    def create_group(
        self, name: str, parent_id: str = "", color: str = ""
    ) -> Optional[str]:
        ...

    def delete_group(self, group_id: str) -> bool:
        ...

    def rename_group(self, group_id: str, new_name: str) -> bool:
        ...

    def split_connection(
        self, request: SplitConnectionRequest
    ) -> 'ConnectionMutationResult':
        ...

    def get_daemon_status(self) -> DaemonStatus:
        ...

    def get_daemon_diagnostics(self) -> DaemonDiagnostics:
        ...

    def stop_daemon(self, request: Optional[StopDaemonRequest] = None) -> DaemonStopResult:
        ...

    def restart_daemon(
        self, request: Optional[RestartDaemonRequest] = None
    ) -> DaemonStopResult:
        ...

    def list_sessions(self) -> List[SessionSummary]:
        ...

    def get_session(self, session_id: SessionId) -> SessionSummary:
        ...

    def open_session(self, request: OpenSessionRequest) -> SessionSummary:
        ...

    def attach_session(self, request: AttachSessionRequest) -> AttachSessionResult:
        ...

    def detach_session(self, request: DetachSessionRequest) -> None:
        ...

    def close_session(self, request: CloseSessionRequest) -> None:
        ...

    def send_terminal_input(self, request: TerminalInput) -> None:
        ...

    def broadcast_terminal_input(self, request: BroadcastTerminalInputRequest) -> None:
        ...

    def claim_terminal_input(self, request: ClaimTerminalInputRequest) -> None:
        ...

    def release_terminal_input(self, request: ReleaseTerminalInputRequest) -> None:
        ...

    def resize_terminal(self, request: ResizeTerminalRequest) -> None:
        ...

    def replay_terminal(self, request: ReplayRequest) -> ReplayResult:
        ...

    def subscribe_terminal(
        self,
        session_id: SessionId,
        on_output: TerminalOutputCallback,
        *,
        on_continuity_lost: Optional[TerminalContinuityCallback] = None,
        on_eof: Optional[TerminalEofCallback] = None,
        on_error: Optional[TerminalErrorCallback] = None,
    ) -> TerminalSubscription:
        ...

    def list_interactions(self) -> List[InteractionSummary]:
        ...

    def get_interaction(self, interaction_id: InteractionId) -> InteractionSummary:
        ...

    def claim_interaction(self, interaction_id: InteractionId) -> InteractionClaim:
        ...

    def release_interaction(self, interaction_id: InteractionId) -> None:
        ...

    def respond_to_interaction(
        self,
        response: InteractionDecisionRequest,
    ) -> None:
        ...

    def cancel_interaction(self, interaction_id: InteractionId) -> None:
        ...

    def send_interaction_secret(
        self,
        interaction_id: InteractionId,
        nonce: str,
        secret: bytearray,
    ) -> None:
        ...

    def list_sftp_services(self) -> List[SftpServiceSummary]:
        ...

    def get_sftp_service(self, service_id: SftpServiceId) -> SftpServiceSummary:
        ...

    def open_sftp(self, request: OpenSftpRequest) -> SftpServiceSummary:
        ...

    def attach_sftp(self, request: AttachSftpRequest) -> SftpServiceSummary:
        ...

    def detach_sftp(self, service_id: SftpServiceId) -> None:
        ...

    def close_sftp(self, request: CloseSftpRequest) -> None:
        ...

    def sftp_list_directory(self, request: ListDirectoryRequest) -> ListDirectoryResult:
        ...

    def sftp_stat(self, request: SftpPathRequest) -> RemoteFileEntry:
        ...

    def sftp_directory_size(
        self,
        request: SftpDirectorySizeRequest,
    ) -> OperationSummary:
        ...

    def sftp_lstat(self, request: SftpPathRequest) -> RemoteFileEntry:
        ...

    def sftp_realpath(self, request: SftpPathRequest) -> str:
        ...

    def sftp_readlink(self, request: SftpPathRequest) -> str:
        ...

    def sftp_read_file(self, request: SftpReadFileRequest) -> SftpReadFileResult:
        ...

    def sftp_replace_file(self, request: SftpReplaceFileRequest) -> SftpReplaceFileResult:
        ...

    def sftp_mkdir(self, request: SftpPathRequest) -> None:
        ...

    def sftp_copy(self, request: SftpCopyRequest) -> Optional[OperationSummary]:
        ...

    def sftp_rmdir(self, request: SftpPathRequest) -> None:
        ...

    def sftp_rename(self, request: SftpRenameRequest) -> None:
        ...

    def sftp_remove(self, request: SftpPathRequest) -> Optional[OperationSummary]:
        ...

    def sftp_chmod(self, request: SftpChmodRequest) -> None:
        ...

    def sftp_symlink(self, request: SftpSymlinkRequest) -> None:
        ...

    def list_transfers(self) -> List[TransferSummary]:
        ...

    def get_transfer(self, transfer_id: TransferId) -> TransferSummary:
        ...

    def start_transfer(self, request: StartTransferRequest) -> TransferSummary:
        ...

    def start_scp_transfer(self, request: StartScpTransferRequest) -> TransferSummary:
        ...

    def cancel_transfer(self, request: CancelTransferRequest) -> None:
        ...

    def list_forwards(self) -> List[ForwardSummary]:
        ...

    def get_forward(self, forward_id: ForwardId) -> ForwardSummary:
        ...
    def open_forward(self, request: OpenForwardRequest) -> ForwardSummary:

        ...

    def claim_forward(self, request: ClaimForwardRequest) -> ForwardSummary:

        ...

    def close_forward(self, request: CloseForwardRequest) -> None:
        ...

    def list_known_hosts(self) -> KnownHostsSnapshot:
        ...

    def remove_known_host_entries(
        self,
        request: RemoveKnownHostEntriesRequest,
    ) -> KnownHostsMutationResult:
        ...

    def list_keys(self, request: ListKeysRequest) -> KeyList:
        ...

    def read_public_key(self, request: ReadPublicKeyRequest) -> PublicKeyResult:
        ...

    def generate_key(self, request: GenerateKeyRequest) -> GenerateKeyResult:
        ...

    def get_identity_providers(self) -> IdentityProviderRegistry:
        ...

    def get_identity_state(self) -> IdentityState:
        ...

    def update_identity_selection(
        self, request: UpdateIdentitySelectionRequest
    ) -> IdentityState:
        ...

    def update_identity_configuration(
        self, request: UpdateIdentityConfigurationRequest
    ) -> IdentityState:
        ...

    def list_agent_keys(self) -> AgentKeyList:
        ...

    def add_agent_key(self, request: AgentKeyMutationRequest) -> AgentKeyList:
        ...

    def remove_agent_key(self, request: AgentKeyMutationRequest) -> AgentKeyList:
        ...

    def deploy_key(self, request: DeployKeyRequest) -> OperationSummary:
        ...

    def list_authorized_keys(
        self, request: ListAuthorizedKeysRequest
    ) -> AuthorizedKeyList:
        ...

    def remove_authorized_key(
        self, request: RemoveAuthorizedKeyRequest
    ) -> OperationSummary:
        ...

    def get_operation(self, operation_id: OperationId) -> OperationSummary:
        ...

    def cancel_operation(self, operation_id: OperationId) -> OperationSummary:
        ...

    def get_global_ssh_overrides(self) -> GlobalSshOverrides:
        ...

    def update_global_ssh_overrides(
        self,
        request: UpdateGlobalSshOverridesRequest,
    ) -> GlobalSshOverrides:
        ...

    def reset_global_ssh_overrides(
        self,
        expected_revision: Optional[str] = None,
    ) -> GlobalSshOverrides:
        ...

    def get_secret_configuration(self) -> SecretConfiguration:
        ...

    def update_secret_configuration(
        self,
        request: UpdateSecretConfigurationRequest,
    ) -> SecretConfiguration:
        ...

    def get_secret_backends(self) -> SecretBackendRegistry:
        ...

    def get_secret_state(self) -> SecretBackendState:
        ...

    def update_secret_selection(
        self, backend: str, *, expected_revision: Optional[str] = None
    ) -> SecretBackendState:
        ...

    def unlock_secrets(self) -> SecretUnlockResult:
        ...

    def lock_secrets(self) -> SecretBackendState:
        ...

    def keepassxc_create_database(
        self,
        path: str,
        *,
        keyfile: Optional[str] = None,
    ) -> SecretOperationResult:
        ...

    def keepassxc_unlock(self) -> SecretOperationResult:
        ...

    def keepassxc_lock(self) -> SecretOperationResult:
        ...

    def remember_master_password(self) -> SecretOperationResult:
        ...

    def forget_master_password(self) -> SecretOperationResult:
        ...

    def bitwarden_status(self, *, force_refresh: bool = False) -> BitwardenStatus:
        ...

    def bitwarden_configure_server(self, url: str) -> BitwardenStatus:
        ...

    def bitwarden_login(
        self,
        email: str,
        *,
        twofa_method: Optional[str] = None,
    ) -> BitwardenStatus:
        ...

    def bitwarden_api_key_login(self, client_id: str) -> BitwardenStatus:
        ...

    def bitwarden_sso_login(self, identifier: Optional[str] = None) -> BitwardenStatus:
        ...

    def bitwarden_unlock(self) -> BitwardenStatus:
        ...

    def bitwarden_sync(self) -> BitwardenStatus:
        ...

    def bitwarden_lock(self) -> BitwardenStatus:
        ...

    def bitwarden_logout(self) -> BitwardenStatus:
        ...

    def rbw_status(self) -> RbwStatus:
        ...

    def rbw_configure(self, email: str, base_url: str) -> RbwStatus:
        ...

    def rbw_unlock(self) -> RbwStatus:
        ...

    def rbw_sync(self) -> RbwStatus:
        ...

    def rbw_lock(self) -> RbwStatus:
        ...

    def export_secret_backup(
        self,
        *,
        destination: str,
        connection_ids: Optional[List[str]] = None,
        options: Optional[Dict[str, Any]] = None,
        mirror_logins: bool = False,
    ) -> SecretTransferResult:
        ...

    def import_secret_backup(
        self,
        *,
        source: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> SecretTransferResult:
        ...

    def preview_backup(self, *, source: str) -> Dict[str, Any]:
        ...

    def preview_bitwarden_backup(self, *, entry_id: str) -> Dict[str, Any]:
        ...

    def preview_ssh_backup(
        self,
        *,
        connection_id: str,
        remote_dir: str,
        entry_id: str,
    ) -> Dict[str, Any]:
        ...

    def list_bitwarden_backups(self) -> List[Dict[str, str]]:
        ...

    def import_bitwarden_backup(
        self,
        *,
        entry_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> SecretTransferResult:
        ...

    def list_ssh_backups(
        self,
        *,
        connection_id: str,
        remote_dir: str,
    ) -> List[Dict[str, str]]:
        ...

    def import_ssh_backup(
        self,
        *,
        connection_id: str,
        remote_dir: str,
        entry_id: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> SecretTransferResult:
        ...

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        ...

    def close(self) -> None:
        ...
