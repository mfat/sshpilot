"""Pluggable secret/key storage backends.

sshPilot stores two kinds of secrets — host passwords and SSH key passphrases
(plugin secrets ride the password path). Historically each call site hard-wired
libsecret with a ``keyring`` fallback. This module abstracts that into a small
backend interface so alternative stores can be selected without touching call
sites.

Built-in backends:
- ``libsecret`` — the GNOME Secret Service (Linux), via ``gi.repository.Secret``.
- ``keyring``   — the cross-platform Python ``keyring`` library (macOS Keychain,
  Windows Credential Manager, KWallet, …).
- ``pass``      — passwordstore.org (``pass`` CLI, gpg-backed).

Selection (``SecretManager.set_selected`` / config ``secrets.backend``):
- ``auto``  — platform default: macOS → keyring; Linux → libsecret then keyring.
- a name    — that backend first, then the platform defaults as store fallback.

Operations:
- ``store`` — first available backend in the selected order (with fallback on
  failure).
- ``lookup`` / ``delete`` — selected order first, then every other registered
  backend that is available (e.g. ``pass`` while on ``auto``), so secrets are
  not orphaned when the user switches backends.

The module is GTK-free so it can be imported by the ``--askpass`` subprocess; it
lazily imports ``Secret`` and ``keyring``.

**Backward compatibility:** the ``SecretSpec`` builders reproduce the exact
libsecret attributes / schema and keyring ``(service, account)`` keys used before
this module existed, so existing saved credentials keep working under ``auto``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - optional dependency
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret
except Exception:  # pragma: no cover - optional dependency
    Secret = None

try:  # pragma: no cover - optional dependency
    import keyring
except Exception:  # pragma: no cover - optional dependency
    keyring = None

try:
    from .platform_utils import is_macos
except Exception:  # pragma: no cover - fallback when run as a loose module
    try:
        from platform_utils import is_macos  # type: ignore
    except Exception:
        def is_macos() -> bool:  # type: ignore
            import sys
            return sys.platform == "darwin"


# Legacy identifiers — MUST match the values used before this module existed so
# that already-stored secrets remain readable.
SERVICE_NAME = "sshPilot"
SCHEMA_NAME = "io.github.mfat.sshpilot"

_SCHEMA = None


def get_schema():
    """Return the shared ``Secret.Schema`` (or ``None`` when libsecret is absent).

    Identical to the historical schema in ``askpass_utils.get_secret_schema`` so
    existing entries match.
    """
    global _SCHEMA
    if Secret is None:
        return None
    if _SCHEMA is None:
        _SCHEMA = Secret.Schema.new(
            SCHEMA_NAME,
            Secret.SchemaFlags.NONE,
            {
                "application": Secret.SchemaAttributeType.STRING,
                "type": Secret.SchemaAttributeType.STRING,
                "key_path": Secret.SchemaAttributeType.STRING,
                "host": Secret.SchemaAttributeType.STRING,
                "username": Secret.SchemaAttributeType.STRING,
            },
        )
    return _SCHEMA


@dataclass
class SecretSpec:
    """Identifies one secret across every backend representation.

    Each backend consumes whichever fields it needs; together they reproduce the
    exact legacy storage keys.
    """

    keyring_service: str
    keyring_account: str
    attributes: Dict[str, str]
    label: str
    pass_path: str


def _pass_segment(value: str) -> str:
    """Make a string safe as a single ``pass`` path segment.

    ``pass`` treats ``/`` as a directory separator, so a hostname, username or key
    path containing slashes would scatter entries across the store (or escape it).
    Replace path separators and control characters with ``_``.
    """
    return "".join("_" if (ch in "/\\" or ord(ch) < 0x20) else ch for ch in value)


def password_spec(host: str, username: str) -> SecretSpec:
    """Spec for a host login password (also used by plugin secrets)."""
    account = f"{username}@{host}"
    return SecretSpec(
        keyring_service=SERVICE_NAME,
        keyring_account=account,
        attributes={
            "application": SERVICE_NAME,
            "type": "ssh_password",
            "host": host,
            "username": username,
        },
        label=f"{SERVICE_NAME}: {account}",
        pass_path=f"sshpilot/password/{_pass_segment(account)}",
    )


def passphrase_spec(canonical_key_path: str) -> SecretSpec:
    """Spec for an SSH key passphrase. ``canonical_key_path`` must already be
    normalized by the caller (``askpass_utils._normalize_key_path_for_storage``)."""
    return SecretSpec(
        keyring_service=SERVICE_NAME,
        keyring_account=canonical_key_path,
        attributes={
            "application": SERVICE_NAME,
            "type": "key_passphrase",
            "key_path": canonical_key_path,
        },
        label=f"SSH Key Passphrase: {os.path.basename(canonical_key_path)}",
        pass_path=f"sshpilot/passphrase/{_pass_segment(canonical_key_path)}",
    )


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class SecretBackend:
    """Interface for a secret store. Subclasses must be safe to instantiate even
    when their dependency is missing (``is_available()`` reports readiness)."""

    name: str = "base"

    def describe(self) -> str:
        """Human/diagnostic label (may include sub-backend detail)."""
        return self.name

    def is_available(self) -> bool:
        raise NotImplementedError

    def store(self, spec: SecretSpec, secret: str) -> bool:
        raise NotImplementedError

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        raise NotImplementedError

    def delete(self, spec: SecretSpec) -> bool:
        raise NotImplementedError


class LibSecretBackend(SecretBackend):
    name = "libsecret"

    def __init__(self) -> None:
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if Secret is None or get_schema() is None:
            return False
        if self._available is None:
            try:
                Secret.Service.get_sync(Secret.ServiceFlags.NONE)
                self._available = True
            except Exception as exc:
                logger.debug("libsecret unavailable: %s", exc)
                self._available = False
        return self._available

    def store(self, spec: SecretSpec, secret: str) -> bool:
        try:
            Secret.password_store_sync(
                get_schema(),
                spec.attributes,
                Secret.COLLECTION_DEFAULT,
                spec.label,
                secret,
                None,
            )
            return True
        except Exception as exc:
            logger.error("libsecret store failed: %s", exc)
            return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        try:
            return Secret.password_lookup_sync(get_schema(), spec.attributes, None)
        except Exception as exc:
            logger.error("libsecret lookup failed: %s", exc)
            return None

    def delete(self, spec: SecretSpec) -> bool:
        try:
            return bool(Secret.password_clear_sync(get_schema(), spec.attributes, None))
        except Exception as exc:
            logger.error("libsecret delete failed: %s", exc)
            return False


class KeyringBackend(SecretBackend):
    name = "keyring"

    def describe(self) -> str:
        if keyring is None:
            return "keyring"
        try:
            return f"keyring:{keyring.get_keyring().__class__.__name__}"
        except Exception:
            return "keyring"

    def is_available(self) -> bool:
        if keyring is None:
            return False
        try:
            keyring.get_keyring()
            return True
        except Exception as exc:
            logger.debug("keyring unavailable: %s", exc)
            return False

    def store(self, spec: SecretSpec, secret: str) -> bool:
        try:
            keyring.set_password(spec.keyring_service, spec.keyring_account, secret)
            return True
        except Exception as exc:
            logger.error("keyring store failed: %s", exc)
            return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        try:
            return keyring.get_password(spec.keyring_service, spec.keyring_account)
        except Exception as exc:
            logger.debug("keyring lookup failed: %s", exc)
            return None

    def delete(self, spec: SecretSpec) -> bool:
        try:
            keyring.delete_password(spec.keyring_service, spec.keyring_account)
            return True
        except Exception as exc:
            logger.debug("keyring delete failed: %s", exc)
            return False


class PassBackend(SecretBackend):
    """passwordstore.org backend (the ``pass`` CLI). Single-line secrets.

    Entries are stored at ``spec.pass_path`` (e.g. ``sshpilot/password/u@h``).
    Requires ``pass`` on PATH and an initialized store; gpg-agent handles unlock.

    Note: like the synchronous libsecret calls, a ``pass`` operation can block the
    calling thread while gpg-agent shows a pinentry prompt. A timeout guards
    against an indefinite hang; running secret I/O off the main thread is future
    work.
    """

    name = "pass"
    _TIMEOUT = 120  # seconds — generous enough for a pinentry prompt

    def __init__(self, pass_path: str = "pass") -> None:
        self._bin = shutil.which(pass_path) or None

    @staticmethod
    def _store_dir() -> str:
        return os.environ.get(
            "PASSWORD_STORE_DIR", os.path.expanduser("~/.password-store")
        )

    def is_available(self) -> bool:
        return bool(self._bin) and os.path.isdir(self._store_dir())

    def _run(self, args: List[str], *, input_text: Optional[str] = None):
        return subprocess.run(
            [self._bin] + args,
            input=(input_text.encode() if input_text is not None else None),
            capture_output=True,
            env=os.environ.copy(),
            check=False,
            timeout=self._TIMEOUT,
        )

    def store(self, spec: SecretSpec, secret: str) -> bool:
        try:
            result = self._run(["insert", "-m", "-f", spec.pass_path], input_text=secret)
            if result.returncode != 0:
                logger.error("pass insert failed: %s", result.stderr.decode("utf-8", "replace").strip())
            return result.returncode == 0
        except Exception as exc:
            logger.error("pass store failed: %s", exc)
            return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        try:
            result = self._run(["show", spec.pass_path])
            if result.returncode != 0:
                return None
            text = result.stdout.decode("utf-8", "replace")
            return text.split("\n", 1)[0] if text else None
        except Exception as exc:
            logger.debug("pass lookup failed: %s", exc)
            return None

    def delete(self, spec: SecretSpec) -> bool:
        try:
            result = self._run(["rm", "-f", spec.pass_path])
            return result.returncode == 0
        except Exception as exc:
            logger.debug("pass delete failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SecretManager:
    """Selects and orchestrates secret backends.

    - ``store`` — :meth:`_ordered_backends` only: first available backend in the
      selected order, falling back on failure (mirrors the old libsecret→keyring
      fallback).
    - ``lookup`` / ``delete`` — :meth:`_all_available_backends`: selected order
      first, then any other registered backend that is available, so secrets
      stored under a non-selected backend (e.g. ``pass`` while on ``auto``) still
      resolve and can be cleared.
    """

    def __init__(self) -> None:
        self._backends: Dict[str, SecretBackend] = {
            "libsecret": LibSecretBackend(),
            "keyring": KeyringBackend(),
            "pass": PassBackend(),
        }
        self._selected: Optional[str] = None  # resolved lazily

    # -- selection / registry --------------------------------------------
    def register_backend(self, name: str, backend: SecretBackend) -> None:
        """Register a custom backend (future stores / plugins)."""
        self._backends[name] = backend

    def set_selected(self, name: Optional[str]) -> None:
        self._selected = (name or "auto").strip().lower()

    def _selected_name(self) -> str:
        if self._selected is None:
            env = os.environ.get("SSHPILOT_SECRET_BACKEND")
            self._selected = (env or "auto").strip().lower()
        return self._selected

    @staticmethod
    def _platform_default_order() -> List[str]:
        return ["keyring"] if is_macos() else ["libsecret", "keyring"]

    def _ordered_names(self) -> List[str]:
        selected = self._selected_name()
        defaults = self._platform_default_order()
        if selected in ("", "auto"):
            order = list(defaults)
        else:
            order = [selected] + [n for n in defaults if n != selected]
        # de-dup while preserving order
        seen: set = set()
        return [n for n in order if not (n in seen or seen.add(n))]

    def _ordered_backends(self) -> List[SecretBackend]:
        result = []
        for name in self._ordered_names():
            backend = self._backends.get(name)
            if backend is not None and backend.is_available():
                result.append(backend)
        return result

    def _all_available_backends(self) -> List[SecretBackend]:
        """Backends to consult for read/clear: the selected+legacy order first,
        then any other registered backend that is available (e.g. ``pass`` while
        on ``auto``) so secrets stored under a non-selected backend are never
        orphaned."""
        result = self._ordered_backends()
        seen = {b.name for b in result}
        for name, backend in self._backends.items():
            if name not in seen and backend.is_available():
                result.append(backend)
                seen.add(name)
        return result

    def available_backends(self) -> List[str]:
        """Names of all registered backends that are currently usable."""
        return [n for n, b in self._backends.items() if b.is_available()]

    @property
    def active_backend_name(self) -> str:
        """The backend that ``store`` would use right now (for diagnostics/UI)."""
        backends = self._ordered_backends()
        return backends[0].name if backends else "none"

    def active_backend_label(self) -> str:
        """Like :attr:`active_backend_name` but with sub-backend detail (e.g.
        ``keyring:KWalletKeyring``) for logs/support."""
        backends = self._ordered_backends()
        return backends[0].describe() if backends else "none"

    # -- operations ------------------------------------------------------
    def store(self, spec: SecretSpec, secret: str) -> bool:
        for backend in self._ordered_backends():
            if backend.store(spec, secret):
                logger.debug("secret stored via %s", backend.name)
                return True
        logger.warning("No secure storage backend available; secret not stored")
        return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        for backend in self._all_available_backends():
            value = backend.lookup(spec)
            if value:
                return value
        return None

    def delete(self, spec: SecretSpec) -> bool:
        removed = False
        for backend in self._all_available_backends():
            if backend.delete(spec):
                removed = True
        return removed


_MANAGER: Optional[SecretManager] = None


def get_secret_manager() -> SecretManager:
    """Return the process-wide :class:`SecretManager` singleton."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = SecretManager()
    return _MANAGER
