"""GTK-free SSH-key controller.

A stateful frontend controller over a daemon-backed ``SshPilotClient`` for one
semantic key-store scope. No GTK/GI, filesystem, ``KeyService``, or path
resolver usage. One ``RLock`` guards the busy flag and the cached ``KeyList``;
client RPCs run *outside* the lock so a blocked request never stalls other
callers. A successful generation refreshes the cached list without duplicating
ids, and passphrases are never retained after the request returns.
"""
from __future__ import annotations

import threading

from sshpilot.api.client import SshPilotClient
from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    ListKeysRequest,
    PublicKeyResult,
    ReadPublicKeyRequest,
)


class KeyController:
    """Stateful frontend controller for the daemon SSH-key API."""

    def __init__(self, client: SshPilotClient, scope: KeyStoreScope) -> None:
        self._client = client
        self._scope = scope
        self._lock = threading.RLock()
        self._busy = False
        self._cached: KeyList | None = None

    def _enter_operation(self) -> None:
        with self._lock:
            if self._busy:
                raise RuntimeError("an SSH-key operation is already in progress")
            self._busy = True

    def list_keys(self) -> KeyList:
        self._enter_operation()
        try:
            key_list = self._client.list_keys(
                ListKeysRequest(scope=self._scope)
            )
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._cached = key_list
            self._busy = False
        return key_list

    def read_public_key(self, key_id: KeyId) -> PublicKeyResult:
        self._enter_operation()
        try:
            result = self._client.read_public_key(
                ReadPublicKeyRequest(key_id=key_id, scope=self._scope)
            )
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._busy = False
        return result

    def generate_key(
        self,
        name: str,
        key_type: str = "ed25519",
        key_size: int = 0,
        comment: str = "",
        passphrase: str = "",
    ) -> GenerateKeyResult:
        self._enter_operation()
        request = GenerateKeyRequest(
            name=name,
            key_type=key_type,
            key_size=key_size,
            comment=comment,
            passphrase=passphrase,
            scope=self._scope,
        )
        try:
            result = self._client.generate_key(request)
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._upsert_summary(result.key)
            self._busy = False
        # The request (and its passphrase) goes out of scope here; the
        # controller retains only the public summary.
        return result

    def key_snapshot(self) -> tuple[KeySummary, ...]:
        """Read-only snapshot of the latest cached key list."""
        with self._lock:
            if self._cached is None:
                return ()
            return self._cached.keys

    def _upsert_summary(self, summary: KeySummary) -> None:
        cached = self._cached
        if cached is None:
            self._cached = KeyList(keys=(summary,))
            return
        keys = [item for item in cached.keys if item.key_id != summary.key_id]
        keys.append(summary)
        self._cached = KeyList(keys=tuple(keys))
