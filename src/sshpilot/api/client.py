"""Typed frontend-independent sshPilot client contract."""

from typing import List, Protocol

from .capabilities import Capabilities
from .events import CoreEventCallback, Subscription
from .models.connections import (
    ConnectionDetails,
    ConnectionSummary,
    CreateConnectionRequest,
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
from .models.terminal import ResizeTerminalRequest, TerminalInput
from .models.common import ConnectionId


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

    def delete_connection(self, connection_id: ConnectionId) -> DeleteConnectionResult:
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

    def resize_terminal(self, request: ResizeTerminalRequest) -> None:
        ...

    def respond_to_interaction(self, response: InteractionResponse) -> None:
        ...

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        ...

    def close(self) -> None:
        ...

