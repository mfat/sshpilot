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
import stat
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models import ClientId, ConnectionId, SessionId
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
    VerifyKeyPassphraseResult,
    contains_private_key_material,
)
from sshpilot.core.errors import CoreError, ErrorCode as CoreErrorCode
from sshpilot.core.keys import KeyGenerateSpec, KeyService, SSHKeyInfo

_MAX_PUBLIC_KEY_BYTES = 1024 * 1024

if TYPE_CHECKING:
    from sshpilot.daemon.interaction_broker import InteractionBroker


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
        self._scope_lock = threading.RLock()
        self._interaction_broker: Optional[InteractionBroker] = None
        self._active_scopes: dict[SessionId, ClientId] = {}

    def attach_interaction_broker(self, broker: InteractionBroker) -> None:
        """Attach the daemon's sole protected interaction broker."""
        self._interaction_broker = broker

    def client_can_interact(
        self,
        scope_id: SessionId,
        client_id: ClientId,
    ) -> bool:
        with self._scope_lock:
            return self._active_scopes.get(scope_id) == client_id

    def disconnect_client(self, client_id: ClientId) -> None:
        """Cancel protected key work whose owning client disappeared."""
        broker = self._interaction_broker
        if broker is None:
            return
        with self._scope_lock:
            scopes = tuple(
                scope
                for scope, owner in self._active_scopes.items()
                if owner == client_id
            )
            for scope in scopes:
                self._active_scopes.pop(scope, None)
        for scope in scopes:
            broker.cancel_session(scope)

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
            text = self._read_public_text(Path(info.public_path), root)
            if text is None:
                raise SshPilotError(
                    ErrorCode.KEY_PUBLIC_UNAVAILABLE,
                    "The key's public file is unavailable",
                )
            return PublicKeyResult(key_id=request.key_id, text=text)

    # -- deletion ----------------------------------------------------------
    def delete_key(self, request: DeleteKeyRequest) -> DeleteKeyResult:
        """Delete a managed key pair resolved from an opaque key id."""
        with self._lock:
            root = Path(self._path_resolver(request.scope))
            info = self._find_key(request.key_id, request.scope, root)
            if info is None:
                raise SshPilotError(
                    ErrorCode.KEY_NOT_FOUND,
                    "The requested SSH key was not found",
                )
            service = self._service_factory(root)
            if not service.delete_key(info.private_path):
                raise SshPilotError(
                    ErrorCode.KEY_DELETION_FAILED,
                    "The daemon could not delete the SSH key",
                )
            return DeleteKeyResult(key_id=request.key_id)

    # -- generation --------------------------------------------------------
    def generate_key(
        self,
        request: GenerateKeyRequest,
        *,
        owner_client_id: ClientId,
    ) -> GenerateKeyResult:
        scope_id = request.interaction_scope_id
        if request.encrypted:
            assert scope_id is not None
            self._register_scope(scope_id, owner_client_id)
        with self._lock:
            try:
                root = Path(self._path_resolver(request.scope))
                service = self._service_factory(root)
                spec = KeyGenerateSpec(
                    key_name=request.name,
                    key_type=request.key_type,
                    key_size=request.key_size,
                    comment=request.comment or None,
                    encrypted=request.encrypted,
                )
                try:
                    info = service.generate_key(
                        spec,
                        prepare_launch=(
                            None
                            if scope_id is None
                            else lambda argv: self._prepare_key_launch(
                                argv,
                                scope_id=scope_id,
                                label="SSH key generation",
                                confirm_passphrase=True,
                            )
                        ),
                    )
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
            finally:
                if scope_id is not None:
                    self._release_scope(scope_id)

    def verify_key_passphrase(
        self,
        request: VerifyKeyPassphraseRequest,
        *,
        owner_client_id: ClientId,
    ) -> VerifyKeyPassphraseResult:
        scope_id = request.interaction_scope_id
        self._register_scope(scope_id, owner_client_id)
        try:
            path = Path(request.key_path).expanduser()
            if not path.is_file():
                return VerifyKeyPassphraseResult(valid=False)
            service = self._service_factory(path.parent)
            try:
                valid = service.verify_key_passphrase(
                    path,
                    prepare_launch=lambda argv: self._prepare_key_launch(
                        argv,
                        scope_id=scope_id,
                        label="SSH key verification",
                        confirm_passphrase=False,
                    ),
                )
            except CoreError as exc:
                raise SshPilotError(
                    ErrorCode.KEY_VERIFICATION_FAILED,
                    "The daemon could not verify the key passphrase",
                ) from exc
            return VerifyKeyPassphraseResult(valid=valid)
        finally:
            self._release_scope(scope_id)

    # -- helpers -----------------------------------------------------------
    def _register_scope(
        self,
        scope_id: SessionId,
        owner_client_id: ClientId,
    ) -> None:
        if self._interaction_broker is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Protected key interaction is unavailable",
            )
        with self._scope_lock:
            if scope_id in self._active_scopes:
                raise SshPilotError(
                    ErrorCode.INVALID_REQUEST,
                    "The key interaction scope is already active",
                )
            self._active_scopes[scope_id] = owner_client_id

    def _release_scope(self, scope_id: SessionId) -> None:
        broker = self._interaction_broker
        if broker is not None:
            broker.cancel_session(scope_id)
        with self._scope_lock:
            self._active_scopes.pop(scope_id, None)

    def _prepare_key_launch(
        self,
        argv,
        *,
        scope_id: SessionId,
        label: str,
        confirm_passphrase: bool,
    ):
        broker = self._interaction_broker
        if broker is None:
            raise SshPilotError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "Protected key interaction is unavailable",
            )
        with self._scope_lock:
            if scope_id not in self._active_scopes:
                raise SshPilotError(
                    ErrorCode.TRANSPORT_CLOSED,
                    "The protected key operation owner disconnected",
                )
        return broker.prepare_operation_launch(
            argv,
            os.environ,
            scope_id=scope_id,
            connection_id=ConnectionId(f"key-{scope_id[-24:]}"),
            hostname=label,
            allow_stored_secrets=False,
            confirm_passphrase=confirm_passphrase,
        )

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
        payload = f"{scope.value}\x00{relative}".encode()
        digest = hashlib.sha256(payload).hexdigest()[:32]
        return KeyId(f"key-{digest}")

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _read_public_text(self, public_path: Path, root: Path) -> Optional[str]:
        """Read one public key through a single safely-opened descriptor.

        Returns the decoded text, or ``None`` when the file is unsafe, missing,
        unreadable, empty, oversized, malformed, symlinked, or private-key-like.
        The lexical path must sit beneath *root*; symbolic links are rejected
        outright (``lstat`` and ``O_NOFOLLOW`` where available). Size limits are
        enforced on the *opened* descriptor via ``fstat`` and a bounded read
        loop, so a file swapped between metadata and open cannot smuggle data.
        Never exposes paths or OS exception text.
        """
        if not self._is_within(public_path, root):
            return None
        fd: Optional[int] = None
        try:
            lst = os.lstat(public_path)
            if stat.S_ISLNK(lst.st_mode):
                return None
            flags = os.O_RDONLY
            for extra in (getattr(os, "O_CLOEXEC", 0), getattr(os, "O_NOFOLLOW", 0)):
                flags |= extra
            fd = os.open(public_path, flags)
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode):
                return None
            if fst.st_size < 1 or fst.st_size > _MAX_PUBLIC_KEY_BYTES:
                return None
            chunks: list[bytes] = []
            total = 0
            limit = _MAX_PUBLIC_KEY_BYTES + 1
            while total < limit:
                chunk = os.read(fd, min(64 * 1024, limit - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > _MAX_PUBLIC_KEY_BYTES:
                return None
            if total == 0:
                return None
            text = b"".join(chunks).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if not self._public_text_is_valid(text):
            return None
        return text

    @staticmethod
    def _public_text_is_valid(text: str) -> bool:
        """Same content rules as ``PublicKeyResult`` (shared pre-construction)."""
        if not text.strip():
            return False
        if "\x00" in text:
            return False
        if contains_private_key_material(text):
            return False
        return True

    def _public_available(self, public_path: str, root: Path) -> bool:
        """Metadata-only availability: no symlink, regular, non-empty, bounded."""
        path = Path(public_path)
        if not self._is_within(path, root):
            return False
        try:
            lst = os.lstat(path)
            if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
                return False
            if lst.st_size < 1 or lst.st_size > _MAX_PUBLIC_KEY_BYTES:
                return False
        except OSError:
            return False
        return True

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
