"""Frontend-neutral daemon broadcast command models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

from .common import ConnectionId, require_identifier
from .operations import OperationId, OperationSummary, ServiceFailure

MAX_BROADCAST_TARGETS = 256
MAX_BROADCAST_COMMAND_BYTES = 65_536
MAX_BROADCAST_CONCURRENCY = 32
MAX_BROADCAST_TIMEOUT_SECONDS = 86_400.0
MAX_BROADCAST_OUTPUT_BYTES = 16_777_216
DEFAULT_BROADCAST_OUTPUT_BYTES = 1_048_576


class BroadcastFailurePolicy(str, Enum):
    CONTINUE = "continue"


class HostCommandState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class BroadcastExecutionPolicy:
    failure_policy: BroadcastFailurePolicy = BroadcastFailurePolicy.CONTINUE
    concurrency_limit: int = 4
    timeout_seconds: Optional[float] = None
    capture_stdout: bool = True
    capture_stderr: bool = True
    output_limit_bytes: int = DEFAULT_BROADCAST_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.failure_policy, BroadcastFailurePolicy):
            raise TypeError("failure_policy must be a BroadcastFailurePolicy")
        if (
            type(self.concurrency_limit) is not int
            or not 1 <= self.concurrency_limit <= MAX_BROADCAST_CONCURRENCY
        ):
            raise ValueError("concurrency_limit is outside the supported range")
        if self.timeout_seconds is not None and (
            type(self.timeout_seconds) not in (int, float)
            or isinstance(self.timeout_seconds, bool)
            or not 0 < float(self.timeout_seconds) <= MAX_BROADCAST_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds is outside the supported range")
        if type(self.capture_stdout) is not bool or type(self.capture_stderr) is not bool:
            raise TypeError("capture flags must be booleans")
        if (
            type(self.output_limit_bytes) is not int
            or not 1 <= self.output_limit_bytes <= MAX_BROADCAST_OUTPUT_BYTES
        ):
            raise ValueError("output_limit_bytes is outside the supported range")


@dataclass(frozen=True)
class BroadcastCommandRequest:
    connection_ids: Tuple[ConnectionId, ...]
    command: str = field(repr=False)
    policy: BroadcastExecutionPolicy = field(default_factory=BroadcastExecutionPolicy)

    def __post_init__(self) -> None:
        if type(self.connection_ids) is not tuple or not self.connection_ids:
            raise ValueError("broadcast requires at least one connection id")
        if len(self.connection_ids) > MAX_BROADCAST_TARGETS:
            raise ValueError("broadcast target limit exceeded")
        seen: set[str] = set()
        for connection_id in self.connection_ids:
            require_identifier(connection_id, "connection id")
            if connection_id in seen:
                raise ValueError("duplicate broadcast connection id")
            seen.add(connection_id)
        if type(self.command) is not str or not self.command.strip() or "\x00" in self.command:
            raise ValueError("broadcast command must be non-empty and contain no NUL")
        if len(self.command.encode("utf-8")) > MAX_BROADCAST_COMMAND_BYTES:
            raise ValueError("broadcast command size limit exceeded")
        if type(self.policy) is not BroadcastExecutionPolicy:
            raise TypeError("policy must be a BroadcastExecutionPolicy")


@dataclass(frozen=True)
class HostCommandResult:
    connection_id: ConnectionId
    state: HostCommandState
    exit_code: Optional[int] = None
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)
    truncated: bool = False
    failure: Optional[ServiceFailure] = None

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if not isinstance(self.state, HostCommandState):
            raise TypeError("state must be a HostCommandState")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise TypeError("exit_code must be an integer or None")
        if type(self.stdout) is not str or type(self.stderr) is not str:
            raise TypeError("captured output must be text")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a boolean")
        if self.failure is not None and type(self.failure) is not ServiceFailure:
            raise TypeError("failure must be ServiceFailure or None")


@dataclass(frozen=True)
class BroadcastCommandSummary:
    operation: OperationSummary
    targets: Tuple[HostCommandResult, ...]

    def __post_init__(self) -> None:
        if type(self.operation) is not OperationSummary:
            raise TypeError("operation must be an OperationSummary")
        if type(self.targets) is not tuple or any(
            type(item) is not HostCommandResult for item in self.targets
        ):
            raise TypeError("targets must be a tuple of HostCommandResult")


@dataclass(frozen=True)
class BroadcastCommandOutput:
    operation_id: OperationId
    connection_id: ConnectionId
    stream: str
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.operation_id, "operation id")
        require_identifier(self.connection_id, "connection id")
        if self.stream not in {"stdout", "stderr"}:
            raise ValueError("broadcast output stream is invalid")
        if type(self.text) is not str:
            raise TypeError("broadcast output must be text")
