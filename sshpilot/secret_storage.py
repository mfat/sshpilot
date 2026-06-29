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
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

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
    profile = os.environ.get("BITWARDENCLI_APPDATA_DIR", "")
    return master_password_spec(name, profile)


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
    cheap on the hot path. The ``bw`` CLI holds one account/server per **data directory**;
    the user selects an account by pointing ``BITWARDENCLI_APPDATA_DIR`` at that data dir
    (``secrets.bitwarden.profile``). This is also how a self-hosted **Vaultwarden** account
    is used — a profile whose data dir is configured (``bw config server`` + ``bw login``)
    for the self-hosted server. GTK-free (imported by the askpass subprocess); UI lives in
    the GTK layer.
    """

    name = "bitwarden"
    session_backed = True
    _TIMEOUT = 120  # seconds — generous enough for a master-password unlock

    def __init__(self, bin_name: str = "bw") -> None:
        self._bin_name = bin_name
        self._bin_override: Optional[str] = None
        self._token: Optional[str] = None                # session token (this process)
        self._unlocked = False                           # we unlocked it this session
        self._deadline: Optional[float] = None           # monotonic; None = no timeout
        self._items: Optional[Dict[str, dict]] = None    # name→item; None = not loaded
        self._cache_complete = False                     # _items holds the whole vault
        self._lock = threading.RLock()

    @property
    def _bin(self) -> Optional[str]:
        """Resolve ``bw`` on each access so a CLI installed after launch is found.
        Assigning ``self._bin`` pins an explicit path (used by tests)."""
        if self._bin_override is not None:
            return self._bin_override
        return shutil.which(self._bin_name) or None

    @_bin.setter
    def _bin(self, value: Optional[str]) -> None:
        self._bin_override = value

    def describe(self) -> str:
        return self.name

    def is_available(self) -> bool:
        # Cheap: just whether the CLI exists. Lock/unlock state is a separate question
        # answered by is_unlocked()/needs_login(). Which server/account the CLI talks to
        # (Bitwarden cloud, a self-hosted Vaultwarden, a specific profile) is configured
        # by the user via the `bw` CLI + the optional BITWARDENCLI_APPDATA_DIR profile.
        return bool(self._bin)

    # -- bw subprocess helper --------------------------------------------
    def _run(self, args: List[str], *, token: Optional[str] = None,
             input_bytes: Optional[bytes] = None):
        """Run ``bw <args>`` non-interactively, returning the CompletedProcess.
        ``token`` (if given) is passed as ``BW_SESSION``. ``--nointeraction`` guarantees
        ``bw`` never blocks on stdin (a GUI must never hang on terminal input)."""
        env = os.environ.copy()
        if token:
            env["BW_SESSION"] = token
        started = time.monotonic()
        result = subprocess.run(
            [self._bin, "--nointeraction"] + list(args),
            input=input_bytes, capture_output=True, env=env, check=False,
            timeout=self._TIMEOUT,
        )
        logger.debug("bw %s: %.2fs (rc=%s)",
                     " ".join(args[:2]), time.monotonic() - started, result.returncode)
        return result

    # -- status / unlock state -------------------------------------------
    def _status(self) -> Optional[str]:
        """``bw status`` state ('unauthenticated'|'locked'|'unlocked') or None. Needs no
        session token."""
        if not self._bin:
            return None
        try:
            result = self._run(["status"])
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
            status = data.get("status")
            return status if isinstance(status, str) else None
        except Exception as exc:
            logger.debug("bw status failed: %s", exc)
            return None

    def needs_login(self) -> bool:
        """True when no account is authenticated (``bw login`` required), so ``bw unlock``
        cannot succeed. Only called in the unlock-decision flow."""
        return self._status() == "unauthenticated"

    # -- idle timeout ----------------------------------------------------
    @staticmethod
    def _session_timeout_seconds() -> int:
        """Idle seconds before the session is dropped (0 = never). Driven by the
        ``SSHPILOT_SECRET_SESSION_TIMEOUT`` env var set from ``secrets.session_timeout``."""
        try:
            return int(os.environ.get("SSHPILOT_SECRET_SESSION_TIMEOUT", "0") or 0)
        except Exception:
            return 0

    def _touch_deadline(self) -> None:
        """Refresh the idle deadline (sliding window). Caller holds ``self._lock``."""
        secs = self._session_timeout_seconds()
        self._deadline = (time.monotonic() + secs) if secs > 0 else None

    def _expired(self) -> bool:
        """Caller holds ``self._lock``."""
        return self._deadline is not None and time.monotonic() > self._deadline

    def _drop_session(self) -> None:
        """Forget the in-process session + cache (re-lock). Caller holds ``self._lock``."""
        self._token = None
        self._unlocked = False
        self._deadline = None
        self._items = None
        self._cache_complete = False
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
        if not self._bin:
            return False
        with self._lock:
            try:
                self._report(progress, "starting")
                # We do NOT run `bw config server` here: `bw` rejects a server change while
                # logged in (and you must be logged in for `unlock` to succeed), so it only
                # ever burned a ~1-2s spawn. Vaultwarden's server is configured by the user
                # via the CLI (`bw config server <url>` then `bw login`); the app's URL
                # setting just gates availability and labels the backend.
                env = os.environ.copy()
                env["BW_MASTER"] = secret or ""
                self._report(progress, "unlocking")
                started = time.monotonic()
                result = subprocess.run(
                    [self._bin, "--nointeraction", "unlock",
                     "--passwordenv", "BW_MASTER", "--raw"],
                    capture_output=True, env=env, check=False, timeout=self._TIMEOUT,
                )
                logger.debug("bw unlock: %.2fs (rc=%s)",
                             time.monotonic() - started, result.returncode)
                if result.returncode != 0:
                    logger.error("bw unlock failed: %s",
                                 result.stderr.decode("utf-8", "replace").strip())
                    return False
                token = result.stdout.decode("utf-8", "replace").strip()
                if not token:
                    return False
                self._token = token
                self._unlocked = True
                os.environ["BW_SESSION"] = token
                self._touch_deadline()
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

    def delete(self, spec: SecretSpec) -> bool:
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

    def available_backends(self) -> List[str]:
        """Names of all registered backends that are currently usable."""
        return [n for n, b in self._backends.items() if b.is_available()]

    def registered_backends(self) -> List[str]:
        """Names of every registered backend, available or not (for UI choices)."""
        return list(self._backends.keys())

    def is_session_backed(self, name: str) -> bool:
        """Whether the named backend has a lock lifecycle (Bitwarden/Vaultwarden)."""
        backend = self._backends.get((name or "").strip().lower())
        return bool(backend is not None and getattr(backend, "session_backed", False))

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
        exclusive = self._exclusive_backend()
        backends = [exclusive] if exclusive is not None else self._all_available_backends()
        for backend in backends:
            value = backend.lookup(spec)
            if value:
                return value
        return None

    def delete(self, spec: SecretSpec) -> bool:
        exclusive = self._exclusive_backend()
        backends = [exclusive] if exclusive is not None else self._all_available_backends()
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
