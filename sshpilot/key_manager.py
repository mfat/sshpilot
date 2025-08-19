# key_manager.py
from __future__ import annotations

import os
import subprocess
import logging
from pathlib import Path
from typing import Optional, List

from gi.repository import GObject

logger = logging.getLogger(__name__)


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
        self.ssh_dir = Path(ssh_dir or Path.home() / ".ssh")
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
            if not self.ssh_dir.exists():
                return keys
            for file_path in self.ssh_dir.iterdir():
                if not file_path.is_file():
                    continue
                name = file_path.name
                if name.endswith(".pub"):
                    continue
                # skip very common non-key files
                if name in ("config", "known_hosts", "authorized_keys"):
                    continue
                pub = file_path.with_suffix(file_path.suffix + ".pub")
                if pub.exists():
                    keys.append(SSHKey(str(file_path)))
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
                raise FileExistsError(f"A key named '{key_name}' already exists at {key_path}")

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