"""Schema-only SFTP, forwarding, and plugin operation models."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from .common import ConnectionId, RequestId, SessionId, require_identifier


class FileEntryKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True)
class SftpEntry:
    name: str
    path: str
    kind: FileEntryKind
    size: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("SFTP entry name must not be empty")
        if not self.path:
            raise ValueError("SFTP entry path must not be empty")
        if self.size is not None and self.size < 0:
            raise ValueError("SFTP entry size must not be negative")


@dataclass(frozen=True)
class ListDirectoryRequest:
    connection_id: ConnectionId
    path: str

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if not self.path:
            raise ValueError("SFTP path must not be empty")


class ForwardKind(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    DYNAMIC = "dynamic"


class ForwardState(str, Enum):
    STARTING = "starting"
    ACTIVE = "active"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True)
class PortForwardSummary:
    id: str
    session_id: SessionId
    kind: ForwardKind
    state: ForwardState
    bind_host: str
    bind_port: int
    target_host: str = ""
    target_port: Optional[int] = None

    def __post_init__(self) -> None:
        require_identifier(self.id, "port-forward id")
        require_identifier(self.session_id, "session id")
        if not 1 <= self.bind_port <= 65535:
            raise ValueError("bind port must be between 1 and 65535")
        if self.target_port is not None and not 1 <= self.target_port <= 65535:
            raise ValueError("target port must be between 1 and 65535")


@dataclass(frozen=True)
class PluginArgument:
    """One deliberate plugin argument; secret values stay out of ``repr``."""

    name: str
    value: str = field(repr=False)
    secret: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("plugin argument name must not be empty")


@dataclass(frozen=True)
class PluginOperationRequest:
    request_id: RequestId
    plugin_id: str
    operation: str
    arguments: Tuple[PluginArgument, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request id")
        if not self.plugin_id.strip():
            raise ValueError("plugin id must not be empty")
        if not self.operation.strip():
            raise ValueError("plugin operation must not be empty")


@dataclass(frozen=True)
class PluginOperationResult:
    request_id: RequestId
    plugin_id: str
    values: Tuple[Tuple[str, str], ...] = field(default=(), repr=False)
