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
- ``bitwarden`` — the ``bw`` CLI (covers Bitwarden cloud **and** self-hosted
  Vaultwarden, and any account, since which server/account it talks to is the CLI's
  own config + the optional ``BITWARDENCLI_APPDATA_DIR`` profile). *Session-backed*:
  must be unlocked before secrets are readable. The ``BW_SESSION`` token from unlock is
  kept in-process and exported to the env so the ``--askpass`` subprocess can read
  non-interactively; the whole vault is cached in memory on unlock so lookups need no
  further ``bw`` spawn.
- ``agent`` — the "don't store secrets" choice: a null backend that persists
  nothing and (when selected) is never read from. The user relies on ssh-agent
  and ssh's own prompts. Like any explicit selection it is exclusive, so the
  manager does not fall back to / read from other stores while it is selected.

Selection (``SecretManager.set_selected`` / config ``secrets.backend``):
- ``auto``  — platform default: macOS → keyring; Linux → libsecret then keyring.
- a name    — **only** that backend is used for ``store``/``lookup``/``delete``.

Operations:
- ``auto`` — ``store`` tries the platform order (libsecret then keyring on Linux);
  ``lookup`` and ``delete`` consult every available backend so secrets stored under
  a previous backend still resolve and can be cleared after switching.
- **Explicit selection** (any named backend, including ``agent`` and session-backed
  vaults) — ``store``/``lookup``/``delete`` consult **only** that backend: no
  fallback on store failure, no read-through to a stale copy in libsecret/keyring,
  and no cross-backend delete. A locked session vault simply returns nothing/False
  until unlocked; an unlocked vault with no matching item returns ``None``.
- The ``agent`` null backend is just a special case of explicit selection: being
  exclusive, "don't store" truly stores/reads nothing and ``delete`` is a no-op on
  other stores.
- If an explicitly-selected backend is **unavailable** (e.g. ``bitwarden`` with no
  ``bw`` on PATH), operations resolve to nothing/False **and a warning is logged** —
  the failure is surfaced, not silent.

The module is GTK-free so it can be imported by the ``--askpass`` subprocess; it
lazily imports ``Secret`` and ``keyring``.

**Backward compatibility:** the ``SecretSpec`` builders reproduce the exact
libsecret attributes / schema and keyring ``(service, account)`` keys used before
this module existed, so existing saved credentials keep working under ``auto``.
"""

from __future__ import annotations

import base64
import json
import functools
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _system_ca_bundle() -> Optional[str]:
    """Path to the OS CA-certificates bundle, or None if not found.

    The ``bw`` CLI is a Node app that verifies TLS against Node's own bundled
    Mozilla roots, ignoring the system trust store — so a Vaultwarden cert
    issued by a CA the user installed via ``update-ca-certificates`` is
    rejected as "self-signed certificate in certificate chain". Pointing
    ``NODE_EXTRA_CA_CERTS`` at the system bundle makes Node trust it too.
    """
    import ssl

    cafile = ssl.get_default_verify_paths().cafile
    if cafile and os.path.isfile(cafile):
        return cafile
    # OpenSSL default (get_default_verify_paths) sometimes reports only a dir;
    # fall back to the well-known distro bundle locations.
    for path in (
        "/etc/ssl/certs/ca-certificates.crt",       # Debian/Ubuntu, Alpine
        "/etc/pki/tls/certs/ca-bundle.crt",         # Fedora/RHEL
        "/etc/ssl/ca-bundle.pem",                   # openSUSE
    ):
        if os.path.isfile(path):
            return path
    return None

# Optional dependencies (libsecret / keyring / pykeepass) are resolved lazily so
# they never load on the startup import chain — pykeepass in particular pulls in
# lxml/argon2/pycryptodomex (~hundreds of ms) and is only needed by the KeePass
# backend. Each name starts as ``_UNSET`` ("not probed"); after the first probe it
# holds the real module/callable or ``None`` ("unavailable"). Tests inject fakes by
# setting these module globals directly (hence they stay plain module attributes).
_UNSET = object()

Secret = _UNSET
keyring = _UNSET
PyKeePass = _UNSET
_kdbx_create_database = _UNSET

# Fallback so `except CredentialsError:` always has a concrete type even when
# pykeepass is absent; ``_get_pykeepass`` swaps in pykeepass's real class when it
# loads (both the raiser and the handler read this module global at runtime, so
# they always agree).
class CredentialsError(Exception):
    pass


def _get_secret():
    """Return the ``gi.repository.Secret`` module, or ``None`` if unavailable."""
    global Secret
    if Secret is _UNSET:
        try:  # pragma: no cover - optional dependency
            import gi

            gi.require_version("Secret", "1")
            from gi.repository import Secret as _mod
            Secret = _mod
        except Exception:  # pragma: no cover - optional dependency
            Secret = None
    return Secret


def _get_keyring():
    """Return the ``keyring`` module, or ``None`` if unavailable."""
    global keyring
    if keyring is _UNSET:
        try:  # pragma: no cover - optional dependency
            import keyring as _mod
            keyring = _mod
        except Exception:  # pragma: no cover - optional dependency
            keyring = None
    return keyring


def _get_pykeepass():
    """Return the ``PyKeePass`` class, or ``None`` if pykeepass is unavailable.

    Also populates the ``_kdbx_create_database`` callable and swaps the module-level
    ``CredentialsError`` for pykeepass's real class on success.
    """
    global PyKeePass, _kdbx_create_database, CredentialsError
    if PyKeePass is _UNSET:
        try:  # pragma: no cover - optional dependency (KDBX backend)
            from pykeepass import PyKeePass as _P, create_database as _c
            from pykeepass.exceptions import CredentialsError as _CredErr
            PyKeePass, _kdbx_create_database, CredentialsError = _P, _c, _CredErr
        except Exception:  # pragma: no cover - optional dependency
            PyKeePass, _kdbx_create_database = None, None
    return PyKeePass


try:
    from .platform_utils import is_macos, resolve_host_binary
except Exception:  # pragma: no cover - fallback when run as a loose module
    try:
        from platform_utils import is_macos, resolve_host_binary  # type: ignore
    except Exception:
        def is_macos() -> bool:  # type: ignore
            import sys
            return sys.platform == "darwin"

        def resolve_host_binary(binary: str) -> Optional[List[str]]:  # type: ignore
            found = shutil.which(binary)
            return [found] if found else None


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
    Secret = _get_secret()
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


# --- SSH key path canonicalization (single source of truth) ------------------
# Shared by the askpass store/lookup path and by credential export, so the passphrase account
# is computed identically everywhere (two copies previously drifted and broke .spbk export).

def home_alias_for_path(path: str) -> str:
    """Home-relative alias (``~/...``) for an ABSOLUTE path under ``$HOME``, else ``''``. Realpaths
    ``$HOME`` so it matches even when the home dir has symlink components (e.g. macOS ``/Users``
    vs ``/System/Volumes/Data/Users``). Non-absolute input (already ``~``-relative, or a bare
    relative path) returns ``''`` — ``relpath`` on it would yield garbage."""
    if not path or not os.path.isabs(path):
        return ""
    try:
        home = os.path.realpath(os.path.expanduser("~"))
    except Exception:
        return ""
    if not home:
        return ""
    try:
        rel = os.path.relpath(path, home)
    except ValueError:
        return ""
    if rel in (".", os.curdir):
        return "~"
    if rel.startswith(".."):
        return ""
    return os.path.join("~", rel)


def normalize_key_path_for_storage(key_path: str) -> str:
    """Canonical passphrase account for an SSH key: a home-relative alias (``~/.ssh/id``) for keys
    under ``$HOME`` — so the same key resolves after moving a portable vault (KDBX/pass) to a
    machine with a different ``$HOME``/username — else the absolute realpath. Keys outside home
    can't be made portable and stay absolute."""
    if not (key_path or "").strip():
        return ""
    expanded = os.path.expanduser(key_path)
    try:
        resolved = os.path.realpath(expanded)
    except Exception:
        resolved = os.path.abspath(expanded)
    return home_alias_for_path(resolved) or resolved


