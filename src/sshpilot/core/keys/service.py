"""GTK-free SSH key discovery and generation planning."""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from ..errors import CoreError, ErrorCode
from .utils import SKIPPED_FILENAMES, is_private_key

logger = logging.getLogger(__name__)

KeyLaunchPreparer = Callable[
    [Sequence[str]],
    tuple[Sequence[str], Mapping[str, str]],
]
_KEYGEN_TIMEOUT_SECONDS = 180
_KEY_VERIFY_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class SSHKeyInfo:
    """Metadata for a discovered or generated key (never embeds private material)."""

    private_path: str

    @property
    def public_path(self) -> str:
        return f"{self.private_path}.pub"

    @property
    def name(self) -> str:
        return os.path.basename(self.private_path)


@dataclass(frozen=True)
class KeyGenerateSpec:
    """Non-secret specification for an ``ssh-keygen`` invocation."""

    key_name: str
    key_type: str = "ed25519"
    key_size: int = 3072
    comment: Optional[str] = None
    encrypted: bool = False


class KeyService:
    """Discover and generate SSH keys without GObject / GTK."""

    def __init__(self, ssh_dir: Path | str):
        self.ssh_dir = Path(ssh_dir)
        if not self.ssh_dir.exists():
            self.ssh_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.ssh_dir, 0o700)
            except OSError:
                pass
        self._key_validation_cache: Dict[str, bool] = {}

    def discover_keys(self) -> List[SSHKeyInfo]:
        keys: List[SSHKeyInfo] = []
        seen: set[str] = set()
        fallback_to_pub = False
        try:
            if not self.ssh_dir.exists():
                return keys
            for file_path in self.ssh_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                name = file_path.name
                if name.endswith(".pub") or name in SKIPPED_FILENAMES:
                    continue
                if fallback_to_pub:
                    pub = file_path.with_suffix(file_path.suffix + ".pub")
                    if pub.exists():
                        key_path = str(file_path)
                        if key_path not in seen:
                            keys.append(SSHKeyInfo(key_path))
                            seen.add(key_path)
                    continue
                try:
                    if is_private_key(
                        file_path,
                        cache=self._key_validation_cache,
                        skipped_filenames=SKIPPED_FILENAMES,
                    ):
                        key_path = str(file_path)
                        if key_path not in seen:
                            keys.append(SSHKeyInfo(key_path))
                            seen.add(key_path)
                except FileNotFoundError:
                    fallback_to_pub = True
                    pub = file_path.with_suffix(file_path.suffix + ".pub")
                    if pub.exists():
                        key_path = str(file_path)
                        if key_path not in seen:
                            keys.append(SSHKeyInfo(key_path))
                            seen.add(key_path)
        except OSError as exc:
            # Type-only message: the exception text may embed full paths.
            logger.error("SSH key discovery failed (%s)", type(exc).__name__)
        return keys

    def validate_generate_spec(self, spec: KeyGenerateSpec) -> None:
        """Raise :class:`CoreError` when *spec* cannot be used."""
        if type(spec.encrypted) is not bool:
            raise CoreError(
                ErrorCode.VALIDATION_ERROR,
                "Encrypted key selection must be boolean.",
            )
        if not spec.key_name or not spec.key_name.strip():
            raise CoreError(ErrorCode.VALIDATION_ERROR, "Key file name is required.")
        if "/" in spec.key_name or spec.key_name.startswith("."):
            raise CoreError(
                ErrorCode.VALIDATION_ERROR,
                "Key file name must not contain '/' or start with '.'.",
            )
        key_path = self.ssh_dir / spec.key_name
        if key_path.exists():
            base_name = spec.key_name
            counter = 1
            while (self.ssh_dir / f"{base_name}_{counter}").exists():
                counter += 1
            suggestion = f"{base_name}_{counter}"
            raise CoreError(
                ErrorCode.KEY_EXISTS,
                f"A key named '{spec.key_name}' already exists. Try '{suggestion}' instead.",
                details={"suggestion": suggestion},
            )
        kt = (spec.key_type or "").lower().strip()
        if kt not in ("ed25519", "rsa"):
            raise CoreError(ErrorCode.KEY_INVALID, f"Unsupported key type: {spec.key_type}")
        if kt == "rsa":
            size = int(spec.key_size) if spec.key_size else 3072
            if size < 1024:
                raise CoreError(ErrorCode.VALIDATION_ERROR, "RSA key size must be >= 1024 bits.")

    def build_keygen_argv(self, spec: KeyGenerateSpec) -> Sequence[str]:
        """Return ``ssh-keygen`` argv for *spec* (after validation)."""
        self.validate_generate_spec(spec)
        kt = (spec.key_type or "").lower().strip()
        key_path = self.ssh_dir / spec.key_name
        cmd = ["ssh-keygen", "-t", kt]
        if kt == "rsa":
            cmd += ["-b", str(int(spec.key_size) if spec.key_size else 3072)]
        comment = spec.comment
        if not comment:
            try:
                user = os.getenv("USER") or "user"
                host = os.uname().nodename
                comment = f"{user}@{host}"
            except OSError:
                comment = "generated-by-sshpilot"
        cmd += ["-C", comment, "-f", str(key_path)]
        if not spec.encrypted:
            # An empty literal is not protected input and keeps unencrypted
            # generation non-interactive. Encrypted generation is prompted by
            # native ssh-keygen through the daemon askpass broker instead.
            cmd += ["-N", ""]
        return cmd

    def generate_key(
        self,
        spec: KeyGenerateSpec,
        *,
        prepare_launch: Optional[KeyLaunchPreparer] = None,
    ) -> SSHKeyInfo:
        """Run ``ssh-keygen`` for *spec* and return the resulting key info."""
        cmd = list(self.build_keygen_argv(spec))
        environment: Optional[Mapping[str, str]] = None
        if spec.encrypted:
            if prepare_launch is None:
                raise CoreError(
                    ErrorCode.KEY_INVALID,
                    "Protected key generation is unavailable.",
                )
            prepared_argv, environment = prepare_launch(cmd)
            cmd = list(prepared_argv)
        key_path = self.ssh_dir / spec.key_name
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                env=None if environment is None else dict(environment),
                timeout=_KEYGEN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise CoreError(
                ErrorCode.KEY_INVALID,
                "ssh-keygen timed out.",
            ) from exc
        except OSError as exc:
            raise CoreError(
                ErrorCode.KEY_INVALID,
                "ssh-keygen could not be started.",
            ) from exc
        if completed.returncode != 0:
            raise CoreError(ErrorCode.KEY_INVALID, "ssh-keygen failed.")
        try:
            os.chmod(key_path, 0o600)
            pub_path = f"{key_path}.pub"
            if os.path.exists(pub_path):
                os.chmod(pub_path, 0o644)
        except OSError as perm_err:
            # Type-only message: the exception text may embed full paths.
            logger.warning(
                "Failed setting permissions on key files (%s)",
                type(perm_err).__name__,
            )
        return SSHKeyInfo(str(key_path))

    def verify_key_passphrase(
        self,
        key_path: Path | str,
        *,
        prepare_launch: KeyLaunchPreparer,
    ) -> bool:
        """Verify a key through native prompting without ``-P`` or secret env."""
        path = Path(key_path).expanduser()
        if not path.is_file():
            return False
        cmd: Sequence[str] = ("ssh-keygen", "-y", "-f", str(path))
        prepared_argv, environment = prepare_launch(cmd)
        try:
            completed = subprocess.run(
                list(prepared_argv),
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                env=dict(environment),
                timeout=_KEY_VERIFY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise CoreError(
                ErrorCode.KEY_INVALID,
                "SSH key verification timed out.",
            ) from exc
        except OSError as exc:
            raise CoreError(
                ErrorCode.KEY_INVALID,
                "SSH key verification could not start.",
            ) from exc
        return completed.returncode == 0

    def fingerprint(self, public_or_private_path: str) -> Optional[str]:
        """Return ``ssh-keygen -lf`` fingerprint, or None on failure."""
        try:
            completed = subprocess.run(
                ["ssh-keygen", "-lf", public_or_private_path],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                return None
            return (completed.stdout or "").strip() or None
        except OSError:
            return None

    def parse_public_key(self, text: str) -> Optional[Dict[str, str]]:
        """Parse a single authorized_keys / .pub line into type/data/comment."""
        line = (text or "").strip()
        if not line or line.startswith("#"):
            return None
        parts = line.split()
        if len(parts) < 2:
            return None
        return {
            "key_type": parts[0],
            "key_data": parts[1],
            "comment": " ".join(parts[2:]) if len(parts) > 2 else "",
        }
