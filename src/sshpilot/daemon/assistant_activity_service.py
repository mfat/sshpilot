"""Ring buffer of AI assistant MCP tool activity for the GTK UI."""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from typing import Callable, Deque, Optional

from sshpilot.api.models.assistant_activity import (
    AssistantActivityEntry,
    AssistantActivityList,
    AssistantActivityState,
    RecordAssistantActivityRequest,
    new_activity_timestamp,
)

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY = 200


class AssistantActivityService:
    """Thread-safe recent-activity log for runtime MCP tool calls."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        on_entry: Optional[Callable[[AssistantActivityEntry], None]] = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._lock = threading.RLock()
        self._entries: Deque[AssistantActivityEntry] = deque(maxlen=capacity)
        self._by_id: dict[str, AssistantActivityEntry] = {}
        self._on_entry = on_entry

    def set_on_entry(
        self, callback: Optional[Callable[[AssistantActivityEntry], None]]
    ) -> None:
        with self._lock:
            self._on_entry = callback

    def list(self) -> AssistantActivityList:
        with self._lock:
            return AssistantActivityList(entries=tuple(self._entries))

    def record(self, request: RecordAssistantActivityRequest) -> AssistantActivityEntry:
        if type(request) is not RecordAssistantActivityRequest:
            raise TypeError("a RecordAssistantActivityRequest is required")
        with self._lock:
            activity_id = request.activity_id or f"asst-{uuid.uuid4().hex[:12]}"
            existing = self._by_id.get(activity_id)
            entry = AssistantActivityEntry(
                activity_id=activity_id,
                tool=request.tool.strip(),
                state=request.state,
                summary=request.summary,
                timestamp=new_activity_timestamp(),
                connection_id=request.connection_id,
                detail=request.detail,
                error=request.error,
                level=request.level,
            )
            if existing is not None:
                try:
                    self._entries.remove(existing)
                except ValueError:
                    pass
            self._entries.append(entry)
            self._by_id = {item.activity_id: item for item in self._entries}
        self._notify(entry)
        return entry

    def _notify(self, entry: AssistantActivityEntry) -> None:
        callback = self._on_entry
        if callback is None:
            return
        try:
            callback(entry)
        except Exception:  # noqa: BLE001
            logger.exception("assistant activity on_entry callback failed")
