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
- ``bitwarden`` / ``vaultwarden`` — a persistent ``bw serve`` daemon + loopback
  HTTP (Vaultwarden = self-hosted server URL). *Session-backed*: must be unlocked
  before secrets are readable. The daemon holds the session and listens on
  ``127.0.0.1`` (port via ``SSHPILOT_BW_SERVE_PORT``), so the ``--askpass``
  subprocess reads from the same daemon without cold-loading the vault.
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

import atexit
import http.client
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

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
    """Bitwarden backend using a persistent ``bw serve`` daemon + loopback HTTP.

    ``bw`` cold-starts a Node runtime (~1–2s) per invocation, so instead of spawning it
    per operation we start one ``bw serve`` daemon and make fast REST calls to its
    local API. The daemon holds the unlocked vault in memory, so reads/writes are quick;
    because it listens on ``127.0.0.1`` the ``--askpass`` subprocess can hit the *same*
    daemon (port via ``SSHPILOT_BW_SERVE_PORT``) without cold-loading the vault.

    The daemon owns the session (we don't track ``BW_SESSION``). We keep an in-memory
    name→item cache, warmed once on unlock, so repeated lookups in this process need no
    HTTP. GTK-free (imported by the askpass subprocess); UI lives in the GTK layer.

    Security: the API is reachable by any same-user process while the vault is unlocked
    (TCP loopback). A dedicated default port is used so we never clobber a ``bw serve``
    the user runs for their own tooling.
    """

    name = "bitwarden"
    session_backed = True
    _HOST = "127.0.0.1"
    _DEFAULT_PORT = 8765        # dedicated; NOT bw's default 8087
    _START_TIMEOUT = 8.0        # seconds to wait for `bw serve` to become ready
    _CONFIG_TIMEOUT = 30        # subprocess timeout for one-shot `bw config server`
    _STATUS_TTL = 1.0           # seconds to cache GET /status

    def __init__(self, bin_name: str = "bw") -> None:
        self._bin_name = bin_name
        self._bin_override: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None   # the daemon we own, if any
        self._unlocked = False                           # we unlocked it this session
        self._items: Optional[Dict[str, dict]] = None    # name→item; None = not loaded
        self._status_cache: tuple = (0.0, None)          # (monotonic_ts, status|None)
        self._lock = threading.RLock()
        atexit.register(self._terminate_proc)            # don't leak an unlocked daemon

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

    # -- server (Vaultwarden overrides) ----------------------------------
    def _server_url(self) -> str:
        """Self-hosted server URL; empty = Bitwarden cloud."""
        return ""

    def describe(self) -> str:
        url = self._server_url()
        return f"{self.name}:{url}" if url else self.name

    def is_available(self) -> bool:
        # Cheap, daemon-free: just whether the CLI exists. Lock/unlock state is a
        # separate question answered by is_unlocked()/needs_login().
        return bool(self._bin)

    # -- loopback HTTP to the serve daemon -------------------------------
    def _port(self) -> int:
        try:
            return int(os.environ.get("SSHPILOT_BW_SERVE_PORT") or self._DEFAULT_PORT)
        except Exception:
            return self._DEFAULT_PORT

    def _http(self, method: str, path: str, *, params=None, body=None, timeout: float = 10):
        """One request to the serve API. Returns ``(status_code, data_dict)``; raises on
        a transport error (connection refused => daemon down)."""
        if params:
            query = urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                path = f"{path}?{query}"
        conn = http.client.HTTPConnection(self._HOST, self._port(), timeout=timeout)
        try:
            payload = None
            headers = {}
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:
                data = {}
            return resp.status, (data if isinstance(data, dict) else {})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _ok(code: int, data: dict) -> bool:
        return code == 200 and bool(data.get("success"))

    def _serve_reachable(self) -> bool:
        try:
            code, _ = self._http("GET", "/status", timeout=1.5)
            return code == 200
        except Exception:
            return False

    # -- daemon lifecycle ------------------------------------------------
    def _ensure_serve(self) -> bool:
        """Ensure a serve daemon is reachable; return True on success. Only called from
        the unlock flow (never from the askpass subprocess, which reuses the main app's
        daemon for reads)."""
        if not self._bin:
            return False
        with self._lock:
            if self._serve_reachable():
                if self._proc is None:
                    # A daemon we didn't start (leftover from a crash / previous run):
                    # lock it so an unlocked session can't be inherited across launches.
                    try:
                        self._http("POST", "/lock", timeout=3)
                    except Exception:
                        pass
                os.environ["SSHPILOT_BW_SERVE_PORT"] = str(self._port())
                return True
            url = self._server_url()  # Vaultwarden: point the CLI at the server first
            if url:
                try:
                    subprocess.run([self._bin, "config", "server", url],
                                   capture_output=True, check=False,
                                   timeout=self._CONFIG_TIMEOUT)
                except Exception as exc:
                    logger.debug("bw config server failed: %s", exc)
            try:
                self._proc = subprocess.Popen(
                    [self._bin, "serve", "--port", str(self._port())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                logger.error("failed to start bw serve: %s", exc)
                self._proc = None
                return False
            deadline = time.monotonic() + self._START_TIMEOUT
            while time.monotonic() < deadline:
                if self._serve_reachable():
                    os.environ["SSHPILOT_BW_SERVE_PORT"] = str(self._port())
                    logger.debug("bw serve ready on %s:%s", self._HOST, self._port())
                    return True
                if self._proc.poll() is not None:
                    logger.error("bw serve exited (rc=%s) during startup",
                                 self._proc.returncode)
                    self._proc = None
                    return False
                time.sleep(0.1)
            logger.error("bw serve did not become ready within %ss", self._START_TIMEOUT)
            self._terminate_proc()
            return False

    def _terminate_proc(self) -> None:
        proc = self._proc
        self._proc = None
        self._unlocked = False
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        except Exception:
            pass

    # -- status / unlock state -------------------------------------------
    @staticmethod
    def _extract_status(data: dict) -> Optional[str]:
        """Pull the vault status out of a ``/status`` reply, tolerating shape variants
        ({"data": {"status": ...}} or a nested template)."""
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        if isinstance(d, dict):
            if isinstance(d.get("status"), str):
                return d["status"]
            tmpl = d.get("template")
            if isinstance(tmpl, dict) and isinstance(tmpl.get("status"), str):
                return tmpl["status"]
        return None

    def _status(self) -> Optional[str]:
        """Vault status via the daemon ('unauthenticated'|'locked'|'unlocked') or None.
        Does NOT start the daemon — only queries one if reachable (TTL-cached)."""
        now = time.monotonic()
        ts, cached = self._status_cache
        if cached is not None and (now - ts) < self._STATUS_TTL:
            return cached
        status = None
        try:
            code, data = self._http("GET", "/status", timeout=2)
            if code == 200:
                status = self._extract_status(data)
                if status is None:
                    logger.debug("bw serve /status unparsed: %s", data)
            else:
                logger.debug("bw serve /status HTTP %s: %s", code, data)
        except Exception as exc:
            logger.debug("bw serve /status error: %s", exc)
        self._status_cache = (now, status)
        return status

    def needs_login(self) -> bool:
        """True when no account is authenticated (``bw login`` required). Starts the
        daemon if needed — only called in the unlock-decision flow."""
        if not self._bin:
            return False
        if not self._serve_reachable() and not self._ensure_serve():
            return False
        return self._status() == "unauthenticated"

    def _daemon_alive(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:
            return True   # can't probe (e.g. a test sentinel) → assume alive

    def is_unlocked(self) -> bool:
        # Trust our own successful unlock while the daemon we started is still running,
        # so we don't re-prompt on every connection if a /status probe is flaky. Falls
        # back to asking the daemon (subprocess / external daemon case); never starts one.
        if self._unlocked and self._daemon_alive():
            return True
        return self._status() == "unlocked"

    # -- unlock / lock ---------------------------------------------------
    def unlock(self, secret: str, progress: Optional[Callable[[str], None]] = None) -> bool:
        if not self._bin:
            return False
        with self._lock:
            if not self._ensure_serve():
                return False
            self._report(progress, "unlocking")
            self._status_cache = (0.0, None)
            try:
                code, data = self._http("POST", "/unlock", body={"password": secret or ""},
                                        timeout=self._CONFIG_TIMEOUT)
            except Exception as exc:
                logger.error("bw serve unlock error: %s", exc)
                return False
            if not self._ok(code, data):
                logger.error("bw serve unlock failed: %s",
                             data.get("message") or f"HTTP {code}")
                return False
            self._unlocked = True
            self._status_cache = (0.0, None)
            # Do NOT prefetch the whole vault here — for a large vault, serializing every
            # item over HTTP is the dominant cost felt at connect time. Start with an
            # empty per-item cache; lookups fetch (and cache) just the item they need via
            # a fast targeted search against the in-memory daemon. Refresh the on-disk
            # vault in the background.
            self._items = {}
            self._start_background_sync()
            return True

    @staticmethod
    def _report(progress, stage: str) -> None:
        if callable(progress):
            try:
                progress(stage)
            except Exception:
                pass

    def _start_background_sync(self) -> None:
        """Best-effort ``POST /sync`` off-thread so a multi-device change lands without
        blocking unlock/connect (the warm cache already serves this session)."""
        def _bg():
            try:
                self._http("POST", "/sync", timeout=60)
            except Exception as exc:
                logger.debug("bw serve sync failed: %s", exc)
        threading.Thread(target=_bg, daemon=True).start()

    def lock(self) -> None:
        with self._lock:
            try:
                if self._serve_reachable():
                    self._http("POST", "/lock", timeout=3)
            except Exception:
                pass
            self._items = None
            self._unlocked = False
            self._status_cache = (0.0, None)
            self._terminate_proc()                      # stop the daemon we own
            os.environ.pop("SSHPILOT_BW_SERVE_PORT", None)

    # -- item cache + operations -----------------------------------------
    def _search_item(self, account: str) -> Optional[dict]:
        """Targeted ``GET /list/object/items?search=`` for one item by exact name.
        The daemon filters its in-memory vault and returns only matches, so this is
        cheap even for a large vault (no whole-vault serialization). Returns the item
        dict or None."""
        try:
            code, data = self._http("GET", "/list/object/items",
                                    params={"search": account}, timeout=15)
            if code != 200:
                return None
            for item in ((data.get("data") or {}).get("data") or []):
                if item.get("name") == account:
                    return item
        except Exception as exc:
            logger.debug("bw serve search failed: %s", exc)
        return None

    def _find_item(self, account: str) -> Optional[dict]:
        """Return the item named ``account`` — from the per-item cache if present, else
        one targeted search (caching a hit). ``None`` if not found."""
        with self._lock:
            if self._items is not None and account in self._items:
                return self._items[account]
        item = self._search_item(account)
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

    def store(self, spec: SecretSpec, secret: str) -> bool:
        if not self._bin:
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
                code, data = self._http("PUT", f"/object/item/{item['id']}",
                                        body=item, timeout=15)
                if not self._ok(code, data):
                    return False
                updated = data.get("data") if isinstance(data.get("data"), dict) else item
                self._cache_put(account, updated)
                return True
            item = self._new_item(account, secret, spec)
            code, data = self._http("POST", "/object/item", body=item, timeout=15)
            if not self._ok(code, data):
                return False
            created = data.get("data") if isinstance(data.get("data"), dict) else item
            self._cache_put(account, created)
            return True
        except Exception as exc:
            logger.error("bw serve store failed: %s", exc)
            return False

    def lookup(self, spec: SecretSpec) -> Optional[str]:
        # Per-item cache hit (instant) or one targeted search (cheap via the daemon).
        item = self._find_item(spec.keyring_account)
        if not item:
            return None
        return (item.get("login") or {}).get("password") or None

    def delete(self, spec: SecretSpec) -> bool:
        if not self._bin:
            return False
        account = spec.keyring_account
        try:
            existing = self._find_item(account)
            item_id = existing.get("id") if existing else None
            if not item_id:
                return False
            code, data = self._http("DELETE", f"/object/item/{item_id}", timeout=10)
            if code not in (200, 204) or (code == 200 and not data.get("success", True)):
                return False
            self._cache_drop(account)
            return True
        except Exception as exc:
            logger.debug("bw serve delete failed: %s", exc)
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

    # -- pre-warm --------------------------------------------------------
    def prewarm(self) -> None:
        """Start the serve daemon in the background (no unlock), so its node startup
        overlaps the user typing the master password."""
        def _bg():
            try:
                self._ensure_serve()
            except Exception as exc:
                logger.debug("bw serve prewarm failed: %s", exc)
        threading.Thread(target=_bg, daemon=True).start()


class VaultwardenBackend(BitwardenBackend):
    """Vaultwarden = self-hosted Bitwarden. Same ``bw serve`` daemon, configured server.

    The URL is read from ``SSHPILOT_VAULTWARDEN_SERVER`` (propagated from the
    ``secrets.vaultwarden.server`` setting); without it the backend is not offered.
    ``_ensure_serve`` runs ``bw config server <url>`` before starting the daemon.
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

    def registered_backends(self) -> List[str]:
        """Names of every registered backend, available or not (for UI choices)."""
        return list(self._backends.keys())

    def is_session_backed(self, name: str) -> bool:
        """Whether the named backend has a lock lifecycle (Bitwarden/Vaultwarden)."""
        backend = self._backends.get((name or "").strip().lower())
        return bool(backend is not None and getattr(backend, "session_backed", False))

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

    def prewarm_selected(self) -> None:
        """Best-effort: start the selected session backend's daemon ahead of unlock so
        its startup overlaps the master-password prompt. No-op for other backends."""
        backend = self.selected_backend()
        prewarm = getattr(backend, "prewarm", None)
        if callable(prewarm):
            try:
                prewarm()
            except Exception:
                pass

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
        """Backends ``store`` may use, honoring the authoritative short-circuit and
        the "explicitly-selected session backend that isn't ready" rule.

        When the user explicitly selected a session-backed backend (Bitwarden/
        Vaultwarden) that is available but **not unlocked**, do NOT fall back to
        other stores — otherwise the secret would silently land in libsecret while
        the user believes it went to their chosen vault. Return only that backend
        (whose ``store`` returns False until unlocked), so the caller can surface it.
        """
        auth = self._authoritative_selected()
        if auth is not None:
            return [auth]
        backend = self.selected_backend()
        if (backend is not None and getattr(backend, "session_backed", False)
                and backend.is_available() and not backend.is_unlocked()):
            return [backend]
        return self._ordered_backends()

    # -- operations ------------------------------------------------------
    def store(self, spec: SecretSpec, secret: str) -> bool:
        for backend in self._store_backends():
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
