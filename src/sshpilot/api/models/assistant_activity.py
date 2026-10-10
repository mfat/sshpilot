"""Models for AI assistant MCP activity visible in the SSH Pilot UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from .common import ConnectionId, utc_now


class AssistantActivityState(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class AssistantActivityEntry:
    """One MCP tool invocation recorded for the AI Activity panel."""

    activity_id: str
    tool: str
    state: AssistantActivityState
    summary: str
    timestamp: datetime
    connection_id: Optional[ConnectionId] = None
    detail: str = ""
    error: str = ""
    level: str = "read"

    def __post_init__(self) -> None:
        if type(self.activity_id) is not str or not self.activity_id.strip():
            raise ValueError("activity_id must be a non-empty string")
        if type(self.tool) is not str or not self.tool.strip():
            raise ValueError("tool must be a non-empty string")
        if type(self.state) is not AssistantActivityState:
            raise TypeError("state must be an AssistantActivityState")
        if type(self.summary) is not str:
            raise TypeError("summary must be a string")
        if type(self.timestamp) is not datetime:
            raise TypeError("timestamp must be a datetime")
        if self.connection_id is not None and (
            type(self.connection_id) is not str or not self.connection_id.strip()
        ):
            raise ValueError("connection_id must be a non-empty string or None")
        if type(self.detail) is not str or type(self.error) is not str:
            raise TypeError("detail and error must be strings")
        if type(self.level) is not str or self.level not in {"read", "operate", "mutate"}:
            raise ValueError("level must be read, operate, or mutate")


@dataclass(frozen=True)
class AssistantActivityList:
    """Ring-buffer snapshot of recent assistant activity."""

    entries: tuple[AssistantActivityEntry, ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple:
            raise TypeError("entries must be a tuple")
        for entry in self.entries:
            if type(entry) is not AssistantActivityEntry:
                raise TypeError("entries must contain AssistantActivityEntry values")


@dataclass(frozen=True)
class RecordAssistantActivityRequest:
    """Append or update an assistant activity entry."""

    tool: str
    state: AssistantActivityState
    summary: str
    activity_id: Optional[str] = None
    connection_id: Optional[ConnectionId] = None
    detail: str = ""
    error: str = ""
    level: str = "operate"

    def __post_init__(self) -> None:
        if type(self.tool) is not str or not self.tool.strip():
            raise ValueError("tool must be a non-empty string")
        if type(self.state) is not AssistantActivityState:
            raise TypeError("state must be an AssistantActivityState")
        if type(self.summary) is not str:
            raise TypeError("summary must be a string")
        if self.activity_id is not None and (
            type(self.activity_id) is not str or not self.activity_id.strip()
        ):
            raise ValueError("activity_id must be a non-empty string or None")
        if self.connection_id is not None and (
            type(self.connection_id) is not str or not self.connection_id.strip()
        ):
            raise ValueError("connection_id must be a non-empty string or None")
        if type(self.detail) is not str or type(self.error) is not str:
            raise TypeError("detail and error must be strings")
        if type(self.level) is not str or self.level not in {"read", "operate", "mutate"}:
            raise ValueError("level must be read, operate, or mutate")


def new_activity_timestamp() -> datetime:
    return utc_now()
