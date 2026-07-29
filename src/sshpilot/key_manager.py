# key_manager.py — GObject adapter over ``sshpilot.core.keys.KeyService``.
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from gi.repository import GObject

from .platform_utils import get_ssh_dir
from sshpilot.core.keys import KeyGenerateSpec, KeyService, SSHKeyInfo

logger = logging.getLogger(__name__)


class SSHKey:
    """Lightweight representation of a generated/known SSH key."""

    def __init__(self, private_path: str):
        self.private_path = private_path
        self.public_path = f"{private_path}.pub"

    def __str__(self) -> str:
        return Path(self.private_path).name

    @classmethod
    def from_info(cls, info: SSHKeyInfo) -> "SSHKey":
        return cls(info.private_path)


class KeyManager(GObject.Object):
    """GObject-facing key manager; domain work is in :class:`KeyService`."""

    __gsignals__ = {
        "key-generated": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self, ssh_dir: Optional[Path] = None):
        super().__init__()
        self._service = KeyService(Path(ssh_dir or get_ssh_dir()))
        self.ssh_dir = self._service.ssh_dir

    def discover_keys(self) -> List[SSHKey]:
        return [SSHKey.from_info(info) for info in self._service.discover_keys()]

    def generate_key(
        self,
        key_name: str,
        key_type: str = "ed25519",
        key_size: int = 3072,
        comment: Optional[str] = None,
        passphrase: Optional[str] = None,
    ) -> Optional[SSHKey]:
        """Generate a key via the core service; emit ``key-generated`` on success."""
        from sshpilot.core.errors import CoreError, ErrorCode

        try:
            info = self._service.generate_key(
                KeyGenerateSpec(
                    key_name=key_name,
                    key_type=key_type,
                    key_size=key_size,
                    comment=comment,
                    passphrase=passphrase,
                )
            )
            key = SSHKey.from_info(info)
            self.emit("key-generated", key)
            logger.info("Generated SSH key at %s", info.private_path)
            return key
        except CoreError as e:
            # Preserve historical exception types for GTK callers / tests.
            if e.code == ErrorCode.KEY_EXISTS:
                raise FileExistsError(e.message) from e
            if e.code == ErrorCode.VALIDATION_ERROR:
                raise ValueError(e.message) from e
            raise RuntimeError(e.message) from e
        except Exception as e:
            logger.error("Key generation failed: %s", e, exc_info=True)
            raise
