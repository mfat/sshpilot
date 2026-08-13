"""Daemon-owned known-hosts service (headless, revision-checked).

Owns authoritative access to the known-hosts file: listing parses the exact
bytes and removal commits a batched mutation against a revision loaded by the
caller. The service never exposes the filesystem path.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.known_hosts import (
    KnownHostEntryId,
    KnownHostEntrySummary,
    KnownHostsMutationResult,
    KnownHostsSnapshot,
    RemoveKnownHostEntriesRequest,
)
from sshpilot.core.known_hosts.document import (
    parse_known_hosts_document,
    remove_known_hosts_document_entries,
)
from sshpilot.core.known_hosts.file_io import (
    atomic_write_known_hosts_bytes,
    read_known_hosts_bytes,
)


class KnownHostsService:
    """Authoritative, lock-serialized known-hosts document service."""

    def __init__(self, path_resolver: Callable[[], Path]) -> None:
        self._path_resolver = path_resolver
        self._lock = threading.RLock()

    def list_known_hosts(self) -> KnownHostsSnapshot:
        path = self._path_resolver()
        content = read_known_hosts_bytes(path)
        document = parse_known_hosts_document(content)
        return _document_snapshot(document)

    def remove_known_host_entries(
        self,
        request: RemoveKnownHostEntriesRequest,
    ) -> KnownHostsMutationResult:
        with self._lock:
            path = self._path_resolver()
            current = parse_known_hosts_document(read_known_hosts_bytes(path))
            if current.revision != request.revision:
                raise SshPilotError(
                    ErrorCode.STALE_EDITOR,
                    "The known-hosts file changed since it was loaded",
                    retryable=True,
                    details={
                        "resource": "known_hosts",
                        "expected_revision": request.revision,
                        "current_revision": current.revision,
                    },
                )
            entry_ids = tuple(str(item) for item in request.entry_ids)
            try:
                content = remove_known_hosts_document_entries(current, entry_ids)
            except ValueError as exc:
                raise SshPilotError(
                    ErrorCode.VALIDATION_FAILED,
                    "The known-hosts removal request is invalid",
                    retryable=False,
                    details={"resource": "known_hosts"},
                ) from exc
            atomic_write_known_hosts_bytes(path, content)
            refreshed = parse_known_hosts_document(read_known_hosts_bytes(path))
            return KnownHostsMutationResult(
                revision=refreshed.revision,
                removed_count=len(entry_ids),
                entries=tuple(
                    KnownHostEntrySummary(
                        entry_id=KnownHostEntryId(entry.entry_id),
                        hostname=entry.parsed.hostname,
                        key_type=entry.parsed.key_type,
                        display_line=entry.parsed.line,
                    )
                    for entry in refreshed.entries
                ),
            )


def _document_snapshot(document) -> KnownHostsSnapshot:
    return KnownHostsSnapshot(
        revision=document.revision,
        entries=tuple(
            KnownHostEntrySummary(
                entry_id=KnownHostEntryId(entry.entry_id),
                hostname=entry.parsed.hostname,
                key_type=entry.parsed.key_type,
                display_line=entry.parsed.line,
            )
            for entry in document.entries
        ),
    )
