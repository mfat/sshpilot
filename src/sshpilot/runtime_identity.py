"""Thread-safe runtime resource ID allocation (no UUIDs)."""

from __future__ import annotations

import secrets
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sshpilot.api.models.common import (
        AttachmentId,
        ClientId,
        ForwardId,
        InteractionId,
        RequestId,
        SessionId,
        SftpServiceId,
        TransferId,
    )
    from sshpilot.api.models.operations import OperationId


class RuntimeIdAllocator:
    """Thread-safe monotonic integer counter ID allocator."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self._next = 1
        self._lock = threading.Lock()

    def allocate(self) -> str:
        with self._lock:
            val = self._next
            self._next += 1
        return f"{self.prefix}-{val}"

    def _reset(self) -> None:
        with self._lock:
            self._next = 1


_SESSION_ALLOCATOR = RuntimeIdAllocator("session")
_SFTP_ALLOCATOR = RuntimeIdAllocator("sftp")
_TRANSFER_ALLOCATOR = RuntimeIdAllocator("transfer")
_FORWARD_ALLOCATOR = RuntimeIdAllocator("forward")
_INTERACTION_ALLOCATOR = RuntimeIdAllocator("interaction")
_TERMINAL_ALLOCATOR = RuntimeIdAllocator("terminal")
_REQUEST_ALLOCATOR = RuntimeIdAllocator("request")
_CLIENT_ALLOCATOR = RuntimeIdAllocator("client")
_ATTACHMENT_ALLOCATOR = RuntimeIdAllocator("attachment")
_COMMAND_BLOCK_ALLOCATOR = RuntimeIdAllocator("cmd")
_OPERATION_ALLOCATOR = RuntimeIdAllocator("operation")


def new_session_id() -> SessionId:
    from sshpilot.api.models.common import SessionId
    return SessionId(_SESSION_ALLOCATOR.allocate())


def new_sftp_id() -> SftpServiceId:
    from sshpilot.api.models.common import SftpServiceId
    return SftpServiceId(_SFTP_ALLOCATOR.allocate())


def new_transfer_id() -> TransferId:
    from sshpilot.api.models.common import TransferId
    return TransferId(_TRANSFER_ALLOCATOR.allocate())


def new_forward_id() -> ForwardId:
    from sshpilot.api.models.common import ForwardId
    return ForwardId(_FORWARD_ALLOCATOR.allocate())


def new_interaction_id() -> InteractionId:
    from sshpilot.api.models.common import InteractionId
    return InteractionId(_INTERACTION_ALLOCATOR.allocate())


def new_terminal_id() -> str:
    return _TERMINAL_ALLOCATOR.allocate()


def new_request_id() -> RequestId:
    from sshpilot.api.models.common import RequestId
    return RequestId(_REQUEST_ALLOCATOR.allocate())


def new_client_id() -> ClientId:
    from sshpilot.api.models.common import ClientId
    return ClientId(_CLIENT_ALLOCATOR.allocate())


def new_attachment_id() -> AttachmentId:
    from sshpilot.api.models.common import AttachmentId
    return AttachmentId(_ATTACHMENT_ALLOCATOR.allocate())


def new_command_block_id() -> str:
    return _COMMAND_BLOCK_ALLOCATOR.allocate()


def new_operation_id() -> "OperationId":
    from sshpilot.api.models.operations import OperationId
    return OperationId(_OPERATION_ALLOCATOR.allocate())


def new_server_instance_id() -> str:
    """Return a random boot token unique to this daemon process."""
    return f"daemon-{secrets.token_urlsafe(16)}"


def new_unique_client_id() -> ClientId:
    """Return a process-unique client ID using a random token."""
    from sshpilot.api.models.common import ClientId
    return ClientId(f"client-{secrets.token_urlsafe(12)}")
