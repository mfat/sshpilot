"""News and announcements for the notification center.

The feed is a JSON file kept in the repository (``data/news.json`` on
``main``), edited by hand and fetched once at startup. It is written in the
languages its author chooses -- it is data, not part of the app's translation
catalogues -- so each text field is a ``{"en": "...", "fa": "..."}`` map and the
UI language picks, falling back to English.

Schema (version 1)::

    {"version": 1, "items": [{
        "id": "2026-11-relay",            # required, stable, unique
        "date": "2026-11-20",             # required, ISO date
        "severity": "info",               # info | important | critical
        "title": {"en": "..."},           # required, needs "en"
        "body": {"en": "..."},            # optional, plain text
        "link": {"url": "https://...", "label": {"en": "Read more"}},
        "min_version": "5.0.0",           # optional, inclusive
        "max_version": "5.2.0",           # optional, inclusive
        "platforms": ["linux", "macos", "windows"],
        "packages": ["flatpak", "appimage", "native", "macos-dmg", "windows"],
        "expires": "2026-12-31"           # optional, last day shown
    }]}

An item that does not validate is skipped, never the whole feed. Fetch
failures are silent: the feed is a nicety, not something to warn about.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

NEWS_URL = "https://raw.githubusercontent.com/mfat/sshpilot/main/data/news.json"
FETCH_TIMEOUT_S = 10
#: The feed is a few kilobytes; refuse anything absurd.
MAX_FEED_BYTES = 256 * 1024
SUPPORTED_FORMAT = 1

SEVERITIES = ("info", "important", "critical")
PLATFORMS = ("linux", "macos", "windows", "bsd")
PACKAGES = ("flatpak", "appimage", "native", "macos-dmg", "windows")

_VERSION_RE = re.compile(r"^v?\d+(\.\d+){0,3}$")
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(_[A-Za-z0-9]{2,4})?$")
_MAX_TEXT = 4000


@dataclass(frozen=True)
class NewsItem:
    id: str
    date_iso: str
    severity: str = "info"
    title: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, str] = field(default_factory=dict)
    link_url: Optional[str] = None
    link_label: Mapping[str, str] = field(default_factory=dict)
    expires: Optional[str] = None


# -- environment ---------------------------------------------------------

def current_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "bsd"


def current_package() -> str:
    from .platform_utils import is_flatpak

    if is_flatpak():
        return "flatpak"
    if os.environ.get("APPIMAGE"):
        return "appimage"
    frozen = bool(getattr(sys, "frozen", False))
    if sys.platform == "darwin" and (frozen or ".app/Contents/" in sys.executable):
        return "macos-dmg"
    if sys.platform.startswith("win") and frozen:
        return "windows"
    return "native"


def current_version() -> str:
    from . import __version__

    return __version__


def pick_text(texts: Mapping[str, str], languages: Optional[Sequence[str]] = None) -> str:
    """The text for the UI language (``pt_BR`` → ``pt`` → ``en``)."""
    if not texts:
        return ""
    if languages is None:
        from .i18n import ui_language_codes

        languages = ui_language_codes()
    for code in list(languages) + ["en"]:
        value = texts.get(code)
        if value:
            return value
    return next(iter(texts.values()), "")


# -- validation ----------------------------------------------------------

def _version_tuple(value: str) -> Tuple[int, ...]:
    parts = [int(p) for p in value.lstrip("v").split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def _texts(value: Any, *, required: bool) -> Optional[Dict[str, str]]:
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        return None
    texts = {}
    for lang, text in value.items():
        if not isinstance(lang, str) or not _LANG_RE.match(lang):
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        texts[lang] = text.strip()[:_MAX_TEXT]
    if required and "en" not in texts:
        return None
    return texts


def _iso_date(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _string_list(value: Any, allowed: Iterable[str]) -> Optional[List[str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    allowed = set(allowed)
    return [v for v in value if isinstance(v, str) and v in allowed]


def parse_item(
    raw: Any,
    *,
    version: str,
    platform: str,
    package: str,
    today: date,
) -> Optional[NewsItem]:
    """Validate one item and apply its targeting; ``None`` means skip it."""
    if not isinstance(raw, dict):
        return None
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 200:
        return None
    published = _iso_date(raw.get("date"))
    if published is None:
        return None
    severity = raw.get("severity", "info")
    if severity not in SEVERITIES:
        return None
    title = _texts(raw.get("title"), required=True)
    body = _texts(raw.get("body"), required=False)
    if title is None or body is None:
        return None

    link_url = None
    link_label: Dict[str, str] = {}
    link = raw.get("link")
    if link is not None:
        if not isinstance(link, dict):
            return None
        url = link.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            return None
        link_url = url
        link_label = _texts(link.get("label"), required=False) or {}

    for key in ("min_version", "max_version"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not _VERSION_RE.match(value)):
            return None
    try:
        current = _version_tuple(version)
    except ValueError:
        current = None
    if current is not None:
        if raw.get("min_version") and current < _version_tuple(raw["min_version"]):
            return None
        if raw.get("max_version") and current > _version_tuple(raw["max_version"]):
            return None

    platforms = _string_list(raw.get("platforms"), PLATFORMS)
    packages = _string_list(raw.get("packages"), PACKAGES)
    if platforms is None or packages is None:
        return None
    if raw.get("platforms") and platform not in platforms:
        return None
    if raw.get("packages") and package not in packages:
        return None

    expires = raw.get("expires")
    if expires is not None:
        limit = _iso_date(expires)
        if limit is None:
            return None
        if today > limit:
            return None

    return NewsItem(
        id=item_id.strip(),
        date_iso=published.isoformat() + "T00:00:00+00:00",
        severity=severity,
        title=title,
        body=body,
        link_url=link_url,
        link_label=link_label,
        expires=expires,
    )


def parse_feed(
    data: Any,
    *,
    version: Optional[str] = None,
    platform: Optional[str] = None,
    package: Optional[str] = None,
    today: Optional[date] = None,
) -> Optional[List[NewsItem]]:
    """Items for this install, or ``None`` if the feed itself is unusable."""
    if not isinstance(data, dict) or data.get("version") != SUPPORTED_FORMAT:
        return None
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return None
    version = version or current_version()
    platform = platform or current_platform()
    package = package or current_package()
    today = today or date.today()
    items: List[NewsItem] = []
    seen = set()
    for raw in raw_items:
        item = parse_item(raw, version=version, platform=platform, package=package, today=today)
        if item is None:
            logger.debug("Skipping a news item that did not validate or does not apply")
            continue
        if item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
    return items


# -- fetching ------------------------------------------------------------

def fetch_feed_bytes(url: str = NEWS_URL) -> bytes:
    from urllib.request import Request, urlopen

    from . import __version__
    from .update_checker import _ssl_context

    request = Request(url, headers={"User-Agent": f"sshPilot/{__version__}"})
    with urlopen(request, timeout=FETCH_TIMEOUT_S, context=_ssl_context()) as response:
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("news feed is too large")
    return data


def fetch_news(fetch: Callable[[], bytes] = fetch_feed_bytes) -> Optional[List[NewsItem]]:
    """Fetch and parse the feed; ``None`` on any failure (logged at debug)."""
    try:
        data = json.loads(fetch().decode("utf-8"))
    except Exception as exc:
        logger.debug("News feed unavailable: %s: %s", type(exc).__name__, exc)
        return None
    items = parse_feed(data)
    if items is None:
        logger.debug("News feed has an unsupported format")
    return items


def fetch_news_async(
    on_items: Callable[[List[NewsItem]], None],
    *,
    fetch: Callable[[], bytes] = fetch_feed_bytes,
) -> threading.Thread:
    """Fetch on a worker thread; ``on_items`` runs on the GLib main loop.

    It is not called at all when the fetch fails, so a network hiccup never
    empties the news list the user already has.
    """
    from gi.repository import GLib

    def _worker() -> None:
        items = fetch_news(fetch)
        if items is None:
            return
        GLib.idle_add(lambda: (on_items(items), GLib.SOURCE_REMOVE)[1])

    thread = threading.Thread(target=_worker, name="sshpilot-news", daemon=True)
    thread.start()
    return thread
