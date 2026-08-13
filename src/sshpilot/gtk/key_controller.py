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
from sshpilot.api.events import EventType
from sshpilot.api.models import (
    InteractionDecisionRequest,
    InteractionType,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from sshpilot.api.models.connections import StoreKeyPassphraseRequest
from sshpilot.api.models.keys import (
    DeleteKeyRequest,
    DeleteKeyResult,
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    ListKeysRequest,
    PublicKeyResult,
    ReadPublicKeyRequest,
    VerifyKeyPassphraseRequest,
)
from sshpilot.runtime_identity import new_operation_id


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
            with self._lock:
                self._cached = key_list
            return key_list
        finally:
            with self._lock:
                self._busy = False

    def read_public_key(self, key_id: KeyId) -> PublicKeyResult:
        self._enter_operation()
        try:
            result = self._client.read_public_key(
                ReadPublicKeyRequest(key_id=key_id, scope=self._scope)
            )
            return result
        finally:
            with self._lock:
                self._busy = False

    def delete_key(self, key_id: KeyId) -> DeleteKeyResult:
        self._enter_operation()
        try:
            result = self._client.delete_key(
                DeleteKeyRequest(key_id=key_id, scope=self._scope)
            )
            with self._lock:
                if self._cached is not None:
                    self._cached = KeyList(
                        keys=tuple(
                            key
                            for key in self._cached.keys
                            if key.key_id != result.key_id
                        )
                    )
            return result
        finally:
            with self._lock:
                self._busy = False

    def generate_key(
        self,
        name: str,
        key_type: str = "ed25519",
        key_size: int = 0,
        comment: str = "",
        encrypted: bool = False,
        interaction_scope_id: SessionId | None = None,
    ) -> GenerateKeyResult:
        self._enter_operation()
        try:
            request = GenerateKeyRequest(
                name=name,
                key_type=key_type,
                key_size=key_size,
                comment=comment,
                encrypted=encrypted,
                interaction_scope_id=interaction_scope_id,
                scope=self._scope,
            )
            result = self._client.generate_key(request)
            with self._lock:
                self._upsert_summary(result.key)
            return result
        finally:
            with self._lock:
                self._busy = False

    def verify_key_passphrase(
        self,
        key_path: str,
        secret: bytearray,
    ) -> bool:
        """Verify via the daemon, sending *secret* only as a secret frame."""
        scope_id = SessionId(f"key-{new_operation_id()}")
        result = self._run_protected_key_secret(
            scope_id,
            secret,
            lambda: self._client.verify_key_passphrase(
                VerifyKeyPassphraseRequest(
                    key_path=key_path,
                    interaction_scope_id=scope_id,
                )
            ),
        )
        return result.valid

    def store_key_passphrase(
        self,
        key_path: str,
        secret: bytearray,
    ) -> bool:
        """Store through a protected interaction, never an ordinary DTO field."""
        scope_id = SessionId(f"key-{new_operation_id()}")
        return bool(
            self._run_protected_key_secret(
                scope_id,
                secret,
                lambda: self._client.store_key_passphrase(
                    StoreKeyPassphraseRequest(
                        key_path=key_path,
                        interaction_scope_id=scope_id,
                    )
                ),
            )
        )

    def _run_protected_key_secret(
        self,
        scope_id: SessionId,
        secret: bytearray,
        operation,
    ):
        if type(secret) is not bytearray:
            raise TypeError("key passphrase must be a bytearray")
        self._enter_operation()
        responder_lock = threading.Lock()
        responder: list[threading.Thread] = []

        def _clear() -> None:
            secret[:] = b"\0" * len(secret)
            secret.clear()

        def _respond(summary) -> None:
            try:
                claim = self._client.claim_interaction(summary.id)
                self._client.respond_to_interaction(
                    InteractionDecisionRequest(
                        interaction_id=summary.id,
                        secret_decision=SecretDecision.SUBMIT,
                        remember_policy=RememberPolicy.DO_NOT_STORE,
                    )
                )
                self._client.send_interaction_secret(
                    summary.id,
                    claim.nonce,
                    secret,
                )
            except Exception:
                # The request thread receives the daemon's typed failure. Do
                # not expose transport exception text from this secret worker.
                pass
            finally:
                _clear()

        def _on_event(event) -> None:
            if event.type is not EventType.INTERACTION_CREATED:
                return
            summary = event.payload
            if (
                summary.session_id != scope_id
                or summary.type is not InteractionType.PRIVATE_KEY_PASSPHRASE
            ):
                return
            with responder_lock:
                if responder:
                    return
                thread = threading.Thread(
                    target=_respond,
                    args=(summary,),
                    name="sshpilot-key-passphrase-response",
                    daemon=True,
                )
                responder.append(thread)
                thread.start()

        subscription = None
        try:
            subscription = self._client.subscribe_events(_on_event)
            return operation()
        finally:
            if subscription is not None:
                subscription.close()
            with responder_lock:
                thread = responder[0] if responder else None
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5.0)
            _clear()
            with self._lock:
                self._busy = False

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
