# key_manager.py
from __future__ import annotations

import os
import subprocess
import logging
from pathlib import Path
from typing import Optional, List

from paramiko import pkey
from paramiko.ssh_exception import (
    PasswordRequiredException,
    SSHException,
)

from gi.repository import GObject

from .platform_utils import get_ssh_dir

logger = logging.getLogger(__name__)


_IGNORED_KEY_FILENAMES = {"config", "known_hosts", "authorized_keys"}


def _is_private_key(file_path: Path) -> bool:
    """Return True if *file_path* looks like a private key."""

    try:
        if not file_path.is_file():
            return False
        name = file_path.name
        if name.endswith(".pub"):
            return False
        if name in _IGNORED_KEY_FILENAMES:
            return False
        try:
            pkey.load_private_key_file(str(file_path))
            return True
        except PasswordRequiredException:
            return True
        except (SSHException, ValueError):
            return False
    except OSError:
        return False


class SSHKey:
    """
    Lightweight representation of a generated/known SSH key.
    """
    def __init__(self, private_path: str):
        self.private_path = private_path
        self.public_path = f"{private_path}.pub"

    def __str__(self) -> str:
        return os.path.basename(self.private_path)


class KeyManager(GObject.Object):
    """
    Unified SSH key generation (single method) + discovery helper.
    Uses system `ssh-keygen` for portability and OpenSSH-compatible output.
    """
    __gsignals__ = {
        # Emitted after a key is generated successfully
        "key-generated": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self, ssh_dir: Optional[Path] = None):
        super().__init__()
        self.ssh_dir = Path(ssh_dir or get_ssh_dir())
        if not self.ssh_dir.exists():
            self.ssh_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.ssh_dir, 0o700)
            except Exception:
                pass

    # ---------------- Public API ----------------

    def discover_keys(self) -> List[SSHKey]:
        """List keys that have a matching .pub next to the private key."""
        keys: List[SSHKey] = []
        try:
            ssh_dir = self.ssh_dir or Path(get_ssh_dir())
            if not ssh_dir.exists():
                return keys
            seen: set[Path] = set()
            for file_path in ssh_dir.rglob("*"):
                if file_path in seen:
                    continue
                if _is_private_key(file_path):
                    keys.append(SSHKey(str(file_path)))
                    seen.add(file_path)
        except Exception as e:
            logger.error("Failed to discover SSH keys: %s", e)
        return keys

    def generate_key(
        self,
        key_name: str,
        key_type: str = "ed25519",
        key_size: int = 3072,          # used only when key_type == "rsa"
        comment: Optional[str] = None,
        passphrase: Optional[str] = None,
    ) -> Optional[SSHKey]:
        """
        Single, unified generator using `ssh-keygen`.
        - key_type: "ed25519" (default) or "rsa"
        - key_size: used only for RSA (min 1024)
        - passphrase: optional; empty means unencrypted key
        Returns SSHKey or None on failure.
        """
        try:
            # validate filename
            if not key_name or key_name.strip() == "":
                raise ValueError("Key file name is required.")
            if "/" in key_name or key_name.startswith("."):
                raise ValueError("Key file name must not contain '/' or start with '.'.")

            key_path = self.ssh_dir / key_name
            if key_path.exists():
                # Suggest alternative names
                base_name = key_name
                counter = 1
                while (self.ssh_dir / f"{base_name}_{counter}").exists():
                    counter += 1
                suggestion = f"{base_name}_{counter}"
                
                raise FileExistsError(f"A key named '{key_name}' already exists. Try '{suggestion}' instead.")

            kt = (key_type or "").lower().strip()
            if kt not in ("ed25519", "rsa"):
                raise ValueError(f"Unsupported key type: {key_type}")

            cmd = ["ssh-keygen", "-t", kt]
            if kt == "rsa":
                size = int(key_size) if key_size else 3072
                if size < 1024:
                    raise ValueError("RSA key size must be >= 1024 bits.")
                cmd += ["-b", str(size)]

            if not comment:
                try:
                    user = os.getenv("USER") or "user"
                    host = os.uname().nodename
                    comment = f"{user}@{host}"
                except Exception:
                    comment = "generated-by-sshpilot"
            cmd += ["-C", comment]

            cmd += ["-f", str(key_path)]
            cmd += ["-N", passphrase or ""]  # empty => no passphrase

            logger.debug("Running ssh-keygen: %s", " ".join(cmd))
            completed = subprocess.run(cmd, capture_output=True, text=True, check=False)

            if completed.returncode != 0:
                # Surface stderr as the UI message
                stderr = completed.stderr.strip() or "ssh-keygen failed"
                raise RuntimeError(stderr)

            # Ensure sane permissions (best effort)
            try:
                os.chmod(key_path, 0o600)
                pub_path = f"{key_path}.pub"
                if os.path.exists(pub_path):
                    os.chmod(pub_path, 0o644)
            except Exception as perm_err:
                logger.warning("Failed setting permissions on key files: %s", perm_err)

            key = SSHKey(str(key_path))
            self.emit("key-generated", key)
            logger.info("Generated SSH key at %s", key_path)
            return key

        except Exception as e:
            logger.error("Key generation failed: %s", e, exc_info=True)
            # Re-raise the exception so the UI can handle it properly
            raise