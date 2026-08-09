# key_manager.py — GObject adapter over the daemon-backed ``SshPilotClient``.
#
# M1 ownership: the daemon owns key discovery, generation, and public-key
# reads. This module performs no filesystem access — no key-directory scanning,
# no ``ssh-keygen``, no ``.pub`` reads — and keeps a compatibility ``SSHKey``
# DTO so the existing ssh-copy-id / authorized-keys UIs keep working against
# the daemon's summaries.
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from gi.repository import GObject

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models import SessionId
from sshpilot.api.models.keys import KeyStoreScope, KeySummary
from sshpilot.gtk.key_controller import KeyController

logger = logging.getLogger(__name__)


class SSHKey:
    """Lightweight compatibility DTO projected from a daemon ``KeySummary``.

    Paths are daemon-provided compatibility metadata for the M7 ssh-copy-id
    subprocess adapter; the frontend never derives, scans, or reads these
    files. ``key_id`` is the opaque daemon identifier used for API calls.
    """

    __slots__ = (
        "key_id",
        "name",
        "private_path",
        "public_path",
        "public_key_available",
    )

    def __init__(
        self,
        private_path: str,
        *,
        key_id: Optional[str] = None,
        name: Optional[str] = None,
        public_path: Optional[str] = None,
        public_key_available: bool = False,
    ):
        self.private_path = private_path
        self.public_path = (
            public_path if public_path is not None else f"{private_path}.pub"
        )
        self.name = name if name is not None else (
            Path(private_path).name if private_path else ""
        )
        self.key_id = key_id
        self.public_key_available = public_key_available

    def __str__(self) -> str:
        return self.name

    @classmethod
    def from_summary(cls, summary: KeySummary) -> "SSHKey":
        return cls(
            summary.private_path,
            key_id=summary.key_id,
            name=summary.name,
            public_path=summary.public_path,
            public_key_available=summary.public_key_available,
        )


class KeyManager(GObject.Object):
    """GObject-facing key manager; domain work happens in the daemon.

    Never instantiates ``core.keys.KeyService`` and never touches the file
    system. All operations are routed through ``KeyController`` over the
    injected ``SshPilotClient``; there is no local fallback.
    """

    __gsignals__ = {
        "key-generated": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(
        self,
        client,
        scope: KeyStoreScope = KeyStoreScope.DEFAULT,
    ):
        if isinstance(client, (str, bytes, Path)) or hasattr(client, "__fspath__"):
            raise TypeError(
                "KeyManager no longer accepts a key-directory path; "
                "pass a daemon-backed SshPilotClient instead"
            )
        super().__init__()
        self._controller = KeyController(client, scope)
        self._scope = scope

    def discover_keys(self) -> List[SSHKey]:
        key_list = self._controller.list_keys()
        return [SSHKey.from_summary(summary) for summary in key_list.keys]

    def read_public_key(self, key: SSHKey) -> str:
        """Return the public-key text for a daemon-discovered ``SSHKey``.

        The daemon resolves the opaque ``key_id``; no local ``.pub`` read.
        """
        result = self._controller.read_public_key(key.key_id)
        return result.text

    def generate_key(
        self,
        key_name: str,
        key_type: str = "ed25519",
        key_size: int = 0,
        comment: Optional[str] = None,
        encrypted: bool = False,
        interaction_scope_id: Optional[SessionId] = None,
    ) -> Optional[SSHKey]:
        """Generate a key via the daemon; emit ``key-generated`` on success.

        Compatibility: RSA callers that omit ``key_size`` (the historical
        adapter default) get 3072, matching the old local ``KeyService``
        contract. Ed25519 always uses size 0.
        """
        if key_type == "rsa" and not key_size:
            key_size = 3072
        try:
            result = self._controller.generate_key(
                name=key_name,
                key_type=key_type,
                key_size=key_size,
                comment=comment or "",
                encrypted=encrypted,
                interaction_scope_id=interaction_scope_id,
            )
        except SshPilotError as exc:
            if exc.code == ErrorCode.MUTATION_AMBIGUOUS:
                # Structured ambiguity: the mutation may have completed. The
                # caller decides how to recover (reload, no automatic retry).
                raise
            raise self._map_error(exc) from exc
        key = SSHKey.from_summary(result.key)
        try:
            self.emit("key-generated", key)
        except Exception:
            # Fire-and-forget notification; never fail generation on a signal
            # callback issue (matches Config/ConnectionManager emit usage).
            logger.debug("key-generated signal emit failed", exc_info=True)
        logger.info("SSH key generated")
        return key

    def verify_key_passphrase(
        self,
        key_path: str,
        secret: bytearray,
    ) -> bool:
        """Verify through the daemon's protected interaction channel."""
        return self._controller.verify_key_passphrase(key_path, secret)

    def store_key_passphrase(
        self,
        key_path: str,
        secret: bytearray,
    ) -> bool:
        """Store through the daemon's protected interaction channel."""
        return self._controller.store_key_passphrase(key_path, secret)

    @staticmethod
    def _map_error(exc: SshPilotError) -> BaseException:
        """Preserve historical exception types for GTK callers / tests.

        Never includes paths, passphrases, or subprocess command lines.
        """
        if exc.code == ErrorCode.KEY_ALREADY_EXISTS:
            return FileExistsError(exc.message)
        if exc.code == ErrorCode.VALIDATION_FAILED:
            return ValueError(exc.message)
        return RuntimeError(exc.message)
