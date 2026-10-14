"""Frontend controller for AI assistant MCP access settings."""

from __future__ import annotations

import threading
from typing import Iterable, Optional, Sequence

from sshpilot.api.client import SshPilotClient
from sshpilot.api.models.settings import (
    AssistantAccessSettings,
    UpdateAssistantAccessRequest,
)


class AssistantAccessController:
    """Load and save daemon-owned assistant-access policy."""

    def __init__(self, client: SshPilotClient) -> None:
        self._client = client
        self._lock = threading.RLock()
        self._snapshot: AssistantAccessSettings | None = None

    def load(self) -> AssistantAccessSettings:
        with self._lock:
            snapshot = self._client.get_assistant_access()
            self._snapshot = snapshot
            return snapshot

    def snapshot(self) -> AssistantAccessSettings | None:
        with self._lock:
            return self._snapshot

    def save(
        self,
        *,
        enabled: bool,
        allowed_connection_ids: Sequence[str],
        operate: bool,
        mutate: bool,
        content: bool,
        http_listen: bool = False,
        http_port: int = 0,
        regenerate_token: bool = False,
        expected_revision: Optional[str] = None,
    ) -> AssistantAccessSettings:
        with self._lock:
            if expected_revision is None and self._snapshot is not None:
                expected_revision = self._snapshot.revision
            snapshot = self._client.set_assistant_access(
                UpdateAssistantAccessRequest(
                    enabled=enabled,
                    allowed_connection_ids=tuple(allowed_connection_ids),
                    operate=operate,
                    mutate=mutate,
                    content=content,
                    http_listen=http_listen,
                    http_port=http_port,
                    regenerate_token=regenerate_token,
                    expected_revision=expected_revision,
                )
            )
            self._snapshot = snapshot
            return snapshot

    def set_connection_allowed(
        self,
        connection_id: str,
        allowed: bool,
        *,
        known_ids: Iterable[str] | None = None,
    ) -> AssistantAccessSettings:
        current = self.load()
        ids = set(current.allowed_connection_ids)
        if allowed:
            ids.add(connection_id)
        else:
            ids.discard(connection_id)
        if known_ids is not None:
            known = set(known_ids)
            ids = {item for item in ids if item in known}
        return self.save(
            enabled=current.enabled or allowed,
            allowed_connection_ids=sorted(ids),
            operate=current.operate,
            mutate=current.mutate,
            content=current.content,
            http_listen=current.http_listen,
            http_port=current.http_port,
            expected_revision=current.revision,
        )
