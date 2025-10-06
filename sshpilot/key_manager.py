# key_manager.py
from __future__ import annotations

import os
import subprocess
import logging
from pathlib import Path
from typing import Optional, List, Dict

from gi.repository import GObject

from .platform_utils import get_ssh_dir

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
    _SKIPPED_FILENAMES = {"config", "known_hosts", "authorized_keys"}

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
        self._key_validation_cache: Dict[str, bool] = {}

    def _is_private_key(self, file_path: Path) -> bool:
        """Return True if the path looks like a private SSH key."""
        name = file_path.name
        if not file_path.is_file() or name.endswith(".pub") or name in self._SKIPPED_FILENAMES:
            return False

        key_path = str(file_path)
        cached = self._key_validation_cache.get(key_path)
        if cached is not None:
            return cached

        cmd = ["ssh-keygen", "-y", "-f", key_path, "-P", ""]
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            raise
        except Exception as exc:
            logger.debug("Failed to run ssh-keygen for %s: %s", key_path, exc, exc_info=True)
            self._key_validation_cache[key_path] = False
            return False

        stderr = completed.stderr or ""
        stdout = completed.stdout or ""
        stderr_lower = stderr.lower()

        success = completed.returncode == 0
        if not success and ("incorrect passphrase supplied" in stderr_lower or "load key" in stderr_lower):
            success = True
            logger.debug(
                "ssh-keygen reported a protected key for %s: %s",
                key_path,
                stderr.strip() or stdout.strip() or "passphrase required",
            )

        if success:
            self._key_validation_cache[key_path] = True
            return True

        message = stderr.strip() or stdout.strip() or f"ssh-keygen exited with {completed.returncode}"
        logger.debug("ssh-keygen rejected %s: %s", key_path, message)
        self._key_validation_cache[key_path] = False
        return False

    # ---------------- Public API ----------------

    def discover_keys(self) -> List[SSHKey]:
        """Discover known SSH keys within the configured SSH directory."""
        keys: List[SSHKey] = []
        seen: set[str] = set()
        fallback_to_pub = False
        try:
            ssh_dir = self.ssh_dir or Path(get_ssh_dir())
            if not ssh_dir.exists():
                return keys
            # Recursively walk SSH directory for private keys that have a matching .pub
            for file_path in ssh_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                name = file_path.name
                if name.endswith(".pub"):
                    continue
                # skip very common non-key files
                if name in self._SKIPPED_FILENAMES:
                    continue
                if fallback_to_pub:
                    pub = file_path.with_suffix(file_path.suffix + ".pub")
                    if pub.exists():
                        key_path = str(file_path)
                        if key_path not in seen:
                            keys.append(SSHKey(key_path))
                            seen.add(key_path)
                    continue

                try:
                    if self._is_private_key(file_path):
                        key_path = str(file_path)
                        if key_path not in seen:
                            keys.append(SSHKey(key_path))
                            seen.add(key_path)
                except FileNotFoundError:
                    fallback_to_pub = True
                    logger.debug(
                        "ssh-keygen not available; falling back to public-key discovery for %s",
                        file_path,
                    )
                    pub = file_path.with_suffix(file_path.suffix + ".pub")
                    if pub.exists():
                        key_path = str(file_path)
                        if key_path not in seen:
                            keys.append(SSHKey(key_path))
                            seen.add(key_path)
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