def key_path_lookup_candidates(key_path: str) -> List[str]:
    """Account variants to probe for a key passphrase: the canonical (home-relative) title, the
    absolute expansion, home aliases, and the raw input — so both portable ``~`` entries and
    legacy absolute entries resolve, and passphrases saved under a previous backend still hit."""
    if not key_path:
        return []
    candidates: List[str] = []
    seen: set = set()

    def _add(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            candidates.append(path)

    canonical = normalize_key_path_for_storage(key_path)
    _add(canonical)
    expanded = os.path.expanduser(key_path)
    _add(expanded)
    for base in (canonical, expanded):
        _add(home_alias_for_path(base))
    _add(key_path)
    return candidates


def passphrase_spec(canonical_key_path: str) -> SecretSpec:
    """Spec for an SSH key passphrase. ``canonical_key_path`` must already be
    normalized by the caller (:func:`normalize_key_path_for_storage`)."""
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


def sudo_password_spec(host: str, username: str) -> SecretSpec:
    """Spec for a host's **sudo** password.

    Reproduces the exact legacy sudo keys so passwords saved before sudo went
    through the backend still resolve: the macOS keyring account ``sudo:user@host``
    and the libsecret ``type=sudo_password`` attributes. Kept distinct from the
    SSH login password (``type=ssh_password``) so the two never collide.
    """
    account = f"sudo:{username or ''}@{host or ''}"
    return SecretSpec(
        keyring_service=SERVICE_NAME,
        keyring_account=account,
        attributes={
            "application": SERVICE_NAME,
            "type": "sudo_password",
            "host": host,
            "username": username or "",
        },
        label=f"{SERVICE_NAME} sudo password: {username or ''}@{host or ''}",
        pass_path=f"sshpilot/sudo/{_pass_segment((username or '') + '@' + (host or ''))}",
    )


def _looks_like_key_path(account: str) -> bool:
    return account.startswith("/") or account.startswith("~") or (
        os.sep in account and "@" not in account
    )


def _split_user_host(text: str, username_hint: Optional[str] = None) -> Tuple[str, str]:
    """Split ``user@host`` into ``(user, host)``. ``username_hint`` (e.g. Bitwarden's
    ``login.username``) keeps this correct when the username itself contains ``@``
    (email-style SSH usernames); otherwise splits on the first ``@``."""
    if username_hint and text.startswith(username_hint + "@"):
        return username_hint, text[len(username_hint) + 1:]
    user, _, host = text.partition("@")
    return user, host


def parse_account(account: str, *, username_hint: Optional[str] = None) -> Dict[str, str]:
    """Reconstruct spec attributes (``type``/``host``/``username``/``key_path``) from a stored
    account key — the inverse of :func:`password_spec` / :func:`sudo_password_spec` /
    :func:`passphrase_spec`. For backends that key only by the account string (Bitwarden, pass);
    libsecret should use its real stored attributes instead. Returns ``{}`` if unrecognized."""
    account = account or ""
    if account.startswith("sudo:"):
        user, host = _split_user_host(account[len("sudo:"):], username_hint)
        return {"type": "sudo_password", "host": host, "username": user}
    if _looks_like_key_path(account):
        return {"type": "key_passphrase", "key_path": account}
    if "@" in account:
        user, host = _split_user_host(account, username_hint)
        return {"type": "ssh_password", "host": host, "username": user}
    return {}


def master_password_spec(backend_name: str = "bitwarden", profile: str = "") -> SecretSpec:
    """Spec for a session vault's **master password** (e.g. the Bitwarden unlock
    password), keyed by backend and account/profile so multiple accounts don't collide.

    This is the one secret that must NOT live in the session vault it unlocks (circular),
    so it is always stored in the platform keyring via
    :meth:`SecretManager.store_in_keyring`. ``type=vault_master`` keeps it distinct from
    host passwords, and the unique account id is carried in the schema's ``key_path``
    attribute (the libsecret schema has no ``backend``/``profile`` keys)."""
    account = f"{backend_name}-master:{profile or 'default'}"
    return SecretSpec(
        keyring_service=SERVICE_NAME,
        keyring_account=account,
        attributes={
            "application": SERVICE_NAME,
            "type": "vault_master",
            "key_path": account,
        },
        label=f"{SERVICE_NAME}: {backend_name} master password",
        pass_path=f"sshpilot/master/{_pass_segment(account)}",
    )


def selected_master_spec(manager: Optional["SecretManager"] = None) -> SecretSpec:
    """:func:`master_password_spec` for the currently-selected backend + profile —
    the single source of truth shared by the unlock dialog and Preferences so a saved
    password is stored, read, and forgotten under the same key."""
    mgr = manager if manager is not None else get_secret_manager()
    backend = mgr.selected_backend()
    name = (getattr(backend, "name", "") or "session").strip().lower()
    if name == "keepassxc":
        profile = os.environ.get("SSHPILOT_KDBX_DATABASE", "")   # keyed by the .kdbx path
    else:
        profile = os.environ.get("BITWARDENCLI_APPDATA_DIR", "")
    return master_password_spec(name, profile)


# ---------------------------------------------------------------------------
# Session idle timeout (shared by session-backed backends)
# ---------------------------------------------------------------------------


class _SessionIdleTimeout:
    """Sliding idle window for session-backed backends.

    Driven by ``SSHPILOT_SECRET_SESSION_TIMEOUT`` (seconds; ``0`` = no timeout).
    """

    def __init__(self) -> None:
        self._deadline: Optional[float] = None

    @staticmethod
    def timeout_seconds() -> int:
        try:
            return int(os.environ.get("SSHPILOT_SECRET_SESSION_TIMEOUT", "0") or 0)
        except Exception:
            return 0

    def touch(self) -> None:
        secs = self.timeout_seconds()
        self._deadline = (time.monotonic() + secs) if secs > 0 else None

    def expired(self) -> bool:
        return self._deadline is not None and time.monotonic() > self._deadline

    def reset(self) -> None:
        self._deadline = None


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
        Secret = _get_secret()
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
        Secret = _get_secret()
        if Secret is None:
            return False
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
        Secret = _get_secret()
        if Secret is None:
            return None
        try:
            return Secret.password_lookup_sync(get_schema(), spec.attributes, None)
        except Exception as exc:
            logger.error("libsecret lookup failed: %s", exc)
            return None

    def delete(self, spec: SecretSpec) -> bool:
        Secret = _get_secret()
        if Secret is None:
            return False
        try:
            return bool(Secret.password_clear_sync(get_schema(), spec.attributes, None))
        except Exception as exc:
            logger.error("libsecret delete failed: %s", exc)
            return False

    def iter_credentials(self) -> List[Tuple[Dict[str, str], Optional[str]]]:
        """Enumerate every sshPilot item in the Secret Service as ``(attributes, secret)``.

        Searches by the ``application`` attribute (under our schema), so it returns the real
        per-item attributes (``type``/``host``/``username``/``key_path``). The credential
        adapter's ``load_all()`` maps these to :class:`Credential`s."""
        Secret = _get_secret()
        if Secret is None or get_schema() is None:
            return []
        try:
            flags = (Secret.SearchFlags.ALL | Secret.SearchFlags.LOAD_SECRETS
                     | Secret.SearchFlags.UNLOCK)
            items = Secret.password_search_sync(
                get_schema(), {"application": SERVICE_NAME}, flags, None)
        except Exception as exc:
            logger.debug("libsecret search failed: %s", exc)
            return []
        out: List[Tuple[Dict[str, str], Optional[str]]] = []
        for item in items or []:
            try:
                attrs = dict(item.get_attributes() or {})
            except Exception:
                attrs = {}
            value = None
            try:
                secret = item.retrieve_secret_sync(None)
                if secret is not None:
                    value = secret.get_text()
            except Exception:
                value = None
            out.append((attrs, value))
        return out


class KeyringBackend(SecretBackend):
    name = "keyring"

    def describe(self) -> str:
        keyring = _get_keyring()
        if keyring is None:
            return "keyring"
        try:
            return f"keyring:{keyring.get_keyring().__class__.__name__}"
        except Exception:
            return "keyring"

    def is_available(self) -> bool:
        keyring = _get_keyring()
        if keyring is None:
            return False
        try:
            keyring.get_keyring()
            return True
        except Exception as exc:
            logger.debug("keyring unavailable: %s", exc)
            return False

    def store(self, spec: SecretSpec, secret: str) -> bool:
        keyring = _get_keyring()
        if keyring is None:
            return False
        try:
            keyring.set_password(spec.keyring_service, spec.keyring_account, secret)
            return True
        except Exception as exc:
            logger.error("keyring store failed: %s", exc)
            return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        keyring = _get_keyring()
        if keyring is None:
            return None
        try:
            return keyring.get_password(spec.keyring_service, spec.keyring_account)
        except Exception as exc:
            logger.debug("keyring lookup failed: %s", exc)
            return None

    def delete(self, spec: SecretSpec) -> bool:
        keyring = _get_keyring()
        if keyring is None:
            return False
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

    def iter_credentials(self) -> List[Tuple[Dict[str, str], Optional[str]]]:
        """Enumerate the ``sshpilot/`` subtree of the password store as ``(attributes,
        secret)``. The credential type comes from the path prefix (``password``/``sudo``/
        ``passphrase``); host/username are split from the segment (lossy for email-style
        usernames; passphrase key paths are lossy since ``/`` was sanitized to ``_``).
        One ``pass show`` (gpg decrypt) per entry — used only for enumeration/export."""
        if not self.is_available():
            return []
        store = self._store_dir()
        kinds = {"password": "ssh_password", "sudo": "sudo_password",
                 "passphrase": "key_passphrase"}
        out: List[Tuple[Dict[str, str], Optional[str]]] = []
        for sub, raw_type in kinds.items():
            base = os.path.join(store, "sshpilot", sub)
            if not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    if not fn.endswith(".gpg"):
                        continue
                    entry = os.path.relpath(os.path.join(root, fn), store)[:-4]   # strip .gpg
                    seg = entry[len(os.path.join("sshpilot", sub)) + 1:]
                    attrs: Dict[str, str] = {"type": raw_type}
                    if raw_type == "key_passphrase":
                        attrs["key_path"] = seg
                    else:
                        user, _, host = seg.partition("@")
                        attrs["host"] = host
                        attrs["username"] = user
                    value = None
                    try:
                        res = self._run(["show", entry])
                        if res.returncode == 0:
                            text = res.stdout.decode("utf-8", "replace")
                            value = text.split("\n", 1)[0] if text else None
                    except Exception:
                        value = None
                    out.append((attrs, value))
        return out


class BitwardenBackend(SecretBackend):
    """Bitwarden backend driving the ``bw`` CLI.

    *Session-backed*: :meth:`unlock` runs ``bw unlock`` and caches the returned
    ``BW_SESSION`` token (in-process **and** in ``os.environ`` so the ``--askpass``
    subprocess, which inherits the env, can read non-interactively). The token is kept
    until :meth:`lock` / shutdown, or until ``secrets.session_timeout`` minutes of idle
    (propagated as ``SSHPILOT_SECRET_SESSION_TIMEOUT`` seconds; ``0`` = no timeout, the
    default) — a sliding window refreshed on each use.

    On unlock the whole vault is fetched once (``bw list items``) into an in-memory
    name→item cache, so every lookup this session is served from memory with no further
    ``bw`` spawn. A cold miss (e.g. the askpass subprocess, which has no warm cache) does
    a targeted ``bw list items --search``. Secrets are login items named after
    ``spec.keyring_account`` with the secret in ``login.password``.

    ``is_available`` only checks for the ``bw`` binary — never ``bw status`` — so it stays
    cheap on the hot path. Under Flatpak, ``bw`` on the host is reached via
    ``flatpak-spawn --host`` (same as other host CLIs). The ``bw`` CLI holds one
    account/server per **data directory**; the user selects an account by pointing
    ``BITWARDENCLI_APPDATA_DIR`` at that data dir (``secrets.bitwarden.profile``) — under
    Flatpak this should be the **host** path (e.g. ``~/.config/Bitwarden CLI``), or left
    empty so the host ``bw`` uses its default. GTK-free (imported by the askpass subprocess);
    UI lives in the GTK layer.
    """

    name = "bitwarden"
    session_backed = True
    _TIMEOUT = 120  # seconds — generous enough for a master-password unlock

    def __init__(self, bin_name: str = "bw") -> None:
        self._bin_name = bin_name
        self._argv_override: Optional[List[str]] = None
        self._token: Optional[str] = None                # session token (this process)
        self._unlocked = False                           # we unlocked it this session
        self._needs_login: Optional[bool] = None         # stable CLI account state
        self._login_profile: Optional[str] = None        # profile that state belongs to
        self._idle = _SessionIdleTimeout()
        self._items: Optional[Dict[str, dict]] = None    # name→item; None = not loaded
        self._cache_complete = False                     # _items holds the whole vault
        self._folder_id: Optional[str] = None            # sshPilot folder id; None=unresolved, ''=none
        self._lock = threading.RLock()

    @property
    def _argv_prefix(self) -> Optional[List[str]]:
        """Argv prefix to spawn ``bw`` (direct path or ``flatpak-spawn --host bw``).

        Re-resolved on each access so a CLI installed after launch is found. Assigning
        ``self._bin`` pins an explicit path (used by tests)."""
        if self._argv_override is not None:
            return list(self._argv_override)
        try:
            # Non-verifying: never run ``bw --version`` here — this is accessed on the
            # GTK main thread (is_available(), command-building). Verified resolution
            # (bw --version) stays in is_bw_installed()/probe, which run off-thread.
            from .platform_utils import resolve_bw_cli_unverified
            return resolve_bw_cli_unverified()
        except Exception:
            return resolve_host_binary(self._bin_name)

    @property
    def _bin(self) -> Optional[str]:
        """Primary ``bw`` executable label (for logging / legacy checks)."""
        prefix = self._argv_prefix
        if not prefix:
            return None
        return prefix[-1]

    @_bin.setter
    def _bin(self, value: Optional[str]) -> None:
        self._argv_override = [value] if value else None

    def _bw_argv(self, *args: str) -> List[str]:
        prefix = self._argv_prefix
        if not prefix:
            raise RuntimeError("bw is not available")
        return prefix + ["--nointeraction"] + list(args)

    def describe(self) -> str:
        try:
            from .platform_utils import describe_bw_cli_source
            # verify=False: label only, must not spawn ``bw --version`` on the
            # GTK main thread (describe() runs in the synchronous startup banner).
            src = describe_bw_cli_source(verify=False)
            if src:
                return f"{self.name} ({src})"
        except Exception:
            pass
        return self.name

    def is_available(self) -> bool:
        # ``bw`` on PATH only (verified via ``bw --version``). The Bitwarden desktop
        # Flatpak bundles ``bw`` but it is not on PATH — install/setup handles that
        # separately. Lock/unlock state is separate (``is_unlocked()``).
        available = self._argv_prefix is not None
        return available

    def is_discoverable(self) -> bool:
        """Cheap presence check that never spawns ``bw --version``.

        For UI availability labels on the GTK main thread; ``is_available()`` verifies
        the binary runs (a Node spawn) and should stay off-thread."""
        from .platform_utils import bw_cli_discoverable
        return bw_cli_discoverable()

    # -- bw subprocess helper --------------------------------------------
    def _bw_command(self, args: List[str], env: dict) -> Tuple[List[str], Tuple[int, ...]]:
        """Build the full ``bw`` argv, forwarding env into Flatpak host spawns.

        ``flatpak-spawn --host`` does not inherit the sandbox environment, so
        secrets like ``BW_PASSWORD`` / ``BW_SESSION`` are forwarded via a
        ``--env-fd`` memfd — never ``--env=KEY=VALUE``, which would put the
        master password in the world-readable ``/proc/<pid>/cmdline``.

        Returns ``(argv, pass_fds)``; the caller must spawn with ``pass_fds``
        and close those fds afterwards.
        """
        argv = self._bw_argv(*args)
        try:
            from .platform_utils import inject_flatpak_host_env
        except Exception:  # pragma: no cover - loose-module fallback
            try:
                from platform_utils import inject_flatpak_host_env  # type: ignore
            except Exception:
                return argv, ()
        return inject_flatpak_host_env(argv, env)

    def _run(self, args: List[str], *, token: Optional[str] = None,
             input_bytes: Optional[bytes] = None, extra_env: Optional[dict] = None):
        """Run ``bw <args>`` non-interactively, returning the CompletedProcess.
        ``token`` (if given) is passed as ``BW_SESSION``. ``--nointeraction`` guarantees
        ``bw`` never blocks on stdin (a GUI must never hang on terminal input)."""
        prefix = self._argv_prefix
        if not prefix:
            raise RuntimeError("bw is not available")
        env = os.environ.copy()
        # Let bw (Node) trust CAs from the OS trust store, not just Node's
        # bundled Mozilla roots. The user's explicit setting always wins.
        if "NODE_EXTRA_CA_CERTS" not in env:
            bundle = _system_ca_bundle()
            if bundle:
                env["NODE_EXTRA_CA_CERTS"] = bundle
        if extra_env:
            env.update(extra_env)
        if token:
            env["BW_SESSION"] = token
        argv, pass_fds = self._bw_command(args, env)
        # Only pass ``pass_fds`` when needed so non-Flatpak spawns are untouched.
        spawn_kwargs = {"pass_fds": pass_fds} if pass_fds else {}
        started = time.monotonic()
        try:
            result = subprocess.run(
                argv,
                input=input_bytes, capture_output=True, env=env, check=False,
                timeout=self._TIMEOUT, **spawn_kwargs,
            )
        finally:
            for fd in pass_fds:
                os.close(fd)
        logger.debug("bw %s: %.2fs (rc=%s)",
                     " ".join(args[:2]), time.monotonic() - started, result.returncode)
        return result

    # -- login / unlock state --------------------------------------------
    def needs_login(self, *, force_refresh: bool = False) -> bool:
        """True when no account is authenticated (``bw login`` required), so ``bw unlock``
        cannot succeed.

        Account state is stable until login/logout, so cache it after the first CLI probe.
        Connection attempts call this path and must not each pay for a slow Node startup.
        Setup/status UI can use ``force_refresh`` after an out-of-process account change.
        """
        if not self._bin:
            return False
        profile = os.environ.get("BITWARDENCLI_APPDATA_DIR", "")
        with self._lock:
            if (
                self._needs_login is not None
                and self._login_profile == profile
                and not force_refresh
            ):
                return self._needs_login
            try:
                result = self._run(["login", "--check"])
                self._needs_login = result.returncode != 0
                self._login_profile = profile
                return self._needs_login
            except Exception as exc:
                logger.debug("bw login --check failed: %s", exc)
                return False

    def invalidate_login_state(self) -> None:
        """Forget the cached CLI account state before an explicit status refresh."""
        with self._lock:
            self._needs_login = None
            self._login_profile = None

    def _set_needs_login(self, needs_login: bool) -> None:
        """Record account state after an in-process login/logout operation."""
        with self._lock:
            self._needs_login = needs_login
            self._login_profile = os.environ.get("BITWARDENCLI_APPDATA_DIR", "")

    @staticmethod
    def _bw_cli_message(result) -> str:
        for stream in (result.stderr, result.stdout):
            text = (stream or b"").decode("utf-8", "replace").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _login_needs_2fa(message: str, *, twofa_code: Optional[str] = None) -> bool:
        """True when ``bw login`` failed because a two-step code is still needed.

        With ``--nointeraction``, the CLI cannot prompt for 2FA and often returns the
        generic ``Login failed.`` message instead of mentioning two-step login."""
        if (twofa_code or "").strip():
            return False
        lower = (message or "").lower().strip().rstrip(".")
        if lower == "login failed":
            return True
        return any(
            token in lower
            for token in (
                "two-step", "two step", "2fa", "two-factor",
                "verification code", "authenticator",
                # ``bw login`` (no --method) when 2FA is enabled: "No provider selected."
                "no provider selected",
            )
        )

    @staticmethod
    def _login_error_message(result) -> str:
        stdout = (result.stdout or b"").decode("utf-8", "replace").strip()
        try:
            data = json.loads(stdout)
            if isinstance(data, dict) and not data.get("success"):
                msg = data.get("message")
                if isinstance(msg, str) and msg.strip():
                    return msg.strip()
        except json.JSONDecodeError:
            pass
        return BitwardenBackend._bw_cli_message(result) or "login failed"

    @staticmethod
    def _extract_login_session(result) -> Optional[str]:
        stdout = (result.stdout or b"").decode("utf-8", "replace").strip()
        if not stdout:
            return None
        try:
            data = json.loads(stdout)
            if isinstance(data, dict):
                if not data.get("success"):
                    return None
                inner = data.get("data")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
                return None
        except json.JSONDecodeError:
            pass
        if result.returncode == 0 and not stdout.startswith("{"):
            return stdout
        return None

    def _store_session_token(self, token: str) -> None:
        """Persist a ``bw unlock`` / ``bw login --raw`` session key. Caller holds lock."""
        self._token = token
        self._unlocked = True
        os.environ["BW_SESSION"] = token
        self._touch_deadline()

    def login_with_password(
        self,
        email: str,
        password: str,
        *,
        twofa_method: Optional[str] = None,
        twofa_code: Optional[str] = None,
        auth_client_secret: Optional[str] = None,
    ) -> Tuple[bool, str, bool]:
        """Sign in with email and master password.

        Uses ``bw login … --raw`` so a session key is returned when the CLI unlocks
        the vault in the same step (per Bitwarden CLI docs). Returns
        ``(success, error_detail, needs_2fa)``.
        """
        if not self._argv_prefix:
            return False, "bw is not available", False
        email = (email or "").strip()
        if not email:
            return False, "email required", False
        args = ["login", email, "--passwordenv", "BW_PASSWORD", "--raw", "--response"]
        method = (twofa_method or "").strip()
        code = (twofa_code or "").strip()
        if method and code:
            args.extend(["--method", method, "--code", code])
        elif method:
            # Email 2FA: ``bw login --method 1`` (no code) triggers the verification email.
            args.extend(["--method", method])
        extra_env: Dict[str, str] = {"BW_PASSWORD": password or ""}
        challenge = (auth_client_secret or "").strip()
        if challenge:
            extra_env["BW_CLIENTSECRET"] = challenge
        result = self._run(args, extra_env=extra_env)
        if result.returncode == 0:
            self._set_needs_login(False)
            token = self._extract_login_session(result)
            if token:
                with self._lock:
                    self._store_session_token(token)
            return True, "", False
        detail = self._login_error_message(result)
        return False, detail, self._login_needs_2fa(detail, twofa_code=code)

    def login_with_api_key(self, client_id: str, client_secret: str) -> Tuple[bool, str]:
        """Sign in with a personal API key. Returns ``(success, error_detail)``."""
        if not self._argv_prefix:
            return False, "bw is not available"
        client_id = (client_id or "").strip()
        client_secret = (client_secret or "").strip()
        if not client_id or not client_secret:
            return False, "client id and secret required"
        result = self._run(
            ["login", "--apikey"],
            extra_env={"BW_CLIENTID": client_id, "BW_CLIENTSECRET": client_secret},
        )
        if result.returncode == 0:
            self._set_needs_login(False)
            return True, ""
        return False, self._bw_cli_message(result) or "login failed"

    def login_with_sso(self, identifier: Optional[str] = None) -> Tuple[bool, str]:
        """Start SSO sign-in (opens the system browser). Returns ``(success, error_detail)``."""
        if not self._argv_prefix:
            return False, "bw is not available"
        args = ["login", "--sso"]
        ident = (identifier or "").strip()
        if ident:
            args.append(ident)
        result = self._run(args)
        if result.returncode == 0:
            self._set_needs_login(False)
            return True, ""
        return False, self._bw_cli_message(result) or "sso login failed"

    def _touch_deadline(self) -> None:
        """Refresh the idle deadline (sliding window). Caller holds ``self._lock``."""
        self._idle.touch()

    def _expired(self) -> bool:
        """Caller holds ``self._lock``."""
        return self._idle.expired()

    def _drop_session(self) -> None:
        """Forget the in-process session + cache (re-lock). Caller holds ``self._lock``."""
        self._token = None
        self._unlocked = False
        self._idle.reset()
        self._items = None
        self._cache_complete = False
        self._folder_id = None
        os.environ.pop("BW_SESSION", None)

    def _current_token(self) -> Optional[str]:
        """A usable session token: the one from unlock() in this process (honoring the
        idle timeout), else an inherited ``BW_SESSION`` (so the askpass subprocess works
        without unlocking)."""
        with self._lock:
            if self._token is not None:
                if self._expired():
                    self._drop_session()
                else:
                    self._touch_deadline()   # sliding window on activity
                    return self._token
            return os.environ.get("BW_SESSION") or None

    def is_unlocked(self) -> bool:
        # Trust our own successful unlock (no re-prompt across connects) until it idles
        # out. In a subprocess an inherited BW_SESSION means the vault is usable.
        with self._lock:
            if self._unlocked:
                if self._expired():
                    self._drop_session()
                else:
                    self._touch_deadline()
                    return True
        return bool(os.environ.get("BW_SESSION"))

    # -- unlock / lock ---------------------------------------------------
    @staticmethod
    def _report(progress, stage: str) -> None:
        if callable(progress):
            try:
                progress(stage)
            except Exception:
                pass

    def unlock(self, secret: str, progress: Optional[Callable[[str], None]] = None) -> bool:
        if not self._argv_prefix:
            return False
        with self._lock:
            # Already unlocked AND fully cached (e.g. a background startup warm-up just
            # finished): no-op. The RLock serialized us behind that warm-up, so the startup
            # spinner's unlock simply adopts its result instead of spawning a redundant
            # second `bw unlock` + re-list.
            if self._unlocked and self._cache_complete and not self._expired():
                self._touch_deadline()
                return True
            try:
                self._report(progress, "starting")
                # We do NOT run `bw config server` here: `bw` rejects a server change while
                # logged in (and you must be logged in for `unlock` to succeed), so it only
                # ever burned a ~1-2s spawn. Vaultwarden's server is configured by the user
                # via the CLI (`bw config server <url>` then `bw login`); the app's URL
                # setting just gates availability and labels the backend.
                self._report(progress, "unlocking")
                # Use _run so Flatpak host spawns get ``--env=BW_PASSWORD=…`` —
                # ``flatpak-spawn --host`` does not forward sandbox env, and host
                # ``bw`` then fails with "Master password is required."
                result = self._run(
                    ["unlock", "--passwordenv", "BW_PASSWORD", "--raw"],
                    extra_env={"BW_PASSWORD": secret or ""},
                )
                if result.returncode != 0:
                    logger.error("bw unlock failed: %s",
                                 result.stderr.decode("utf-8", "replace").strip())
                    return False
                token = result.stdout.decode("utf-8", "replace").strip()
                if not token:
                    return False
                self._needs_login = False
                self._store_session_token(token)
                self._report(progress, "loading")
                # Warm the whole-vault cache now, while the unlock spinner is still up, so
                # every lookup this session is an in-memory hit (no per-connect bw spawn).
                # The caller keeps the spinner open until this returns. A failed load
                # leaves an empty, non-complete cache so lookups fall back to a search.
                self._items = {}
                self._cache_complete = False
                loaded = self._load_all_items(token)
                if loaded is not None:
                    self._items = loaded
                    self._cache_complete = True
                self._start_background_sync(token)
                return True
            except Exception as exc:
                logger.error("bw unlock error: %s", exc)
                return False

    def _start_background_sync(self, token: str) -> None:
        """Refresh the on-disk vault from the server off-thread (``bw sync``, a network
        pull only — no re-list/decrypt). The cache from unlock serves this session; sync
        just keeps local data current for next launch. Best-effort."""
        def _bg():
            try:
                self._run(["sync"], token=token)
            except Exception as exc:
                logger.debug("bw background sync failed: %s", exc)
        threading.Thread(target=_bg, daemon=True).start()

    def lock(self) -> None:
        # Drop the in-process session + cache only — instant. We never persist the
        # session key, so discarding it fully disables further use; spawning ``bw lock``
        # would add a slow Node subprocess to shutdown.
        with self._lock:
            self._drop_session()

    def logout(self) -> bool:
        """Sign out of the Bitwarden account (``bw logout``) and drop the session."""
        with self._lock:
            self._drop_session()
        if not self._argv_prefix:
            return True
        try:
            result = self._run(["logout"])
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
                logger.error("bw logout failed: %s", detail)
                return False
            self._set_needs_login(True)
            return True
        except Exception as exc:
            logger.error("bw logout error: %s", exc)
            return False

    # -- secure notes (used as a backup destination; not login-item secrets) ---------
    def create_or_update_secure_note(self, name: str, content: str) -> Optional[str]:
        """Create or update a **secure note** (item type 2) named ``name`` with ``content`` in its
        ``notes`` field. Content goes in via base64-on-stdin (no file path — Flatpak-safe). Returns
        the item id, or ``None`` on failure. Used by the Bitwarden backup destination."""
        token = self._current_token()
        if not token or not self._bin:
            return None
        try:
            existing = self._search_item(name, token)
            if existing and existing.get("id"):
                item = dict(existing)
                item["type"] = 2
                item["notes"] = content
                item.setdefault("secureNote", {"type": 0})
                res = self._run(["edit", "item", item["id"]], token=token,
                                input_bytes=self._encode(item))
                return item["id"] if res.returncode == 0 else None
            item = {"type": 2, "name": name, "notes": content, "secureNote": {"type": 0}}
            fid = self._ensure_folder_id(token)
            if fid:
                item["folderId"] = fid
            res = self._run(["create", "item"], token=token, input_bytes=self._encode(item))
            if res.returncode != 0:
                return None
            created = json.loads(res.stdout.decode("utf-8", "replace") or "{}")
            return created.get("id") if isinstance(created, dict) else None
        except Exception as exc:
            logger.error("bw secure-note store failed: %s", exc)
            return None

    def list_secure_notes(self, name_prefix: str) -> List[dict]:
        """Secure-note items whose name starts with ``name_prefix`` (e.g. our backup items)."""
        token = self._current_token()
        if not token:
            return []
        try:
            result = self._run(["list", "items", "--search", name_prefix], token=token)
            if result.returncode != 0:
                return []
            return [it for it in json.loads(result.stdout.decode("utf-8", "replace") or "[]")
                    if it.get("type") == 2 and str(it.get("name", "")).startswith(name_prefix)]
        except Exception as exc:
            logger.debug("bw list secure notes failed: %s", exc)
            return []

    def read_secure_note(self, item_id: str) -> Optional[str]:
        """Return the ``notes`` content of the secure-note item ``item_id``."""
        token = self._current_token()
        if not token:
            return None
        try:
            result = self._run(["get", "item", item_id], token=token)
            if result.returncode != 0:
                return None
            item = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
            return item.get("notes")
        except Exception as exc:
            logger.debug("bw read secure note failed: %s", exc)
            return None

    # -- sshPilot folder (organizes the items we create) -----------------------
    def _ensure_folder_id(self, token: str) -> Optional[str]:
        """The id of the ``sshPilot`` folder, resolving/creating it once per session. Returns
        ``None`` (item lands in No Folder) on any failure — never fatal to a store."""
        with self._lock:
            if self._folder_id is not None:
                return self._folder_id or None      # '' sentinel => resolved, absent
        fid = self._resolve_folder(token) or ""
        with self._lock:
            self._folder_id = fid
        return fid or None

    def _resolve_folder(self, token: str) -> Optional[str]:
        try:
            result = self._run(["list", "folders"], token=token)
            if result.returncode == 0:
                for folder in json.loads(result.stdout.decode("utf-8", "replace") or "[]"):
                    if folder.get("name") == SERVICE_NAME and folder.get("id"):
                        return folder["id"]
            # Not found — create it (base64-on-stdin, same as `create item`).
            res = self._run(["create", "folder"], token=token,
                            input_bytes=self._encode({"name": SERVICE_NAME}))
            if res.returncode == 0:
                created = json.loads(res.stdout.decode("utf-8", "replace") or "{}")
                if isinstance(created, dict):
                    return created.get("id")
        except Exception as exc:
            logger.debug("bw folder resolve/create failed: %s", exc)
        return None

    # -- item cache + operations -----------------------------------------
    def _load_all_items(self, token: str) -> Optional[Dict[str, dict]]:
        """One ``bw list items`` (whole vault) → name→item map. ``None`` on failure so a
        transient error doesn't poison the cache as 'complete'."""
        try:
            result = self._run(["list", "items"], token=token)
            if result.returncode != 0:
                logger.debug("bw list items rc=%s", result.returncode)
                return None
            items: Dict[str, dict] = {}
            for item in json.loads(result.stdout.decode("utf-8", "replace") or "[]"):
                name = item.get("name")
                if isinstance(name, str):
                    items[name] = item
            return items
        except Exception as exc:
            logger.debug("bw list items failed: %s", exc)
            return None

    def _search_item(self, account: str, token: str) -> Optional[dict]:
        """Targeted ``bw list items --search <account>`` for one item by exact name. Used
        on a cold cache (e.g. the askpass subprocess) to avoid pulling the whole vault."""
        try:
            result = self._run(["list", "items", "--search", account], token=token)
            if result.returncode != 0:
                return None
            for item in json.loads(result.stdout.decode("utf-8", "replace") or "[]"):
                if item.get("name") == account:
                    return item
        except Exception as exc:
            logger.debug("bw search failed: %s", exc)
        return None

    def _find_item(self, account: str) -> Optional[dict]:
        """Return the item named ``account``. Served from the cache when present; when
        the whole vault is cached (``_cache_complete``) a miss is definitive (no bw
        spawn). Otherwise (cold cache, e.g. the askpass subprocess) fall back to one
        targeted search and cache the hit."""
        with self._lock:
            if self._items is not None and account in self._items:
                return self._items[account]
            if self._cache_complete:
                return None
        token = self._current_token()
        if not token:
            return None
        item = self._search_item(account, token)
        if item is not None:
            self._cache_put(account, item)
        return item

    @staticmethod
    def _fill_login_metadata(login: dict, spec: SecretSpec, *, overwrite: bool) -> None:
        """Populate ``login.username`` / ``login.uris`` from the spec attributes.
        With ``overwrite=False`` only fills empty fields (never clobbers user edits)."""
        attrs = spec.attributes or {}
        username = attrs.get("username")
        if username and (overwrite or not login.get("username")):
            login["username"] = username
        host = attrs.get("host")
        if host and (overwrite or not login.get("uris")):
            login["uris"] = [{"match": None, "uri": f"ssh://{host}"}]

    def _new_item(self, account: str, secret: str, spec: SecretSpec) -> dict:
        login = {"username": None, "password": secret}
        self._fill_login_metadata(login, spec, overwrite=True)
        return {
            "type": 1,  # login
            "name": account,
            "notes": "Saved by SSH Pilot",
            "login": login,
        }

    @staticmethod
    def _encode(item: dict) -> bytes:
        """``bw create/edit item`` reads the item JSON as base64 on stdin."""
        return base64.b64encode(json.dumps(item).encode("utf-8"))

    def store(self, spec: SecretSpec, secret: str) -> bool:
        # Serialize cache lookup + create/edit as one transaction. UI saves run in
        # workers, and two concurrent misses must not create duplicate vault items.
        with self._lock:
            return self._store_locked(spec, secret)

    def _store_locked(self, spec: SecretSpec, secret: str) -> bool:
        token = self._current_token()
        if not token or not self._bin:
            return False
        account = spec.keyring_account
        try:
            existing = self._find_item(account)
            if existing and existing.get("id"):
                item = dict(existing)
                login = dict(item.get("login") or {})
                login["password"] = secret
                self._fill_login_metadata(login, spec, overwrite=False)
                item["login"] = login
                if not item.get("notes"):
                    item["notes"] = "Saved by SSH Pilot"
                res = self._run(["edit", "item", item["id"]], token=token,
                                input_bytes=self._encode(item))
                if res.returncode != 0:
                    return False
                self._cache_put(account, item)
                return True
            item = self._new_item(account, secret, spec)
            fid = self._ensure_folder_id(token)
            if fid:
                item["folderId"] = fid
            res = self._run(["create", "item"], token=token,
                            input_bytes=self._encode(item))
            if res.returncode != 0:
                return False
            try:
                created = json.loads(res.stdout.decode("utf-8", "replace") or "{}")
            except Exception:
                created = item
            self._cache_put(account, created if isinstance(created, dict) else item)
            return True
        except Exception as exc:
            logger.error("bw store failed: %s", exc)
            return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        # Warm-cache hit (instant) or, on a cold cache, one targeted `bw list` search.
        item = self._find_item(spec.keyring_account)
        if not item:
            return None
        return (item.get("login") or {}).get("password") or None

    def iter_credentials(self) -> List[Tuple[Dict[str, str], Optional[str]]]:
        """Enumerate the cached vault (after unlock) as ``(attributes, secret)``. Empty when
        locked or not cached. Reconstructs spec attributes from each item's name, using
        ``login.username`` as the hint so email-style usernames split correctly."""
        with self._lock:
            items = self._items
            snapshot = list(items.items()) if items else []
        out: List[Tuple[Dict[str, str], Optional[str]]] = []
        for name, item in snapshot:
            login = (item.get("login") or {}) if isinstance(item, dict) else {}
            attrs = parse_account(name, username_hint=login.get("username") or None)
            if not attrs:
                continue
            out.append((attrs, login.get("password")))
        return out

    def delete(self, spec: SecretSpec) -> bool:
        with self._lock:
            return self._delete_locked(spec)

    def _delete_locked(self, spec: SecretSpec) -> bool:
        token = self._current_token()
        if not token or not self._bin:
            return False
        account = spec.keyring_account
        try:
            existing = self._find_item(account)
            item_id = existing.get("id") if existing else None
            if not item_id:
                return False
            res = self._run(["delete", "item", item_id, "--permanent"], token=token)
            if res.returncode != 0:
                return False
            self._cache_drop(account)
            return True
        except Exception as exc:
            logger.debug("bw delete failed: %s", exc)
            return False

    def _cache_put(self, account: str, item: dict) -> None:
        with self._lock:
            if self._items is None:
                self._items = {}
            self._items[account] = item

    def _cache_drop(self, account: str) -> None:
        with self._lock:
            if self._items is not None:
                self._items.pop(account, None)


class RbwBackend(SecretBackend):
    """Bitwarden backend driving the ``rbw`` CLI (https://github.com/doy/rbw).

    A lighter, agent-based alternative to the ``bw`` :class:`BitwardenBackend`,
    kept entirely separate from it. ``rbw`` talks to the same Bitwarden/Vaultwarden
    vault but delegates the whole unlock lifecycle to its own ``rbw-agent`` +
    pinentry, so this backend is **passive** (``session_backed = False``): sshPilot
    never drives unlock — the agent stays unlocked on its own schedule
    (``rbw config set lock_timeout``). Every operation is gated on ``rbw unlocked``
    so a locked vault quietly resolves to nothing/``False`` and never triggers a
    surprise pinentry prompt on the ``auto`` read-through path.

    Secrets are login items named after ``spec.keyring_account`` (matching the ``bw``
    backend) with the secret on the first line — ``rbw``'s editor convention, the rest
    being a note. ``rbw add``/``edit`` read that body from **stdin** when stdin is not a
    TTY, so no ``$EDITOR`` shim is needed; existing items are updated via ``rbw edit``
    to avoid duplicates. Every item lives in a dedicated ``sshpilot`` folder (all ops
    are ``--folder``-scoped) so sshPilot's entries never collide with, or clutter, the
    user's own vault logins; ``rbw`` creates the folder on first ``add``. GTK-free
    (importable by the askpass subprocess). Under Flatpak the host ``rbw`` is reached
    via ``flatpak-spawn --host`` like the other host CLIs.
    """

    name = "rbw"
    _TIMEOUT = 120  # seconds — generous enough for a network sync / pinentry
    _FOLDER = SERVICE_NAME  # same vault folder as the ``bw`` backend, so both see the
    #                         same items when pointed at one vault (and it stays isolated
    #                         from the user's own logins)

    # A big Bitwarden vault makes every `rbw get` slow: the CLI decrypts all entry
    # names via per-cipher agent IPC (≈1s at a few thousand items), so a dialog that
    # probes several keys/candidate paths — mostly *misses* — freezes the UI for
    # seconds. Cache the set of item names in our folder from one `rbw list` and
    # answer misses (and `_exists`) from it; only a real hit pays a `get`.
    _NAMES_TTL = 30.0     # seconds; store/delete invalidate immediately
    # Resolved-secret cache lifetime. In-app store/delete invalidate immediately and a
    # locked agent never serves it (peek() gates on is_unlocked), but an *external* vault
    # edit (changing a passphrase in Bitwarden/rbw directly) is not seen until this
    # expires — kept short enough to bound that staleness while still covering a burst of
    # reconnects.
    _VALUES_TTL = 300.0

    def __init__(self) -> None:
        self._argv_override: Optional[List[str]] = None
        self._names: Optional[set] = None      # item names in our folder; None = unfilled
        self._names_at: float = 0.0
        self._names_lock = threading.Lock()
        # Resolved secret values kept in the (long-lived) main process so repeat lookups
        # — and the askpass IPC fast path — skip the ~1s `rbw get`. Never populated in the
        # short-lived askpass subprocess (fresh instance each spawn). Guarded separately
        # from _names so a cache peek never blocks behind a slow `rbw list`.
        self._values: Dict[str, Tuple[str, float]] = {}   # name -> (value, monotonic ts)
        self._values_lock = threading.Lock()

    @property
    def _argv_prefix(self) -> Optional[List[str]]:
        """Argv prefix to spawn ``rbw`` (direct path or ``flatpak-spawn --host rbw``),
        re-resolved on each access so a CLI installed after launch is still found."""
        if self._argv_override is not None:
            return list(self._argv_override)
        return resolve_host_binary("rbw")

    @property
    def _bin(self) -> Optional[str]:
        prefix = self._argv_prefix
        return prefix[-1] if prefix else None

    @_bin.setter
    def _bin(self, value: Optional[str]) -> None:
        self._argv_override = [value] if value else None

    def describe(self) -> str:
        return self.name

    def is_available(self) -> bool:
        # Binary presence only (cheap, no subprocess) — keeps availability off the hot
        # path. Unlock state is separate and checked per operation via `rbw unlocked`.
        return self._argv_prefix is not None

    def _run(self, *args: str, input_text: Optional[str] = None):
        prefix = self._argv_prefix
        if not prefix:
            raise RuntimeError("rbw is not available")
        return subprocess.run(
            prefix + list(args),
            input=(input_text.encode() if input_text is not None else None),
            capture_output=True,
            env=os.environ.copy(),
            check=False,
            timeout=self._TIMEOUT,
        )

    def is_unlocked(self) -> bool:
        """Whether ``rbw-agent`` currently holds the vault unlocked. Never prompts.

        Overrides the passive-store default (always ``True``) with the real agent state
        so callers can detect a locked vault — but ``session_backed`` stays ``False``, so
        this never routes rbw through the password-collecting unlock dialog (rbw's own
        pinentry owns unlock). The GTK connect path uses it to nudge via pinentry."""
        try:
            return self._run("unlocked").returncode == 0
        except Exception:
            return False

    def _folder_names(self) -> Optional[set]:
        """Names of items in our folder, cached for ``_NAMES_TTL``. One ``rbw list``
        (decrypts every name once) instead of one ``rbw get`` per lookup. ``None`` on
        failure so callers fall back to a direct ``get``. Serialized so a burst of
        concurrent misses triggers a single list, not one per miss."""
        with self._names_lock:
            if self._names is not None and (time.monotonic() - self._names_at) < self._NAMES_TTL:
                return self._names
            try:
                res = self._run("list", "--fields", "name,folder")
                if res.returncode != 0:
                    return None
                names = set()
                for line in res.stdout.decode("utf-8", "replace").splitlines():
                    name, _, folder = line.rpartition("\t")  # folder is the last field
                    if name and folder == self._FOLDER:
                        names.add(name)
                self._names = names
                self._names_at = time.monotonic()
                return names
            except Exception as exc:
                logger.debug("rbw list failed: %s", exc)
                return None

    def _invalidate_names(self) -> None:
        with self._names_lock:
            self._names = None

    def _exists(self, name: str) -> bool:
        names = self._folder_names()
        if names is not None:
            return name in names
        try:  # cache unavailable — fall back to a direct probe
            return self._run("get", "--folder", self._FOLDER, name).returncode == 0
        except Exception:
            return False

    def store(self, spec: SecretSpec, secret: str) -> bool:
        if not self.is_unlocked():
            return False
        name = spec.keyring_account
        # rbw's editor convention: first line = password, the rest = note. rbw reads
        # this from stdin because our stdin is a pipe (not a TTY). Update in place with
        # `edit` when the item already exists so re-saving never leaves a duplicate.
        body = f"{secret}\nSaved by SSH Pilot\n"
        try:
            verb = "edit" if self._exists(name) else "add"
            res = self._run(verb, "--folder", self._FOLDER, name, input_text=body)
            if res.returncode != 0:
                logger.error(
                    "rbw %s failed: %s", verb,
                    res.stderr.decode("utf-8", "replace").strip(),
                )
            else:
                self._invalidate_names()  # a new name may now exist in our folder
                with self._values_lock:
                    self._values.pop(name, None)  # re-read the new value next time
            return res.returncode == 0
        except Exception as exc:
            logger.error("rbw store failed: %s", exc)
            return False

    def _cached_value(self, name: str) -> Optional[str]:
        """Cache-only value read with no unlock check — for callers that already
        verified the agent is unlocked (e.g. :meth:`lookup`)."""
        with self._values_lock:
            hit = self._values.get(name)
            if hit and (time.monotonic() - hit[1]) < self._VALUES_TTL:
                return hit[0]
            if hit:
                self._values.pop(name, None)
        return None

    def peek(self, name: str) -> Optional[str]:
        """Cache-only value read for the askpass IPC fast path — never spawns ``rbw get``.

        **Gated on the live agent state.** rbw can lock on its own ``lock_timeout`` (or via
        the Preferences ``Lock`` button, which runs ``rbw lock`` directly), neither of which
        clears this cache — so a locked vault must not keep serving cached secrets. The
        ``rbw unlocked`` check (~ms) enforces that while staying far cheaper than a ``get``."""
        if not self.is_unlocked():
            return None
        return self._cached_value(name)

    def lock(self) -> None:
        """Drop our in-memory secret/name caches. rbw-agent owns the real lock lifecycle;
        this wipes what we cached so plaintext doesn't linger after an explicit app lock.
        (peek() already refuses to serve once the agent is locked, regardless.)"""
        with self._values_lock:
            self._values.clear()
        self._invalidate_names()

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        if not self.is_unlocked():
            return None
        name = spec.keyring_account
        cached = self._cached_value(name)  # unlock verified above
        if cached is not None:
            return cached
        names = self._folder_names()
        if names is not None and name not in names:
            return None  # fast miss — no slow `rbw get` for an item we know isn't there
        try:
            res = self._run("get", "--folder", self._FOLDER, name)
            if res.returncode != 0:
                return None
            value = res.stdout.decode("utf-8", "replace").split("\n", 1)[0] or None
            if value is not None:
                with self._values_lock:
                    self._values[name] = (value, time.monotonic())
            return value
        except Exception as exc:
            logger.debug("rbw lookup failed: %s", exc)
            return None

    def delete(self, spec: SecretSpec) -> bool:
        if not self.is_unlocked():
            return False
        try:
            ok = self._run("remove", "--folder", self._FOLDER, spec.keyring_account).returncode == 0
            if ok:
                self._invalidate_names()
                with self._values_lock:
                    self._values.pop(spec.keyring_account, None)
            return ok
        except Exception as exc:
            logger.debug("rbw delete failed: %s", exc)
            return False


class KdbxBackend(SecretBackend):
    """KeePass database (``.kdbx``) backend via ``pykeepass`` — a connect-time, read-write store.

    KDBX is a static encrypted file (no session in the format); we keep a per-launch in-memory
    "session" purely for usability: :meth:`unlock` opens the file with the master password (+
    optional key file), caches the opened DB, and exports the derived ``transformed_key`` as
    ``SSHPILOT_KDBX_KEY`` so the ``--askpass`` subprocess can open the file fast (no re-running
    Argon2) without re-prompting — the same env posture as Bitwarden's ``BW_SESSION``. The
    database/keyfile paths come from the env (``SSHPILOT_KDBX_DATABASE``/``_KEYFILE``), set from
    config by the connection manager / Preferences. Entries live in a dedicated ``sshPilot``
    group, titled by the secret account; the sshPilot type is kept in a custom property.
    """

    name = "keepassxc"
    session_backed = True
    _GROUP = "sshPilot"
    _TYPE_PROP = "sshpilot_type"
    _ENV_DB = "SSHPILOT_KDBX_DATABASE"
    _ENV_KEYFILE = "SSHPILOT_KDBX_KEYFILE"
    _ENV_KEY = "SSHPILOT_KDBX_KEY"

    def __init__(self) -> None:
        self._kp = None
        self._cache: Optional[Dict[str, Optional[str]]] = None   # title -> password
        self._idle = _SessionIdleTimeout()
        self._lock = threading.RLock()

    # -- config (paths come from env, set by connection_manager / preferences) ----
    def _database(self) -> str:
        return os.path.expanduser(os.environ.get(self._ENV_DB, "") or "")

    def _keyfile(self) -> Optional[str]:
        kf = os.environ.get(self._ENV_KEYFILE, "") or ""
        return os.path.expanduser(kf) if kf else None

    def describe(self) -> str:
        return self.name

    def is_available(self) -> bool:
        db = self._database()
        return _get_pykeepass() is not None and bool(db) and os.path.exists(db)

    def _touch_deadline(self) -> None:
        self._idle.touch()

    def _expired(self) -> bool:
        return self._idle.expired()

    def _drop_session(self) -> None:
        """Forget the in-process DB + cache + derived key. Caller holds ``self._lock``."""
        self._kp = None
        self._cache = None
        self._idle.reset()
        os.environ.pop(self._ENV_KEY, None)

    @staticmethod
    def _report(progress, stage: str) -> None:
        if callable(progress):
            try:
                progress(stage)
            except Exception:
                pass

    # -- open / unlock ----
    def _open(self, *, password=None, transformed_key=None):
        PyKeePass = _get_pykeepass()
        if PyKeePass is None:
            raise RuntimeError("pykeepass is not installed")
        db = self._database()
        if not db:
            raise RuntimeError("no KeePass database configured")
        if transformed_key is not None:
            return PyKeePass(db, transformed_key=transformed_key)
        return PyKeePass(db, password=(password or None), keyfile=self._keyfile())

    def _db(self):
        """The opened DB: our in-process one (honoring the idle timeout), else one opened from
        an inherited ``SSHPILOT_KDBX_KEY`` (the askpass subprocess). ``None`` if unavailable."""
        with self._lock:
            if self._kp is not None:
                if self._expired():
                    self._drop_session()
                else:
                    self._touch_deadline()
                    return self._kp
            key_b64 = os.environ.get(self._ENV_KEY)
            if key_b64:
                try:
                    return self._open(transformed_key=base64.b64decode(key_b64))
                except Exception as exc:
                    logger.debug("KDBX open from env key failed: %s", exc)
            return None

    def is_unlocked(self) -> bool:
        with self._lock:
            if self._kp is not None:
                if self._expired():
                    self._drop_session()
                else:
                    self._touch_deadline()
                    return True
        return bool(os.environ.get(self._ENV_KEY))

    def unlock(self, secret: str, progress: Optional[Callable[[str], None]] = None) -> bool:
        if not self.is_available():
            return False
        with self._lock:
            if self._kp is not None and not self._expired():
                self._touch_deadline()
                return True
            try:
                self._report(progress, "unlocking")
                kp = self._open(password=secret)
            except CredentialsError:
                logger.error("KDBX unlock failed: wrong master password or key file")
                return False
            except Exception as exc:
                logger.error("KDBX unlock error: %s", exc)
                return False
            self._kp = kp
            try:
                os.environ[self._ENV_KEY] = base64.b64encode(
                    kp.transformed_key).decode("ascii")
            except Exception as exc:
                logger.debug("KDBX transformed_key export failed: %s", exc)
            self._touch_deadline()
            self._report(progress, "loading")
            self._warm_cache(kp)
            return True

    def _warm_cache(self, kp) -> None:
        cache: Dict[str, Optional[str]] = {}
        try:
            for entry in (kp.entries or []):
                if entry.title:
                    cache[entry.title] = entry.password
        except Exception as exc:
            logger.debug("KDBX cache warm failed: %s", exc)
        self._cache = cache

    def lock(self) -> None:
        with self._lock:
            self._drop_session()

    @staticmethod
    def create_database(path: str, password: str, keyfile: Optional[str] = None) -> bool:
        """Create a brand-new ``.kdbx`` at ``path`` (pykeepass' default modern KDBX 4 + Argon2)
        protected by ``password`` (+ optional existing ``keyfile``). Returns True on success.
        Creates the file only — the caller unlocks it through the normal path afterwards."""
        # Probe pykeepass only if the create helper hasn't been resolved/injected yet,
        # so a probe never clobbers a test-injected _kdbx_create_database.
        if _kdbx_create_database is _UNSET:
            _get_pykeepass()
        if not callable(_kdbx_create_database):
            logger.error("Cannot create KeePass database: pykeepass is not installed")
            return False
        try:
            _kdbx_create_database(
                os.path.expanduser(path),
                password=(password or None),
                keyfile=(os.path.expanduser(keyfile) if keyfile else None))
            return True
        except Exception as exc:
            logger.error("KDBX create_database failed: %s", exc)
            return False

    # -- group / entry helpers ----
    def _group(self, kp):
        grp = kp.find_groups(name=self._GROUP, first=True)
        if grp is None:
            grp = kp.add_group(kp.root_group, self._GROUP)
        return grp

    @staticmethod
    def _find(kp, title: str):
        return kp.find_entries(title=title, first=True)

    # -- operations ----
    def lookup(self, spec: SecretSpec) -> Optional[str]:
        account = spec.keyring_account
        with self._lock:
            if self._kp is not None and self._cache is not None and not self._expired():
                self._touch_deadline()
                if account in self._cache:
                    return self._cache[account] or None
        kp = self._db()
        if kp is None:
            return None
        entry = self._find(kp, account)
        return (entry.password if entry else None) or None

    def store(self, spec: SecretSpec, secret: str) -> bool:
        # PyKeePass mutates one in-memory database object and rewrites the whole file.
        # Serialize the complete transaction now that UI callers may save off-thread.
        with self._lock:
            return self._store_locked(spec, secret)

    def _store_locked(self, spec: SecretSpec, secret: str) -> bool:
        kp = self._db()
        if kp is None:
            return False
        try:
            account = spec.keyring_account
            attrs = spec.attributes or {}
            entry = self._find(kp, account)
            if entry is None:
                entry = kp.add_entry(self._group(kp), account,
                                     attrs.get("username") or "", secret)
            else:
                entry.password = secret
                if attrs.get("username"):
                    entry.username = attrs.get("username")
            host = attrs.get("host")
            if host and attrs.get("username"):
                entry.url = f"ssh://{attrs.get('username')}@{host}"
            setter = getattr(entry, "set_custom_property", None)
            if callable(setter):
                try:
                    setter(self._TYPE_PROP, attrs.get("type") or "")
                except Exception:
                    pass
            kp.save()
            with self._lock:
                if self._cache is not None:
                    self._cache[account] = secret
            return True
        except Exception as exc:
            logger.error("KDBX store failed: %s", exc)
            return False

    def delete(self, spec: SecretSpec) -> bool:
        with self._lock:
            return self._delete_locked(spec)

    def _delete_locked(self, spec: SecretSpec) -> bool:
        kp = self._db()
        if kp is None:
            return False
        try:
            entry = self._find(kp, spec.keyring_account)
            if entry is None:
                return False
            kp.delete_entry(entry)
            kp.save()
            with self._lock:
                if self._cache is not None:
                    self._cache.pop(spec.keyring_account, None)
            return True
        except Exception as exc:
            logger.error("KDBX delete failed: %s", exc)
            return False

    def iter_credentials(self) -> List[Tuple[Dict[str, str], Optional[str]]]:
        """Enumerate entries in the dedicated ``sshPilot`` group as ``(attributes, secret)``.

        Empty when locked. Type comes from the ``sshpilot_type`` custom property when set,
        else from :func:`parse_account` on the entry title (``login.username`` is the hint
        for email-style SSH usernames)."""
        kp = self._db()
        if kp is None:
            return []
        out: List[Tuple[Dict[str, str], Optional[str]]] = []
        try:
            grp = kp.find_groups(name=self._GROUP, first=True)
            if grp is None:
                return []
            entries = kp.find_entries(group=grp) or []
            for entry in entries:
                title = entry.title or ""
                if not title:
                    continue
                cred_type = ""
                getter = getattr(entry, "get_custom_property", None)
                if callable(getter):
                    try:
                        cred_type = getter(self._TYPE_PROP) or ""
                    except Exception:
                        cred_type = ""
                username_hint = entry.username or None
                if cred_type:
                    attrs: Dict[str, str] = {"type": cred_type}
                    if cred_type == "key_passphrase":
                        attrs["key_path"] = title
                    elif cred_type == "vault_master":
                        attrs["key_path"] = title
                    else:
                        parsed = parse_account(title, username_hint=username_hint)
                        attrs["host"] = parsed.get("host", "")
                        attrs["username"] = parsed.get("username", "")
                else:
                    attrs = parse_account(title, username_hint=username_hint)
                    if not attrs:
                        continue
                value = entry.password or None
                if not value:
                    continue
                out.append((attrs, value))
        except Exception as exc:
            logger.debug("KDBX iter_credentials failed: %s", exc)
        return out


class SSHAgentBackend(SecretBackend):
    """The "don't store secrets at all" choice.

    A null backend: it persists nothing and returns nothing. Because every explicit
    selection is exclusive, selecting it makes the manager consult only it, so no secret
    lands in or is read from libsecret/keyring. The user relies on ssh-agent (keys loaded
    by the existing preload path or externally) and on ssh's own prompts. ``store``
    returns ``True`` (no-op success) so saving a secret reports success without writing.
    """

    name = "agent"

    def describe(self) -> str:
        return "agent" if os.environ.get("SSH_AUTH_SOCK") else "agent (no SSH_AUTH_SOCK)"

    def is_available(self) -> bool:
        return True

    def store(self, spec: SecretSpec, secret: str) -> bool:
        return True  # no-op success: nothing is stored

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        return None

    def delete(self, spec: SecretSpec) -> bool:
        return False  # nothing of ours to delete


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SecretManager:
    """Selects and orchestrates secret backends.

    **``auto``** (platform default: libsecret then keyring on Linux, keyring on macOS):

    ``store``, ``lookup`` and ``delete`` all act on the platform default local stores only
    (first available: libsecret then keyring). The CLI/session backends (rbw, Bitwarden,
    pass, keepassxc) are used only when explicitly selected — so ``auto`` stays fast and
    never spawns a vault CLI. Cross-backend reads (migration/export) use ``lookup_everywhere``.

    **Explicit selection** (any named backend, including ``agent`` and session vaults):

    - ``store`` / ``lookup`` / ``delete`` consult **only** that backend — no fallback on
      store failure, no read-through to a stale copy elsewhere, and no cross-backend
      delete. A locked session vault returns nothing/``False`` until unlocked.
    - If the selected backend is **unavailable**, operations resolve to nothing/``False``
      and a warning is logged (deduped).

    :meth:`lookup_everywhere` deliberately ignores explicit selection and scans all
    available backends (for export/migration); locked session vaults contribute nothing.
    """

    def __init__(self) -> None:
        self._backends: Dict[str, SecretBackend] = {
            "libsecret": LibSecretBackend(),
            "keyring": KeyringBackend(),
            "pass": PassBackend(),
            "bitwarden": BitwardenBackend(),
            "rbw": RbwBackend(),
            "keepassxc": KdbxBackend(),
            "agent": SSHAgentBackend(),
        }
        self._selected: Optional[str] = None  # resolved lazily
        self._warned_unavailable: Optional[str] = None  # dedup for the warning below

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

    # -- platform keyring (selection-independent) -------------------------
    # Used for the session vault's master password, which must be stored in the OS
    # keyring regardless of the selected backend (it cannot live in the vault it
    # unlocks, nor in `pass`/`agent`).
    def _platform_keyring_backends(self) -> List[SecretBackend]:
        out: List[SecretBackend] = []
        for name in self._platform_default_order():   # libsecret then keyring (Linux)
            backend = self._backends.get(name)
            if backend is not None and backend.is_available():
                out.append(backend)
        return out

    def store_in_keyring(self, spec: SecretSpec, secret: str) -> bool:
        """Store ``secret`` in the platform keyring only (ignores the selection)."""
        for backend in self._platform_keyring_backends():
            if backend.store(spec, secret):
                return True
        logger.warning("No platform keyring available; %s not saved", spec.label)
        return False

    def lookup_in_keyring(self, spec: SecretSpec) -> Optional[str]:
        """Read ``spec`` from the platform keyring only (ignores the selection)."""
        for backend in self._platform_keyring_backends():
            value = backend.lookup(spec)
            if value:
                return value
        return None

    def delete_in_keyring(self, spec: SecretSpec) -> bool:
        """Clear ``spec`` from every platform keyring backend (ignores the selection)."""
        removed = False
        for backend in self._platform_keyring_backends():
            if backend.delete(spec):
                removed = True
        return removed

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

    def all_available_backends(self) -> List[SecretBackend]:
        """Public view of :meth:`_all_available_backends` for export/migration callers."""
        return self._all_available_backends()

    def available_backends(self, *, cheap: bool = False) -> List[str]:
        """Names of all registered backends that are currently usable.

        ``cheap=True`` uses a backend's ``is_discoverable()`` when it provides one
        (Bitwarden), avoiding a blocking ``bw --version`` spawn — for UI labels that
        run on the GTK main thread and only need presence, not a live verification."""
        out: List[str] = []
        for name, backend in self._backends.items():
            probe = getattr(backend, "is_discoverable", None) if cheap else None
            try:
                ok = probe() if callable(probe) else backend.is_available()
            except Exception:
                ok = False
            if ok:
                out.append(name)
        return out

    def registered_backends(self) -> List[str]:
        """Names of every registered backend, available or not (for UI choices)."""
        return list(self._backends.keys())

    def is_session_backed(self, name: str) -> bool:
        """Whether the named backend has a lock lifecycle (Bitwarden/Vaultwarden)."""
        backend = self._backends.get((name or "").strip().lower())
        return bool(backend is not None and getattr(backend, "session_backed", False))

    def get_backend(self, name: str) -> Optional[SecretBackend]:
        """The registered backend object by name (e.g. ``"bitwarden"``), independent of the
        Preferences selection — used to drive a backend as a backup destination."""
        return self._backends.get((name or "").strip().lower())

    def persists_secrets(self) -> bool:
        """Whether :meth:`store` would persist secrets (``False`` for explicit ``agent``)."""
        backend = self.selected_backend()
        return backend is None or backend.name != "agent"

    # -- selection helpers ------------------------------------------------
    def selected_backend(self) -> Optional[SecretBackend]:
        """The explicitly-selected backend object (None for ``auto``/unknown)."""
        name = self._selected_name()
        if name in ("", "auto"):
            return None
        return self._backends.get(name)

    def _exclusive_backend(self) -> Optional[SecretBackend]:
        """The explicitly-selected backend (``None`` for ``auto``).

        When set, ``store``/``lookup``/``delete`` consult only it — no
        fallback/fallthrough to other stores. If it is currently **unavailable**
        (e.g. ``bitwarden`` with no ``bw`` on PATH), operations will resolve to
        nothing — so we log a warning here (deduped) to make that visible rather than
        a silent no-op. Reset once it becomes available again."""
        backend = self.selected_backend()
        if backend is not None:
            try:
                available = backend.is_available()
            except Exception:
                available = False
            if not available:
                if self._warned_unavailable != backend.name:
                    self._warned_unavailable = backend.name
                    logger.warning(
                        "Selected secret backend %r is unavailable — passwords and key "
                        "passphrases will not be stored or autofilled until it is "
                        "available or you change the backend "
                        "(Preferences ▸ Security & Credentials).", backend.name)
            elif self._warned_unavailable == backend.name:
                self._warned_unavailable = None
        return backend

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

    def selected_needs_login(self) -> bool:
        """True when the selected session backend has no authenticated account, so
        the GTK layer should tell the user to run ``bw login`` before unlocking."""
        backend = self.selected_backend()
        probe = getattr(backend, "needs_login", None)
        if backend is None or not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def unlock_selected(self, secret: str, progress=None) -> bool:
        """Unlock the selected session-backed backend (no-op otherwise).

        ``progress(stage: str)`` is forwarded to the backend for staged UI reporting
        (e.g. a startup spinner); passing nothing keeps the simple two-arg call so
        backends without a ``progress`` parameter still work."""
        backend = self.selected_backend()
        if backend is None or not getattr(backend, "session_backed", False):
            return True
        if progress is None:
            return backend.unlock(secret)
        return backend.unlock(secret, progress=progress)

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

    def _store_backends(self) -> List[SecretBackend]:
        """Backends ``store`` may use.

        Explicit selection → only that backend (locked session vaults return False
        until unlocked). ``auto`` → platform order with fallback on failure.
        """
        exclusive = self._exclusive_backend()
        if exclusive is not None:
            return [exclusive]
        return self._ordered_backends()

    # -- operations ------------------------------------------------------
    def store(self, spec: SecretSpec, secret: str) -> bool:
        backends = self._store_backends()
        for backend in backends:
            if backend.store(spec, secret):
                logger.debug("secret stored via %s", backend.name)
                return True
        tried = ", ".join(b.name for b in backends) or "none"
        logger.warning("Secret not stored — no backend accepted it (tried: %s)", tried)
        return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        # ``auto`` == use the platform default local stores only (libsecret then keyring
        # on Linux); an explicit selection uses that backend only. Cross-backend reads for
        # migration/export go through :meth:`lookup_everywhere`.
        exclusive = self._exclusive_backend()
        backends = [exclusive] if exclusive is not None else self._ordered_backends()
        for backend in backends:
            value = backend.lookup(spec)
            if value:
                return value
        return None

    def lookup_everywhere(self, spec: SecretSpec) -> Optional[Tuple[str, str]]:
        """Find ``spec`` across **every available backend**, returning ``(value, backend
        name)`` for the first hit, or ``None``.

        Unlike :meth:`lookup`, this deliberately **ignores the exclusive/selected backend**
        — it scans all available stores so callers (the credential manager / export) can see
        secrets wherever they live, even under a backend the user has since switched away
        from. A locked session vault simply contributes nothing (its ``lookup`` returns
        ``None``); this never prompts or forces an unlock.
        """
        for backend in self._all_available_backends():
            try:
                value = backend.lookup(spec)
            except Exception:
                value = None
            if value:
                return value, backend.name
        return None

    def delete(self, spec: SecretSpec) -> bool:
        # Same scope as store/lookup: ``auto`` acts on the platform default local stores
        # only; an explicit selection acts on that backend only. (Bulk cross-backend
        # cleanup for migration lives in the credential manager, not here.)
        exclusive = self._exclusive_backend()
        backends = [exclusive] if exclusive is not None else self._ordered_backends()
        removed = False
        for backend in backends:
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
