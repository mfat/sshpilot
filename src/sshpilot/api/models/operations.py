"""SFTP service, remote filesystem, and port-forward models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from typing import Dict, NewType, Optional, Tuple

from .common import (
    ClientId,
    ConnectionId,
    ForwardId,
    RequestId,
    SessionId,
    SftpServiceId,
    require_identifier,
    utc_now,
)


@dataclass(frozen=True)
class ServiceFailure:
    """Frontend-safe failure payload for extended SSH services."""

    code: str
    message: str

    def __post_init__(self) -> None:
        require_identifier(self.code, "service failure code")
        require_identifier(self.message, "service failure message")


class SftpServiceState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class RemoteFileType(str, Enum):
    REGULAR = "regular"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    SOCKET = "socket"
    FIFO = "fifo"
    BLOCK = "block"
    CHARACTER = "character"
    UNKNOWN = "unknown"


# Backward-compatible schema alias used by older tests/docs.
class FileEntryKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True)
class RemoteFileEntry:
    name: str
    path: str
    file_type: RemoteFileType
    size: Optional[int] = None
    mode: Optional[int] = None
    uid: Optional[int] = None
    gid: Optional[int] = None
    modified_at: Optional[datetime] = None
    link_target: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("remote file entry name must not be empty")
        if not self.path:
            raise ValueError("remote file entry path must not be empty")
        if "\x00" in self.name or "\x00" in self.path:
            raise ValueError("remote file entry must not contain NUL")
        if not isinstance(self.file_type, RemoteFileType):
            raise TypeError("remote file type must be a RemoteFileType")
        if self.size is not None and (type(self.size) is not int or self.size < 0):
            raise ValueError("remote file size must be a non-negative integer or None")
        if self.mode is not None and (type(self.mode) is not int or self.mode < 0):
            raise ValueError("remote file mode must be a non-negative integer or None")
        if self.uid is not None and type(self.uid) is not int:
            raise TypeError("remote file uid must be an integer or None")
        if self.gid is not None and type(self.gid) is not int:
            raise TypeError("remote file gid must be an integer or None")
        if self.modified_at is not None and (
            type(self.modified_at) is not datetime or self.modified_at.tzinfo is None
        ):
            raise ValueError("remote file modified_at must be timezone-aware or None")
        if self.link_target is not None and type(self.link_target) is not str:
            raise TypeError("remote link target must be a string or None")
        if self.link_target is not None and "\x00" in self.link_target:
            raise ValueError("remote link target must not contain NUL")


@dataclass(frozen=True)
class SftpEntry:
    """Legacy schema-compatible SFTP entry; prefer :class:`RemoteFileEntry`."""

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
class SftpServiceSummary:
    id: SftpServiceId
    connection_id: ConnectionId
    state: SftpServiceState
    created_at: datetime = field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    attachment_count: int = 0
    owner_client_id: Optional[ClientId] = None
    failure: Optional[ServiceFailure] = None

    def __post_init__(self) -> None:
        require_identifier(self.id, "SFTP service id")
        require_identifier(self.connection_id, "connection id")
        if not isinstance(self.state, SftpServiceState):
            raise TypeError("SFTP service state must be a SftpServiceState")
        if type(self.created_at) is not datetime or self.created_at.tzinfo is None:
            raise ValueError("SFTP service creation time must be timezone-aware")
        if self.started_at is not None and (
            type(self.started_at) is not datetime or self.started_at.tzinfo is None
        ):
            raise ValueError("SFTP service started_at must be timezone-aware or None")
        if self.closed_at is not None and (
            type(self.closed_at) is not datetime or self.closed_at.tzinfo is None
        ):
            raise ValueError("SFTP service closed_at must be timezone-aware or None")
        if type(self.attachment_count) is not int or self.attachment_count < 0:
            raise ValueError("SFTP attachment count must not be negative")
        if self.owner_client_id is not None:
            require_identifier(self.owner_client_id, "SFTP owner client id")
        if self.failure is not None and type(self.failure) is not ServiceFailure:
            raise TypeError("SFTP failure must be ServiceFailure or None")


@dataclass(frozen=True)
class OpenSftpRequest:
    connection_id: ConnectionId

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class CloseSftpRequest:
    service_id: SftpServiceId

    def __post_init__(self) -> None:
        require_identifier(self.service_id, "SFTP service id")


@dataclass(frozen=True)
class AttachSftpRequest:
    service_id: SftpServiceId

    def __post_init__(self) -> None:
        require_identifier(self.service_id, "SFTP service id")


@dataclass(frozen=True)
class ListDirectoryRequest:
    connection_id: ConnectionId
    path: str
    service_id: Optional[SftpServiceId] = None
    cursor: Optional[str] = None
    limit: Optional[int] = None

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if not self.path:
            raise ValueError("SFTP path must not be empty")
        if "\x00" in self.path:
            raise ValueError("SFTP path must not contain NUL")
        if self.service_id is not None:
            require_identifier(self.service_id, "SFTP service id")
        if self.limit is not None and (type(self.limit) is not int or self.limit <= 0):
            raise ValueError("SFTP list limit must be a positive integer or None")
        if self.cursor is not None and type(self.cursor) is not str:
            raise TypeError("SFTP list cursor must be a string or None")


@dataclass(frozen=True)
class ListDirectoryResult:
    path: str
    entries: Tuple[RemoteFileEntry, ...]
    truncated: bool = False
    next_cursor: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("list directory path must not be empty")
        if type(self.entries) is not tuple:
            raise TypeError("list directory entries must be a tuple")
        for entry in self.entries:
            if type(entry) is not RemoteFileEntry:
                raise TypeError("list directory entries must be RemoteFileEntry")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a boolean")


@dataclass(frozen=True)
class SftpPathRequest:
    service_id: SftpServiceId
    path: str
    recursive: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.service_id, "SFTP service id")
        if not self.path:
            raise ValueError("SFTP path must not be empty")
        if "\x00" in self.path:
            raise ValueError("SFTP path must not contain NUL")
        if type(self.recursive) is not bool:
            raise ValueError("SFTP path recursive flag must be a bool")


class SftpFileTarget(str, Enum):
    REMOTE = "remote"
    LOCAL_AUTHORIZED_KEYS = "local_authorized_keys"


class SftpFileAccess(str, Enum):
    NORMAL = "normal"
    SUDO = "sudo"


@dataclass(frozen=True)
class SftpReadFileRequest:
    target: SftpFileTarget
    path: str
    service_id: Optional[SftpServiceId] = None
    access: SftpFileAccess = SftpFileAccess.NORMAL

    def __post_init__(self) -> None:
        if not isinstance(self.target, SftpFileTarget):
            raise TypeError("SFTP file target must be a SftpFileTarget")
        if not self.path or "\x00" in self.path:
            raise ValueError("SFTP file path must be a non-empty safe string")
        if not isinstance(self.access, SftpFileAccess):
            raise TypeError("SFTP file access must be a SftpFileAccess")
        if self.access is SftpFileAccess.SUDO and self.target is not SftpFileTarget.REMOTE:
            raise ValueError("privileged file access requires the remote file target")
        if self.target is SftpFileTarget.REMOTE and self.service_id is None:
            raise ValueError("remote file reads require an SFTP service")
        if self.target is SftpFileTarget.LOCAL_AUTHORIZED_KEYS:
            if self.service_id is not None:
                raise ValueError("local authorized_keys reads must not include an SFTP service")
            if self.path != "~/.ssh/authorized_keys":
                raise ValueError("only the local authorized_keys target is supported")


@dataclass(frozen=True)
class SftpReadFileResult:
    target: SftpFileTarget
    path: str
    content: str = field(repr=False)
    exists: bool
    revision: str
    size: int
    mode: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, SftpFileTarget):
            raise TypeError("SFTP file target must be a SftpFileTarget")
        if not self.path or "\x00" in self.path:
            raise ValueError("SFTP file result path must be safe")
        if type(self.content) is not str:
            raise TypeError("SFTP file content must be text")
        if type(self.exists) is not bool:
            raise TypeError("SFTP file exists must be a boolean")
        require_identifier(self.revision, "file revision")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("SFTP file size must be non-negative")
        if self.mode is not None and (type(self.mode) is not int or self.mode < 0):
            raise ValueError("SFTP file mode must be non-negative or None")
        if not self.exists and (self.content or self.size or self.mode is not None):
            raise ValueError("missing files must not carry content or metadata")


@dataclass(frozen=True)
class SftpReplaceFileRequest:
    target: SftpFileTarget
    path: str
    content: str = field(repr=False)
    expected_revision: str
    backup: bool = True
    service_id: Optional[SftpServiceId] = None
    access: SftpFileAccess = SftpFileAccess.NORMAL

    def __post_init__(self) -> None:
        if not isinstance(self.target, SftpFileTarget):
            raise TypeError("SFTP file target must be a SftpFileTarget")
        if not self.path or "\x00" in self.path:
            raise ValueError("SFTP file path must be a non-empty safe string")
        if type(self.content) is not str or "\x00" in self.content:
            raise ValueError("SFTP file content must be safe text")
        require_identifier(self.expected_revision, "expected file revision")
        if type(self.backup) is not bool:
            raise TypeError("SFTP file backup must be a boolean")
        if not isinstance(self.access, SftpFileAccess):
            raise TypeError("SFTP file access must be a SftpFileAccess")
        if self.access is SftpFileAccess.SUDO and self.target is not SftpFileTarget.REMOTE:
            raise ValueError("privileged file access requires the remote file target")
        if self.target is SftpFileTarget.REMOTE and self.service_id is None:
            raise ValueError("remote file replacements require an SFTP service")
        if self.target is SftpFileTarget.LOCAL_AUTHORIZED_KEYS:
            if self.service_id is not None:
                raise ValueError("local authorized_keys replacements must not include an SFTP service")
            if self.path != "~/.ssh/authorized_keys":
                raise ValueError("only the local authorized_keys target is supported")


@dataclass(frozen=True)
class SftpReplaceFileResult:
    target: SftpFileTarget
    path: str
    revision: str
    size: int
    backup_path: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, SftpFileTarget):
            raise TypeError("SFTP file target must be a SftpFileTarget")
        if not self.path:
            raise ValueError("SFTP replacement path must not be empty")
        require_identifier(self.revision, "new file revision")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("SFTP replacement size must be non-negative")
        if self.backup_path is not None and "\x00" in self.backup_path:
            raise ValueError("SFTP backup path must not contain NUL")


@dataclass(frozen=True)
class SftpCreateFileRequest:
    """Create an empty remote file within one SFTP service.

    The daemon owns the create: no frontend temporary file or fake upload is
    involved, and already-exists / permission / path failures surface as
    structured errors.
    """

    service_id: SftpServiceId
    path: str

    def __post_init__(self) -> None:
        require_identifier(self.service_id, "SFTP service id")
        if not self.path or "\x00" in self.path:
            raise ValueError("SFTP create path must be a non-empty safe string")


@dataclass(frozen=True)
class SftpCreateFileResult:
    path: str
    mode: int = 0o644

    def __post_init__(self) -> None:
        if not self.path or "\x00" in self.path:
            raise ValueError("SFTP create result path must be safe")
        if type(self.mode) is not int or self.mode < 0:
            raise ValueError("SFTP create result mode must be non-negative")


@dataclass(frozen=True)
class SftpDirectorySizeRequest:
    """Summarise a remote directory tree within one SFTP service."""

    service_id: SftpServiceId
    path: str

    def __post_init__(self) -> None:
        require_identifier(self.service_id, "SFTP service id")
        if not self.path or "\x00" in self.path:
            raise ValueError("SFTP size path must be a non-empty safe string")


@dataclass(frozen=True)
class SftpDirectorySizeResult:
    path: str
    size_bytes: int
    file_count: int
    directory_count: int

    def __post_init__(self) -> None:
        if not self.path or "\x00" in self.path:
            raise ValueError("SFTP size result path must be safe")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("SFTP size result size must be a non-negative integer")
        if type(self.file_count) is not int or self.file_count < 0:
            raise ValueError("SFTP size result file count must be non-negative")
        if type(self.directory_count) is not int or self.directory_count < 0:
            raise ValueError("SFTP size result directory count must be non-negative")


@dataclass(frozen=True)
class SftpRenameRequest:
    service_id: SftpServiceId
    source_path: str
    destination_path: str
    overwrite: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.service_id, "SFTP service id")
        if not self.source_path or not self.destination_path:
            raise ValueError("SFTP rename paths must not be empty")
        if "\x00" in self.source_path or "\x00" in self.destination_path:
            raise ValueError("SFTP rename paths must not contain NUL")
        if type(self.overwrite) is not bool:
            raise TypeError("overwrite must be a boolean")


@dataclass(frozen=True)
class SftpCopyRequest:
    """Copy or move a remote SFTP path within one service."""

    service_id: SftpServiceId
    source_path: str
    destination_path: str
    recursive: bool = False
    move: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.service_id, "SFTP service id")
        if not self.source_path or not self.destination_path:
            raise ValueError("SFTP copy paths must not be empty")
        if "\x00" in self.source_path or "\x00" in self.destination_path:
            raise ValueError("SFTP copy paths must not contain NUL")
        if self.source_path == self.destination_path:
            raise ValueError("SFTP copy source and destination must differ")
        if type(self.recursive) is not bool or type(self.move) is not bool:
            raise TypeError("SFTP copy flags must be booleans")


@dataclass(frozen=True)
class SftpChmodRequest:
    service_id: SftpServiceId
    path: str
    mode: int

    def __post_init__(self) -> None:
        require_identifier(self.service_id, "SFTP service id")
        if not self.path:
            raise ValueError("SFTP path must not be empty")
        if "\x00" in self.path:
            raise ValueError("SFTP path must not contain NUL")
        if type(self.mode) is not int or self.mode < 0:
            raise ValueError("mode must be a non-negative integer")


@dataclass(frozen=True)
class SftpSymlinkRequest:
    service_id: SftpServiceId
    target_path: str
    link_path: str

    def __post_init__(self) -> None:
        require_identifier(self.service_id, "SFTP service id")
        if not self.target_path or not self.link_path:
            raise ValueError("SFTP symlink paths must not be empty")
        if "\x00" in self.target_path or "\x00" in self.link_path:
            raise ValueError("SFTP symlink paths must not contain NUL")


class ForwardType(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    DYNAMIC = "dynamic"


# Backward-compatible alias.
ForwardKind = ForwardType


class ForwardState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    # Legacy schema values retained for older readers.
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ForwardSummary:
    id: ForwardId
    connection_id: ConnectionId
    type: ForwardType
    state: ForwardState
    bind_host: str
    bind_port: int
    destination_host: Optional[str] = None
    destination_port: Optional[int] = None
    created_at: datetime = field(default_factory=utc_now)
    active_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    owner_client_id: Optional[ClientId] = None
    failure: Optional[ServiceFailure] = None
    # Optional link to a terminal session when the forward is terminal-bound.
    session_id: Optional[SessionId] = None

    def __post_init__(self) -> None:
        require_identifier(self.id, "forward id")
        require_identifier(self.connection_id, "connection id")
        if not isinstance(self.type, ForwardType):
            raise TypeError("forward type must be a ForwardType")
        if not isinstance(self.state, ForwardState):
            raise TypeError("forward state must be a ForwardState")
        if type(self.bind_host) is not str or not self.bind_host.strip():
            raise ValueError("bind host must be a non-empty string")
        if not 0 <= self.bind_port <= 65535:
            raise ValueError("bind port must be between 0 and 65535")
        if self.type is ForwardType.DYNAMIC:
            if self.destination_host is not None or self.destination_port is not None:
                raise ValueError("dynamic forwards must not include a destination")
        else:
            if self.destination_host is None or not str(self.destination_host).strip():
                raise ValueError("destination host is required for local/remote forwards")
            if self.destination_port is None or not 1 <= self.destination_port <= 65535:
                raise ValueError("destination port must be between 1 and 65535")
        if type(self.created_at) is not datetime or self.created_at.tzinfo is None:
            raise ValueError("forward creation time must be timezone-aware")
        if self.active_at is not None and (
            type(self.active_at) is not datetime or self.active_at.tzinfo is None
        ):
            raise ValueError("forward active_at must be timezone-aware or None")
        if self.closed_at is not None and (
            type(self.closed_at) is not datetime or self.closed_at.tzinfo is None
        ):
            raise ValueError("forward closed_at must be timezone-aware or None")
        if self.owner_client_id is not None:
            require_identifier(self.owner_client_id, "forward owner client id")
        if self.failure is not None and type(self.failure) is not ServiceFailure:
            raise TypeError("forward failure must be ServiceFailure or None")
        if self.session_id is not None:
            require_identifier(self.session_id, "session id")


@dataclass(frozen=True)
class PortForwardSummary:
    """Legacy schema-only forward snapshot; prefer :class:`ForwardSummary`."""

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
class OpenForwardRequest:
    connection_id: ConnectionId
    type: ForwardType
    bind_host: str
    bind_port: int
    destination_host: Optional[str] = None
    destination_port: Optional[int] = None

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if not isinstance(self.type, ForwardType):
            raise TypeError("forward type must be a ForwardType")
        if type(self.bind_host) is not str or not self.bind_host.strip():
            raise ValueError("bind host must be a non-empty string")
        if not 0 <= self.bind_port <= 65535:
            raise ValueError("bind port must be between 0 and 65535")
        if self.type is ForwardType.DYNAMIC:
            if self.destination_host is not None or self.destination_port is not None:
                raise ValueError("dynamic forwards must not include a destination")
        else:
            if not self.destination_host or not str(self.destination_host).strip():
                raise ValueError("destination host is required")
            if self.destination_port is None or not 1 <= self.destination_port <= 65535:
                raise ValueError("destination port must be between 1 and 65535")


@dataclass(frozen=True)
class CloseForwardRequest:
    forward_id: ForwardId

    def __post_init__(self) -> None:
        require_identifier(self.forward_id, "forward id")


@dataclass(frozen=True)
class ClaimForwardRequest:
    forward_id: ForwardId

    def __post_init__(self) -> None:
        require_identifier(self.forward_id, "forward id")


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


# ---------------------------------------------------------------------------
# Minimal shared operation lifecycle
# ---------------------------------------------------------------------------
#
# User-visible daemon commands that may take time (public-key deployment,
# remote authorized-key mutations, later SCP/broadcast work) share one small
# lifecycle: an opaque id, a kind, a queued/running/succeeded/failed/cancelled
# state, a safe message, start/finish timestamps and optional safe progress.
# There is deliberately no workflow language, dependency graph, resumable
# persistence, or plugin action framework here.

OperationId = NewType("OperationId", str)


class OperationKind(str, Enum):
    BROADCAST_COMMAND = "broadcast_command"
    KEY_DEPLOYMENT = "key_deployment"
    AUTHORIZED_KEY_REMOVAL = "authorized_key_removal"
    SFTP_DIRECTORY_SIZE = "sftp_directory_size"
    SFTP_REMOVE_TREE = "sftp_remove_tree"
    SFTP_COPY_TREE = "sftp_copy_tree"


class OperationState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_OPERATION_STATES = frozenset(
    {OperationState.SUCCEEDED, OperationState.FAILED, OperationState.CANCELLED}
)


def is_terminal_operation_state(state: OperationState) -> bool:
    """Whether *state* ends the operation lifecycle."""
    return state in _TERMINAL_OPERATION_STATES


def is_valid_operation_transition(
    current: OperationState, target: OperationState
) -> bool:
    """Valid transitions for the minimal operation lifecycle."""
    if not isinstance(current, OperationState) or not isinstance(
        target, OperationState
    ):
        return False
    if current is OperationState.QUEUED:
        return target in (
            OperationState.RUNNING,
            OperationState.FAILED,
            OperationState.CANCELLED,
        )
    if current is OperationState.RUNNING:
        return target in _TERMINAL_OPERATION_STATES
    return False


@dataclass(frozen=True)
class OperationSummary:
    """Frontend-safe snapshot of one daemon operation."""

    operation_id: OperationId
    kind: OperationKind
    state: OperationState
    message: str
    created_at: datetime
    connection_id: Optional[ConnectionId] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    progress: Optional[float] = None
    owner_client_id: Optional[ClientId] = None
    failure: Optional[ServiceFailure] = None
    result: Optional[Dict] = None

    def __post_init__(self) -> None:
        require_identifier(self.operation_id, "operation id")
        if not isinstance(self.kind, OperationKind):
            raise TypeError("operation kind must be an OperationKind")
        if not isinstance(self.state, OperationState):
            raise TypeError("operation state must be an OperationState")
        if type(self.message) is not str or "\x00" in self.message:
            raise ValueError("operation message must be a string without NUL")
        if self.connection_id is not None:
            require_identifier(self.connection_id, "connection id")
        if type(self.created_at) is not datetime or self.created_at.tzinfo is None:
            raise ValueError("operation created_at must be timezone-aware")
        if self.started_at is not None and (
            type(self.started_at) is not datetime
            or self.started_at.tzinfo is None
        ):
            raise ValueError("operation started_at must be timezone-aware or None")
        if self.finished_at is not None and (
            type(self.finished_at) is not datetime
            or self.finished_at.tzinfo is None
        ):
            raise ValueError("operation finished_at must be timezone-aware or None")
        if self.progress is not None and (
            type(self.progress) is not float
            or not isfinite(self.progress)
            or not 0.0 <= self.progress <= 1.0
        ):
            raise ValueError("operation progress must be between 0.0 and 1.0 or None")
        if self.owner_client_id is not None:
            require_identifier(self.owner_client_id, "operation owner client id")
        if self.failure is not None and type(self.failure) is not ServiceFailure:
            raise TypeError("operation failure must be ServiceFailure or None")
        if self.result is not None and type(self.result) is not dict:
            raise TypeError("operation result must be a mapping or None")
