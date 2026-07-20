"""Fetch and verify third-party plugins from the discovery registry.

Network-only, no GTK — kept separate from the preferences UI so it can be unit
tested and run on a worker thread. TLS reuses the update checker's certifi-backed
context so it works inside Flatpak/PyInstaller bundles.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .api import API_VERSION

DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/mfat/sshpilot-plugins/main/plugins.json")
HTTP_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 60


class RegistryError(RuntimeError):
    """A registry fetch/download/verification failed (user-facing message)."""


def _user_agent() -> str:
    try:
        from .. import __version__ as version
    except Exception:
        version = "0"
    return f"sshPilot/{version}"


def _ssl_ctx() -> ssl.SSLContext:
    try:
        from ..update_checker import _ssl_context
        return _ssl_context()
    except Exception:
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()


def _read_url(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        raise RegistryError(f"Could not reach {url}: {exc}") from exc


def fetch_index(url: str = DEFAULT_REGISTRY_URL, timeout: int = HTTP_TIMEOUT) -> dict:
    raw = _read_url(url, timeout)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RegistryError(f"Invalid registry JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise RegistryError("Unsupported or missing registry schemaVersion.")
    return data


def list_entries(index: dict) -> List[Dict[str, Any]]:
    """Normalize the index to one installable row per plugin (its latest version)."""
    out: List[Dict[str, Any]] = []
    for plugin in (index.get("plugins") or []):
        if not isinstance(plugin, dict) or not plugin.get("id"):
            continue
        versions = plugin.get("versions") or []
        latest = plugin.get("latestVersion")
        ver = None
        if latest:
            ver = next((v for v in versions if v.get("version") == latest), None)
        if ver is None and versions:
            ver = versions[-1]
        if not isinstance(ver, dict):
            continue
        pkg = ver.get("package") or {}
        download_url = pkg.get("downloadUrl")
        checksum_url = pkg.get("checksumUrl")
        if not download_url or not checksum_url:
            continue
        api = ver.get("api_version")
        out.append({
            "id": str(plugin["id"]),
            "name": plugin.get("name") or plugin["id"],
            "description": plugin.get("description") or "",
            "author": plugin.get("author") or "",
            "homepage": plugin.get("homepage") or "",
            "version": ver.get("version") or "",
            "api_version": api,
            "permissions": [str(p) for p in (ver.get("permissions") or [])
                            if isinstance(p, str)],
            "download_url": download_url,
            "checksum_url": checksum_url,
            "compatible": api == API_VERSION[0],
        })
    return out


def _parse_checksum(raw: bytes) -> Optional[str]:
    """A .sha256 asset is typically '<hex>  filename'; take the first hex token."""
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split()[0].lower()
        if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
            return token
    return None


def download_package(download_url: str, checksum_url: str, dest: str,
                     timeout: int = DOWNLOAD_TIMEOUT) -> str:
    """Download the package to ``dest`` only if its SHA-256 matches the sibling
    checksum asset. Returns the verified hash; raises RegistryError otherwise."""
    expected = _parse_checksum(_read_url(checksum_url, timeout))
    if not expected:
        raise RegistryError("Could not read the package checksum.")
    data = _read_url(download_url, timeout)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RegistryError("Checksum mismatch — refusing to install.")
    with open(dest, "wb") as fh:
        fh.write(data)
    return actual
