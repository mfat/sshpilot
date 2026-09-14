"""Fetch public SSH keys published by online identities.

This is the fetch half of ``ssh-import-id`` (Launchpad ``lp:``, GitHub
``gh:``, GitLab ``gl:``) plus plain HTTPS URLs such as
``https://github.com/<user>.keys``.  It only resolves and validates keys; the
caller decides where they are installed.

Differences from ``ssh-import-id``, all deliberate:

* only HTTPS is fetched, and redirects to anything but HTTPS are refused —
  whoever controls the response controls who can log in;
* every returned line must be a bare, well-formed public key: option-prefixed
  lines (``command=``, ``cert-authority`` …), certificates, and blobs whose
  embedded key type disagrees with the line are dropped, so a key source can
  never grant more than plain key access;
* the response size is bounded.

Each accepted key keeps the provider comment and gains the same trailing
``# ssh-import-id <source>`` label ``ssh-import-id`` writes, so keys imported
here stay recognisable (and removable with ``ssh-import-id -r``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import ssl
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Mapping, Optional, Tuple
from urllib.parse import quote, urlsplit

from sshpilot import __version__ as sshpilot_version
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.identity import (
    PUBLIC_KEY_FETCH_FAILED,
    PUBLIC_KEY_SOURCE_EMPTY,
    PUBLIC_KEY_SOURCE_INVALID,
    PUBLIC_KEY_SOURCE_NOT_FOUND,
    PUBLIC_KEY_SOURCE_RATE_LIMITED,
    ImportedPublicKey,
    ImportedPublicKeyList,
)

DEFAULT_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SOURCE_LENGTH = 2048

# ssh-import-id's default protocol when a user id has no prefix.
DEFAULT_PROVIDER = "lp"
PROVIDERS = ("gh", "gl", "lp")
URL_PROVIDER = "url"

IMPORT_LABEL_PREFIX = "# ssh-import-id "

# Plain public key types accepted from a key source.  Certificates are not
# authorized_keys material and are rejected.
ACCEPTED_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ssh-dss",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        "sk-ssh-ed25519@openssh.com",
    }
)


@dataclass(frozen=True)
class PublicKeySource:
    """A parsed key source: ``provider`` is ``gh``/``gl``/``lp`` or ``url``."""

    provider: str
    username: str
    url: str

    @property
    def label(self) -> str:
        if self.provider == URL_PROVIDER:
            return self.url
        return f"{self.provider}:{self.username}"


class PublicKeyHttpError(Exception):
    """An HTTP response with a non-success status."""

    def __init__(self, status: int, headers: Optional[Mapping[str, str]] = None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}


# ``fetch(url, headers, timeout, *, follow_redirects) -> body``; raises
# PublicKeyHttpError for HTTP error statuses (and for a redirect it may not
# follow) and OSError (URLError included) for transport failures.
Fetcher = Callable[..., bytes]


def _invalid(message: str) -> SshPilotError:
    return SshPilotError(
        ErrorCode.VALIDATION_FAILED,
        message,
        details={"code": PUBLIC_KEY_SOURCE_INVALID},
    )


def parse_public_key_source(text: str) -> PublicKeySource:
    """Parse ``gh:user``, ``gl:user``, ``lp:user``, ``user`` or an HTTPS URL."""
    if type(text) is not str:
        raise TypeError("public key source must be a string")
    source = text.strip()
    if not source:
        raise _invalid("Enter a user ID or an HTTPS URL")
    if len(source) > MAX_SOURCE_LENGTH or any(
        ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in source
    ):
        raise _invalid("The key source must be a single user ID or URL")

    scheme = source.split(":", 1)[0].lower()
    if scheme in ("https", "http"):
        if scheme != "https":
            raise _invalid("Only HTTPS URLs are supported")
        parts = urlsplit(source)
        if not parts.hostname or "@" in parts.netloc:
            raise _invalid("The URL must name a host and carry no credentials")
        return PublicKeySource(URL_PROVIDER, "", source)

    if ":" in source:
        provider, username = source.split(":", 1)
    else:
        provider, username = DEFAULT_PROVIDER, source
    if provider not in PROVIDERS:
        raise _invalid(f"Unknown key source prefix {provider!r}")
    if not username or ":" in username:
        raise _invalid("The user ID is missing or malformed")
    quoted = quote(username, safe="")
    if provider == "gh":
        url = f"https://api.github.com/users/{quoted}/keys"
    elif provider == "gl":
        url = f"https://gitlab.com/{quoted}.keys"
    else:
        url = f"https://launchpad.net/~{quoted}/+sshkeys"
    return PublicKeySource(provider, username, url)


def _read_string(blob: bytes, offset: int) -> Tuple[bytes, int]:
    if offset + 4 > len(blob):
        raise ValueError("truncated key blob")
    (length,) = struct.unpack(">I", blob[offset : offset + 4])
    offset += 4
    if offset + length > len(blob):
        raise ValueError("truncated key blob")
    return blob[offset : offset + length], offset + length


def fingerprint_sha256(key_blob: bytes) -> str:
    """``ssh-keygen -lf`` style ``SHA256:`` fingerprint of a raw key blob."""
    digest = hashlib.sha256(key_blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def parse_public_key_line(line: str) -> Optional[Tuple[str, str, str, str]]:
    """Return ``(key_type, key_b64, comment, fingerprint)`` or None.

    Only a bare ``type base64 [comment]`` line is accepted; see the module
    docstring for what is rejected and why.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F for ch in stripped):
        return None
    fields = stripped.split()
    if len(fields) < 2 or fields[0] not in ACCEPTED_KEY_TYPES:
        return None
    key_type, key_b64 = fields[0], fields[1]
    try:
        blob = base64.b64decode(key_b64.encode("ascii"), validate=True)
        embedded_type, _offset = _read_string(blob, 0)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return None
    if embedded_type != key_type.encode("ascii"):
        return None
    comment = " ".join(fields[2:])
    return key_type, key_b64, comment, fingerprint_sha256(blob)


