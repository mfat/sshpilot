"""Pluggable secret/key storage backends.

sshPilot stores two kinds of secrets — host passwords and SSH key passphrases
(plugin secrets ride the password path). Historically each call site hard-wired
libsecret with a ``keyring`` fallback. This module abstracts that into a small
backend interface so alternative stores can be selected without touching call
sites.

Built-in backends:
- ``libsecret`` — the GNOME Secret Service (Linux), via ``gi.repository.Secret``.
  (KeePassXC also works through this backend when its GUI Secret Service
  integration is enabled — it speaks the same ``org.freedesktop.secrets`` API.)
- ``keyring``   — the cross-platform Python ``keyring`` library (macOS Keychain,
  Windows Credential Manager, KWallet, …).
- ``pass``      — passwordstore.org (``pass`` CLI, gpg-backed).
- ``bitwarden`` / ``vaultwarden`` — the ``bw`` CLI (Vaultwarden = self-hosted
  server URL). *Session-backed*: must be unlocked before secrets are readable;
  the unlock token (``BW_SESSION``) is cached in-process and exported to the env
  so the ``--askpass`` subprocess can read non-interactively.
- ``agent`` — the "don't store secrets" choice: a null backend that persists
  nothing and (when selected) is never read from. The user relies on ssh-agent
  and ssh's own prompts. Marked ``authoritative`` so the manager does not fall
  back to / read from other stores while it is selected.

Selection (``SecretManager.set_selected`` / config ``secrets.backend``):
- ``auto``  — platform default: macOS → keyring; Linux → libsecret then keyring.
- a name    — that backend first, then the platform defaults as store fallback.

Operations:
- ``store`` — first available backend in the selected order (with fallback on
  failure).
- ``lookup`` / ``delete`` — selected order first, then every other registered
  backend that is available (e.g. ``pass`` while on ``auto``), so secrets are
  not orphaned when the user switches backends.
- When the *selected* backend is ``authoritative`` (the ``agent`` null backend),
  ``store`` and ``lookup`` consult only it — no fallback, no fallthrough — so
  "don't store" truly stores/reads nothing. ``delete`` still clears every store
  so old secrets can be purged after switching.

The module is GTK-free so it can be imported by the ``--askpass`` subprocess; it
lazily imports ``Secret`` and ``keyring``.

**Backward compatibility:** the ``SecretSpec`` builders reproduce the exact
libsecret attributes / schema and keyring ``(service, account)`` keys used before
this module existed, so existing saved credentials keep working under ``auto``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
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

    #: Backend has a lock lifecycle that must be unlocked before secrets resolve
    #: (e.g. Bitwarden/Vaultwarden). Passive stores leave this ``False``.
    session_backed: bool = False

    #: When this backend is the *selected* one, the manager must NOT fall back to
    #: or read from other backends (the ``agent`` "don't store" null backend).
    authoritative: bool = False

    def describe(self) -> str:
        """Human/diagnostic label (may include sub-backend detail)."""
        return self.name

    def is_unlocked(self) -> bool:
        """Whether secrets can currently be read. Passive stores are always
        "unlocked"; session-backed backends override this."""
        return True

    def unlock(self, secret: str) -> bool:
        """Unlock a session-backed backend with ``secret`` (master password).
        No-op for passive stores."""
        return True

    def lock(self) -> None:
        """Drop any cached session/token. No-op for passive stores."""
        return None

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


class BitwardenBackend(SecretBackend):
    """Bitwarden backend driving the ``bw`` CLI.

    *Session-backed*: :meth:`unlock` runs ``bw unlock`` and caches the returned
    ``BW_SESSION`` token (in-process **and** in ``os.environ`` so the
    ``--askpass`` subprocess, which inherits the env, can read non-interactively).
    The token is dropped after ``secrets.session_timeout`` minutes of idle
    (propagated as ``SSHPILOT_SECRET_SESSION_TIMEOUT`` seconds); ``0`` = until exit.

    Secrets are stored as login items named after ``spec.keyring_account`` with the
    secret in ``login.password``. ``is_available`` only checks for the ``bw`` binary
    — never ``bw status`` — so it stays cheap on the hot path. The ``bw`` CLI holds
    one server/account at a time, so Bitwarden-cloud and a Vaultwarden server cannot
    be logged in simultaneously through the same install.
    """

    name = "bitwarden"
    session_backed = True
    _TIMEOUT = 120  # seconds — generous enough for a master-password unlock

    def __init__(self, bin_name: str = "bw") -> None:
        self._bin = shutil.which(bin_name) or None
        self._token: Optional[str] = None
        self._deadline: Optional[float] = None  # monotonic; None = no expiry
        self._lock = threading.RLock()

    # -- server (Vaultwarden overrides) ----------------------------------
    def _server_url(self) -> str:
        """Self-hosted server URL; empty = Bitwarden cloud."""
        return ""

    def describe(self) -> str:
        url = self._server_url()
        return f"{self.name}:{url}" if url else self.name

    def is_available(self) -> bool:
        return bool(self._bin)

    # -- session lifecycle ----------------------------------------------
    @staticmethod
    def _session_timeout_seconds() -> int:
        try:
            return int(os.environ.get("SSHPILOT_SECRET_SESSION_TIMEOUT", "0") or 0)
        except Exception:
            return 0

    def _touch_deadline(self) -> None:
        secs = self._session_timeout_seconds()
        self._deadline = (time.monotonic() + secs) if secs > 0 else None

    def _current_token(self) -> Optional[str]:
        """Return a usable session token, honoring the idle timeout.

        In a subprocess (no in-process token) fall back to an inherited
        ``BW_SESSION`` env var so the askpass helper works without unlocking.
        """
        with self._lock:
            if self._token is None:
                return os.environ.get("BW_SESSION") or None
            if self._deadline is not None and time.monotonic() > self._deadline:
                self._token = None
                self._deadline = None
                os.environ.pop("BW_SESSION", None)
                return None
            self._touch_deadline()  # sliding idle window
            return self._token

    def is_unlocked(self) -> bool:
        return self._current_token() is not None

    def unlock(self, secret: str) -> bool:
        if not self._bin:
            return False
        with self._lock:
            try:
                url = self._server_url()
                if url:
                    self._run(["config", "server", url])
                env = os.environ.copy()
                env["BW_MASTER"] = secret or ""
                result = subprocess.run(
                    [self._bin, "unlock", "--passwordenv", "BW_MASTER", "--raw"],
                    capture_output=True,
                    env=env,
                    check=False,
                    timeout=self._TIMEOUT,
                )
                if result.returncode != 0:
                    logger.error(
                        "bw unlock failed: %s",
                        result.stderr.decode("utf-8", "replace").strip(),
                    )
                    return False
                token = result.stdout.decode("utf-8", "replace").strip()
                if not token:
                    return False
                self._token = token
                self._touch_deadline()
                os.environ["BW_SESSION"] = token
                return True
            except Exception as exc:
                logger.error("bw unlock error: %s", exc)
                return False

    def lock(self) -> None:
        with self._lock:
            self._token = None
            self._deadline = None
            os.environ.pop("BW_SESSION", None)
            try:
                if self._bin:
                    subprocess.run(
                        [self._bin, "lock"],
                        capture_output=True,
                        check=False,
                        timeout=self._TIMEOUT,
                    )
            except Exception:
                pass

    # -- bw helpers ------------------------------------------------------
    def _run(self, args: List[str], *, token: Optional[str] = None,
             input_bytes: Optional[bytes] = None):
        env = os.environ.copy()
        if token:
            env["BW_SESSION"] = token
        return subprocess.run(
            [self._bin] + args,
            input=input_bytes,
            capture_output=True,
            env=env,
            check=False,
            timeout=self._TIMEOUT,
        )

    def _find_item_id(self, account: str, token: str) -> Optional[str]:
        """Return the id of the login item exactly named ``account`` (or None)."""
        try:
            result = self._run(["list", "items", "--search", account], token=token)
            if result.returncode != 0:
                return None
            items = json.loads(result.stdout.decode("utf-8", "replace") or "[]")
            for item in items:
                if item.get("name") == account:
                    return item.get("id")
        except Exception as exc:
            logger.debug("bw list items failed: %s", exc)
        return None

    @staticmethod
    def _encode(item: dict) -> bytes:
        return base64.b64encode(json.dumps(item).encode("utf-8"))

    def store(self, spec: SecretSpec, secret: str) -> bool:
        token = self._current_token()
        if not token or not self._bin:
            return False
        account = spec.keyring_account
        try:
            item_id = self._find_item_id(account, token)
            if item_id:
                got = self._run(["get", "item", item_id], token=token)
                if got.returncode != 0:
                    return False
                item = json.loads(got.stdout.decode("utf-8", "replace"))
                login = item.get("login") or {}
                login["password"] = secret
                item["login"] = login
                res = self._run(["edit", "item", item_id], token=token,
                                input_bytes=self._encode(item))
                return res.returncode == 0
            item = {
                "type": 1,  # login
                "name": account,
                "notes": None,
                "login": {"username": None, "password": secret},
            }
            res = self._run(["create", "item"], token=token,
                            input_bytes=self._encode(item))
            return res.returncode == 0
        except Exception as exc:
            logger.error("bw store failed: %s", exc)
            return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        token = self._current_token()
        if not token or not self._bin:
            return None
        try:
            result = self._run(["get", "password", spec.keyring_account], token=token)
            if result.returncode != 0:
                return None
            text = result.stdout.decode("utf-8", "replace").strip()
            return text or None
        except Exception as exc:
            logger.debug("bw lookup failed: %s", exc)
            return None

    def delete(self, spec: SecretSpec) -> bool:
        token = self._current_token()
        if not token or not self._bin:
            return False
        try:
            item_id = self._find_item_id(spec.keyring_account, token)
            if not item_id:
                return False
            res = self._run(["delete", "item", item_id, "--permanent"], token=token)
            return res.returncode == 0
        except Exception as exc:
            logger.debug("bw delete failed: %s", exc)
            return False


class VaultwardenBackend(BitwardenBackend):
    """Vaultwarden = self-hosted Bitwarden. Same ``bw`` CLI, configured server URL.

    The URL is read from ``SSHPILOT_VAULTWARDEN_SERVER`` (propagated from the
    ``secrets.vaultwarden.server`` setting); without it the backend is not offered.
    """

    name = "vaultwarden"

    def _server_url(self) -> str:
        return (os.environ.get("SSHPILOT_VAULTWARDEN_SERVER", "") or "").strip()

    def is_available(self) -> bool:
        return bool(self._bin) and bool(self._server_url())


class SSHAgentBackend(SecretBackend):
    """The "don't store secrets at all" choice.

    A null backend: it persists nothing and returns nothing. Because it is
    ``authoritative``, selecting it makes the manager skip every other store for
    reads/writes, so no secret lands in or is read from libsecret/keyring. The
    user relies on ssh-agent (keys loaded by the existing preload path or
    externally) and on ssh's own prompts. ``store`` returns ``True`` so the
    manager's fallback loop stops without writing anywhere.
    """

    name = "agent"
    authoritative = True

    def describe(self) -> str:
        return "agent" if os.environ.get("SSH_AUTH_SOCK") else "agent (no SSH_AUTH_SOCK)"

    def is_available(self) -> bool:
        return True

    def store(self, spec: SecretSpec, secret: str) -> bool:
        return True  # no-op success: stops the manager's store fallback

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        return None

    def delete(self, spec: SecretSpec) -> bool:
        return False  # nothing of ours; manager still clears the real stores


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
            "bitwarden": BitwardenBackend(),
            "vaultwarden": VaultwardenBackend(),
            "agent": SSHAgentBackend(),
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

    # -- selection helpers (session / authoritative) ---------------------
    def selected_backend(self) -> Optional[SecretBackend]:
        """The explicitly-selected backend object (None for ``auto``/unknown)."""
        name = self._selected_name()
        if name in ("", "auto"):
            return None
        return self._backends.get(name)

    def _authoritative_selected(self) -> Optional[SecretBackend]:
        """The selected backend iff it is authoritative and available (else None).

        When set, ``store``/``lookup`` consult only it — no fallback/fallthrough.
        """
        backend = self.selected_backend()
        if backend is not None and getattr(backend, "authoritative", False) \
                and backend.is_available():
            return backend
        return None

    def selected_needs_unlock(self) -> bool:
        """True when the selected backend is session-backed and currently locked,
        so the GTK layer should drive an unlock prompt."""
        backend = self.selected_backend()
        return bool(
            backend is not None
            and getattr(backend, "session_backed", False)
            and backend.is_available()
            and not backend.is_unlocked()
        )

    def unlock_selected(self, secret: str) -> bool:
        """Unlock the selected session-backed backend (no-op otherwise)."""
        backend = self.selected_backend()
        if backend is None or not getattr(backend, "session_backed", False):
            return True
        return backend.unlock(secret)

    def lock_all(self) -> None:
        """Drop every backend's cached session/token (e.g. on app shutdown)."""
        for backend in self._backends.values():
            try:
                backend.lock()
            except Exception:
                pass

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
        auth = self._authoritative_selected()
        backends = [auth] if auth else self._ordered_backends()
        for backend in backends:
            if backend.store(spec, secret):
                logger.debug("secret stored via %s", backend.name)
                return True
        logger.warning("No secure storage backend available; secret not stored")
        return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        auth = self._authoritative_selected()
        backends = [auth] if auth else self._all_available_backends()
        for backend in backends:
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
