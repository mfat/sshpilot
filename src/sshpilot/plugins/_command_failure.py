"""Private local-command failures; no plugin API or wire contract."""

from __future__ import annotations

from enum import Enum
from typing import Optional


class LocalCommandFailureReason(str, Enum):
    EMPTY_COMMAND = "empty_command"
    HOST_EXECUTOR_UNAVAILABLE = "host_executor_unavailable"
    TIMED_OUT = "timed_out"
    START_FAILED = "start_failed"
    EXITED = "exited"


class LocalCommandFailure(RuntimeError):
    """A known reason plus technical parameters and an opaque diagnostic."""

    def __init__(
        self,
        reason: LocalCommandFailureReason,
        *,
        program: str = "",
        status: Optional[int] = None,
        diagnostic: str = "",
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.program = program
        self.status = status
        self.diagnostic = diagnostic
