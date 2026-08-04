"""Daemon-owned SSH-key service (headless, scope-resolved).

Owns authoritative access to the selected key store: listing rediscoveries key
metadata beneath the resolved root, public-key reading validates the key id and
bounds the file, and generation translates the semantic request into a core
``KeyService`` invocation. The frontend never supplies a key-directory path;
the service resolves the root itself from the semantic scope. Private-key
contents never cross the API, and passphrases are never logged or surfaced in
errors.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Callable, Optional

from sshpilot.api.errors import ErrorCode, SshPilotError
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
from sshpilot.core.errors import CoreError, ErrorCode as CoreErrorCode
from sshpilot.core.keys import KeyGenerateSpec, KeyService, SSHKeyInfo

_MAX_PUBLIC_KEY_BYTES = 1024 * 1024


class DaemonKeyService:
    """Authoritative, lock-serialized SSH-key service for the daemon."""

    def __init__(
        self,
        path_resolver: Callable[[KeyStoreScope], Path],
        *,
        service_factory: Callable[[Path], KeyService] = KeyService,
    ) -> None:
        self._path_resolver = path_resolver
        self._service_factory = service_factory
        self._lock = threading.RLock()

    # -- listing ---------------------------------------------------------
    def list_keys(self, request: ListKeysRequest) -> KeyList:
        with self._lock:
            root = Path(self._path_resolver(request.scope))
            service = self._service_factory(root)
            summaries: list[KeySummary] = []
            for info in service.discover_keys():
                if not self._is_within(Path(info.private_path), root):
                    continue
                summaries.append(self._summary_for(info, request.scope, root))
            return KeyList(keys=tuple(summaries))

    # -- public-key reading ----------------------------------------------
    def read_public_key(self, request: ReadPublicKeyRequest) -> PublicKeyResult:
        with self._lock:
            root = Path(self._path_resolver(request.scope))
            info = self._find_key(request.key_id, request.scope, root)
            if info is None:
                raise SshPilotError(
                    ErrorCode.KEY_NOT_FOUND,
                    "The requested SSH key was not found",
                )
            public_path = Path(info.public_path)
            if not self._safe_public_path(public_path, root):
                raise SshPilotError(
                    ErrorCode.KEY_PUBLIC_UNAVAILABLE,
                    "The key's public file is unavailable",
                )
            try:
                if public_path.stat().st_size > _MAX_PUBLIC_KEY_BYTES:
                    raise SshPilotError(
                        ErrorCode.KEY_PUBLIC_UNAVAILABLE,
                        "The key's public file is unavailable",
                    )
                data = public_path.read_bytes()
            except OSError:
                raise SshPilotError(
                    ErrorCode.KEY_PUBLIC_UNAVAILABLE,
                    "The key's public file is unavailable",
                ) from None
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise SshPilotError(
                    ErrorCode.KEY_PUBLIC_UNAVAILABLE,
                    "The key's public file is unavailable",
                ) from None
            if "\x00" in text:
                raise SshPilotError(
                    ErrorCode.KEY_PUBLIC_UNAVAILABLE,
                    "The key's public file is unavailable",
                )
            return PublicKeyResult(key_id=request.key_id, text=text)

    # -- generation --------------------------------------------------------
    def generate_key(self, request: GenerateKeyRequest) -> GenerateKeyResult:
        with self._lock:
            root = Path(self._path_resolver(request.scope))
            service = self._service_factory(root)
            spec = KeyGenerateSpec(
                key_name=request.name,
                key_type=request.key_type,
                key_size=request.key_size,
                comment=request.comment or None,
                passphrase=request.passphrase,
            )
            try:
                info = service.generate_key(spec)
            except CoreError as exc:
                raise self._map_generate_error(exc) from exc
            except OSError as exc:
                raise SshPilotError(
                    ErrorCode.KEY_GENERATION_FAILED,
                    "The daemon could not generate the key",
                ) from exc
            if not self._is_within(Path(info.private_path), root):
                raise SshPilotError(
                    ErrorCode.KEY_GENERATION_FAILED,
                    "The daemon could not generate the key",
                )
            return GenerateKeyResult(
                key=self._summary_for(info, request.scope, root)
            )

    # -- helpers -----------------------------------------------------------
    def _find_key(
        self,
        key_id: KeyId,
        scope: KeyStoreScope,
        root: Path,
    ) -> Optional[SSHKeyInfo]:
        service = self._service_factory(root)
        for info in service.discover_keys():
            if (
                self._is_within(Path(info.private_path), root)
                and self._key_id_for_scope(scope, root, info.private_path) == key_id
            ):
                return info
        return None

    def _summary_for(self, info: SSHKeyInfo, scope: KeyStoreScope, root: Path) -> KeySummary:
        public_path = f"{info.private_path}.pub"
        return KeySummary(
            key_id=self._key_id_for_scope(scope, root, info.private_path),
            name=info.name,
            private_path=info.private_path,
            public_path=public_path,
            public_key_available=self._public_available(public_path, root),
        )

    def _key_id_for_scope(
        self,
        scope: KeyStoreScope,
        root: Path,
        private_path: str,
    ) -> KeyId:
        try:
            relative = Path(private_path).relative_to(root).as_posix()
        except ValueError:
            relative = Path(private_path).name
        payload = f"{scope.value}\x00{relative}".encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()[:32]
        return KeyId(f"key-{digest}")

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _safe_public_path(self, public_path: Path, root: Path) -> bool:
        if not self._is_within(public_path, root):
            return False
        try:
            real_root = os.path.realpath(root)
            real_public = os.path.realpath(public_path)
        except OSError:
            return False
        return Path(real_public).is_relative_to(Path(real_root))

    def _public_available(self, public_path: str, root: Path) -> bool:
        path = Path(public_path)
        if not self._safe_public_path(path, root):
            return False
        try:
            return path.is_file()
        except OSError:
            return False

    @staticmethod
    def _map_generate_error(exc: CoreError) -> SshPilotError:
        if exc.code is CoreErrorCode.KEY_EXISTS:
            details: dict = {}
            suggestion = (exc.details or {}).get("suggestion")
            if suggestion is not None:
                details["suggestion"] = str(suggestion)
            return SshPilotError(
                ErrorCode.KEY_ALREADY_EXISTS,
                "A key with that name already exists",
                details=details,
            )
        if exc.code is CoreErrorCode.VALIDATION_ERROR:
            return SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The key generation request is invalid",
            )
        # Core uses KEY_INVALID for ssh-keygen/output failures as well; the
        # daemon model already validates key type and size up front.
        return SshPilotError(
            ErrorCode.KEY_GENERATION_FAILED,
            "The daemon could not generate the key",
        )
