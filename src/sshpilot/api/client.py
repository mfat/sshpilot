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
from .models.common import ConnectionId, SessionId


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

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        ...

    def close(self) -> None:
        ...
