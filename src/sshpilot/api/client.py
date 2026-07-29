"""Typed frontend-independent sshPilot client contract."""

from typing import List, Optional, Protocol

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
    ConnectionSummary,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    UpdateConnectionRequest,
)
from .models.interactions import (
    InteractionClaim,
    InteractionDecisionRequest,
    InteractionId,
    InteractionSummary,
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
from .models.common import (
    ConnectionId,
    ForwardId,
    SessionId,
    SftpServiceId,
    TransferId,
)
from .models.operations import (
    AttachSftpRequest,
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
from .models.transfers import (
    CancelTransferRequest,
    StartTransferRequest,
    TransferSummary,
)


class SshPilotClient(Protocol):
    """Synchronous commands plus a frontend-neutral event subscription."""

    def get_capabilities(self) -> Capabilities:
        ...

    def list_connections(self) -> List[ConnectionSummary]:
        ...

    def get_connection(self, connection_id: ConnectionId) -> ConnectionDetails:
        ...

    def create_connection(self, request: CreateConnectionRequest) -> ConnectionDetails:
        ...

    def update_connection(
        self,
        connection_id: ConnectionId,
        request: UpdateConnectionRequest,
    ) -> ConnectionDetails:
        ...

    def delete_connection(self, request: DeleteConnectionRequest) -> DeleteConnectionResult:
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

    def sftp_lstat(self, request: SftpPathRequest) -> RemoteFileEntry:
        ...

    def sftp_realpath(self, request: SftpPathRequest) -> str:
        ...

    def sftp_readlink(self, request: SftpPathRequest) -> str:
        ...

    def sftp_mkdir(self, request: SftpPathRequest) -> None:
        ...

    def sftp_rmdir(self, request: SftpPathRequest) -> None:
        ...

    def sftp_rename(self, request: SftpRenameRequest) -> None:
        ...

    def sftp_remove(self, request: SftpPathRequest) -> None:
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

    def cancel_transfer(self, request: CancelTransferRequest) -> None:
        ...

    def list_forwards(self) -> List[ForwardSummary]:
        ...

    def get_forward(self, forward_id: ForwardId) -> ForwardSummary:
        ...

    def open_forward(self, request: OpenForwardRequest) -> ForwardSummary:
        ...

    def close_forward(self, request: CloseForwardRequest) -> None:
        ...

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        ...

    def close(self) -> None:
        ...