def _user_agent() -> str:
    return f"sshPilot/{sshpilot_version} (public-key-import)"


def _ssl_context() -> ssl.SSLContext:
    # certifi first so relocated bundles (PyInstaller) without a system CA
    # store still verify certificates; the stdlib default is right elsewhere.
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class _RedirectPolicy(urllib.request.HTTPRedirectHandler):
    """Follow HTTPS-only redirects, or surface the redirect as a status."""

    def __init__(self, follow: bool) -> None:
        super().__init__()
        self._follow = follow

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._follow:
            raise PublicKeyHttpError(code, dict(headers or {}))
        if urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.URLError("refused a redirect to a non-HTTPS URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urllib_fetch(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    *,
    follow_redirects: bool = True,
) -> bytes:
    """Production :data:`Fetcher` over urllib with verified TLS."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_context()),
        _RedirectPolicy(follow_redirects),
    )
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise PublicKeyHttpError(exc.code, dict(exc.headers or {})) from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise OSError("the key source response is too large")
    return body


def _http_failure(source: PublicKeySource, error: PublicKeyHttpError) -> SshPilotError:
    if error.status == 404:
        return SshPilotError(
            ErrorCode.KEY_NOT_FOUND,
            f"No public keys were found at {source.label}",
            details={"code": PUBLIC_KEY_SOURCE_NOT_FOUND, "provider": source.provider},
        )
    if source.provider == "gl" and 300 <= error.status < 400:
        # GitLab answers an unknown user's ``.keys`` with a redirect to its
        # sign-in page rather than a 404.
        return SshPilotError(
            ErrorCode.KEY_NOT_FOUND,
            f"No public keys were found at {source.label}",
            details={"code": PUBLIC_KEY_SOURCE_NOT_FOUND, "provider": source.provider},
        )
    if source.provider == "gh" and error.headers.get("x-ratelimit-remaining") == "0":
        return SshPilotError(
            ErrorCode.KEY_PUBLIC_UNAVAILABLE,
            "The GitHub API rate limit for this network is exhausted",
            details={
                "code": PUBLIC_KEY_SOURCE_RATE_LIMITED,
                "provider": source.provider,
            },
            retryable=True,
        )
    return SshPilotError(
        ErrorCode.KEY_PUBLIC_UNAVAILABLE,
        f"Requesting public keys failed with HTTP status {error.status}",
        details={
            "code": PUBLIC_KEY_FETCH_FAILED,
            "provider": source.provider,
            "status": error.status,
        },
        retryable=True,
    )


def _candidate_lines(source: PublicKeySource, body: bytes) -> List[str]:
    text = body.decode("utf-8", errors="replace")
    if source.provider == "gh":
        # GitHub's API form carries a key id, which ssh-import-id folds into
        # the comment as ``<user>@github/<id>``.
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, list):
            raise SshPilotError(
                ErrorCode.KEY_PUBLIC_UNAVAILABLE,
                "GitHub returned an unexpected key listing",
                details={"code": PUBLIC_KEY_FETCH_FAILED, "provider": source.provider},
                retryable=True,
            )
        lines = []
        for item in data:
            if not isinstance(item, dict):
                continue
            key, key_id = item.get("key"), item.get("id")
            if isinstance(key, str) and type(key_id) is int:
                lines.append(f"{key} {source.username}@github/{key_id}")
        return lines
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if source.provider == "gl":
            line = f"{line} {source.username}@gitlab"
        lines.append(line)
    return lines


def fetch_public_keys(
    source_text: str,
    *,
    fetch: Optional[Fetcher] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> ImportedPublicKeyList:
    """Resolve ``source_text`` and return its validated, labelled public keys."""
    source = parse_public_key_source(source_text)
    fetcher = fetch or urllib_fetch
    try:
        # Provider endpoints answer directly (200/404); only a user-supplied
        # URL may legitimately redirect.
        body = fetcher(
            source.url,
            {"User-Agent": _user_agent()},
            timeout,
            follow_redirects=source.provider == URL_PROVIDER,
        )
    except PublicKeyHttpError as exc:
        raise _http_failure(source, exc) from None
    except OSError as exc:
        raise SshPilotError(
            ErrorCode.KEY_PUBLIC_UNAVAILABLE,
            f"Could not reach the key source: {exc}",
            details={"code": PUBLIC_KEY_FETCH_FAILED, "provider": source.provider},
            retryable=True,
        ) from None

    label = IMPORT_LABEL_PREFIX + source.label
    keys: List[ImportedPublicKey] = []
    seen = set()
    for candidate in _candidate_lines(source, body):
        parsed = parse_public_key_line(candidate)
        if parsed is None:
            continue
        key_type, key_b64, comment, fingerprint = parsed
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        comment = f"{comment} {label}" if comment else label
        keys.append(
            ImportedPublicKey(
                key_type=key_type,
                fingerprint=fingerprint,
                comment=comment,
                line=f"{key_type} {key_b64} {comment}",
            )
        )
    if not keys:
        raise SshPilotError(
            ErrorCode.KEY_NOT_FOUND,
            f"No usable public keys were found at {source.label}",
            details={"code": PUBLIC_KEY_SOURCE_EMPTY, "provider": source.provider},
        )
    return ImportedPublicKeyList(source=source.label, keys=tuple(keys))
