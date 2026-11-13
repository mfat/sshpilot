"""App-wide notification history behind the status bar's bell.

One :class:`NotificationStore` per application. It holds two lists:

* **activity** -- entries the app sends with :func:`notify`, each one a code
  from :mod:`sshpilot.gtk.notification_messages` plus its parameters;
* **news** -- announcements fetched by :mod:`sshpilot.news_feed`.

Both persist in ``notifications.json`` under the state directory. Activity is
capped at :data:`ACTIVITY_LIMIT` entries; a news item the user dismissed is
remembered by id so a later fetch never brings it back.

The store does no translation of its own. Activity entries keep their code and
parameters so the dialog renders them in the current language, plus the
sentence as first rendered, used only if the code later leaves the catalogue.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from gi.repository import Gio, GLib, GObject

logger = logging.getLogger(__name__)

TAB_ACTIVITY = "activity"
TAB_NEWS = "news"
TABS = (TAB_ACTIVITY, TAB_NEWS)

ACTIVITY_LIMIT = 200
#: Upper bound on remembered dismissed news ids, so the file cannot grow forever.
DISMISSED_NEWS_LIMIT = 1000
#: A single stored parameter value is cut to this many characters.
PARAM_MAX_CHARS = 2000
STORE_FILENAME = "notifications.json"
_FORMAT_VERSION = 1
_SAVE_DELAY_MS = 500


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_param(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    from .logging_support import sanitize_log_text

    text = sanitize_log_text(str(value))
    if len(text) > PARAM_MAX_CHARS:
        text = text[: PARAM_MAX_CHARS - 1] + "…"
    return text


@dataclass
class NotificationEntry:
    """One row in the notification center."""

    id: str
    tab: str
    created: str
    read: bool = False
    # Activity entries
    code: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    fallback_title: str = ""
    fallback_details: str = ""
    severity: str = "info"
    # News entries: per-language text, chosen when shown.
    news_id: Optional[str] = None
    title_i18n: Dict[str, str] = field(default_factory=dict)
    body_i18n: Dict[str, str] = field(default_factory=dict)
    link_url: Optional[str] = None
    link_label_i18n: Dict[str, str] = field(default_factory=dict)
    expires: Optional[str] = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Optional["NotificationEntry"]:
        try:
            names = {f for f in cls.__dataclass_fields__}
            values = {k: v for k, v in data.items() if k in names}
            entry = cls(**values)
        except TypeError:
            return None
        if entry.tab not in TABS or not isinstance(entry.id, str) or not entry.id:
            return None
        if not isinstance(entry.params, dict):
            entry.params = {}
        return entry

    def created_datetime(self) -> datetime:
        try:
            value = datetime.fromisoformat(self.created)
        except (TypeError, ValueError):
            return datetime.fromtimestamp(0, timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def is_expired(self, today: Optional[date] = None) -> bool:
        if not self.expires:
            return False
        try:
            limit = date.fromisoformat(self.expires)
        except ValueError:
            return False
        return (today or date.today()) > limit


class NotificationStore(GObject.Object):
    """The model behind the bell badge and the notification center dialog."""

    __gtype_name__ = "SshPilotNotificationStore"

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    unread_count = GObject.Property(type=int, default=0)
    unread_activity = GObject.Property(type=int, default=0)
    unread_news = GObject.Property(type=int, default=0)

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        schedule: Optional[Callable[[int, Callable[[], bool]], int]] = None,
        today: Optional[Callable[[], date]] = None,
    ) -> None:
        super().__init__()
        self._path = path
        self._entries: List[NotificationEntry] = []
        self._dismissed_news: List[str] = []
        self._save_source = 0
        self._schedule = schedule or GLib.timeout_add
        self._today = today or date.today
        self._load()

    # -- queries -----------------------------------------------------------

    def entries(self, tab: str) -> List[NotificationEntry]:
        """Entries of ``tab``, newest first."""
        rows = [e for e in self._entries if e.tab == tab]
        rows.sort(key=lambda e: e.created_datetime(), reverse=True)
        return rows

    def get(self, entry_id: str) -> Optional[NotificationEntry]:
        for entry in self._entries:
            if entry.id == entry_id:
                return entry
        return None

    @property
    def dismissed_news_ids(self) -> List[str]:
        return list(self._dismissed_news)

    # -- activity ----------------------------------------------------------

    def notify(self, code: str, **params: Any) -> Optional[str]:
        """Record an activity entry for a catalogued ``code``.

        Returns the new entry's id, or ``None`` when the code is unknown or
        disabled. Safe from any thread: off the main thread the entry is added
        on the next main-loop iteration and ``None`` is returned.
        """
        if threading.current_thread() is not threading.main_thread():
            GLib.idle_add(lambda: (self.notify(code, **params), GLib.SOURCE_REMOVE)[1])
            return None
        from .gtk.notification_messages import format_notification

        clean = {str(k): _sanitize_param(v) for k, v in params.items()}
        rendered = format_notification(code, clean)
        if rendered is None:
            logger.debug("Notification %r is not in the catalogue or is disabled", code)
            return None
        entry = NotificationEntry(
            id=secrets.token_hex(16),
            tab=TAB_ACTIVITY,
            created=_utc_now_iso(),
            code=code,
            params=clean,
            fallback_title=rendered.title,
            fallback_details=rendered.details,
            severity=rendered.severity.value,
        )
        self._entries.append(entry)
        self._trim_activity()
        self._changed()
        return entry.id

    def add_text_entry(self, title: str, *, details: str = "", severity: str = "info",
                       source: str = "") -> Optional[str]:
        """Record an Activity entry with ready-made text (plugins, API >= 1.17).

        App code uses :meth:`notify` with a catalogued code instead.
        """
        if threading.current_thread() is not threading.main_thread():
            GLib.idle_add(lambda: (self.add_text_entry(
                title, details=details, severity=severity, source=source
            ), GLib.SOURCE_REMOVE)[1])
            return None
        from .gtk.notification_messages import NotificationSeverity

        title = _sanitize_param(title or "").strip()
        if not title:
            return None
        try:
            severity = NotificationSeverity(severity).value
        except ValueError:
            severity = NotificationSeverity.INFO.value
        entry = NotificationEntry(
            id=secrets.token_hex(16),
            tab=TAB_ACTIVITY,
            created=_utc_now_iso(),
            fallback_title=title,
            fallback_details=_sanitize_param(details or ""),
            severity=severity,
            params={"source": source} if source else {},
        )
        self._entries.append(entry)
        self._trim_activity()
        self._changed()
        return entry.id

    def _trim_activity(self) -> None:
        activity = self.entries(TAB_ACTIVITY)
        if len(activity) <= ACTIVITY_LIMIT:
            return
        drop = {e.id for e in activity[ACTIVITY_LIMIT:]}
        self._entries = [e for e in self._entries if e.id not in drop]

    # -- news --------------------------------------------------------------

    def set_news(self, items: Iterable[Any]) -> None:
        """Replace the news list with the feed's current items.

        ``items`` are :class:`sshpilot.news_feed.NewsItem`. A dismissed id never
        comes back, an item already shown keeps its read state, and an item the
        feed no longer carries is dropped (an announcement taken back).
        """
        dismissed = set(self._dismissed_news)
        existing = {e.news_id: e for e in self._entries if e.tab == TAB_NEWS}
        news: List[NotificationEntry] = []
        for item in items:
            if item.id in dismissed:
                continue
            previous = existing.get(item.id)
            entry = NotificationEntry(
                id=previous.id if previous else secrets.token_hex(16),
                tab=TAB_NEWS,
                created=item.date_iso,
                read=previous.read if previous else False,
                severity=item.severity,
                news_id=item.id,
                title_i18n=dict(item.title),
                body_i18n=dict(item.body),
                link_url=item.link_url,
                link_label_i18n=dict(item.link_label),
                expires=item.expires,
            )
            if entry.is_expired(self._today()):
                continue
            news.append(entry)
        self._entries = [e for e in self._entries if e.tab != TAB_NEWS] + news
        self._changed()

    # -- user actions ------------------------------------------------------

    def mark_read(self, tab: str) -> None:
        changed = False
        for entry in self._entries:
            if entry.tab == tab and not entry.read:
                entry.read = True
                changed = True
        if changed:
            self._changed()

    def dismiss(self, entry_id: str) -> None:
        entry = self.get(entry_id)
        if entry is None:
            return
        self._entries.remove(entry)
        if entry.news_id:
            self._remember_dismissed([entry.news_id])
        self._changed()

    def clear(self, tab: str) -> None:
        removed = [e for e in self._entries if e.tab == tab]
        if not removed:
            return
        self._entries = [e for e in self._entries if e.tab != tab]
        self._remember_dismissed([e.news_id for e in removed if e.news_id])
        self._changed()

    def _remember_dismissed(self, news_ids: Iterable[str]) -> None:
        for news_id in news_ids:
            if news_id not in self._dismissed_news:
                self._dismissed_news.append(news_id)
        if len(self._dismissed_news) > DISMISSED_NEWS_LIMIT:
            self._dismissed_news = self._dismissed_news[-DISMISSED_NEWS_LIMIT:]

    # -- bookkeeping -------------------------------------------------------

    def _recount(self) -> None:
        activity = sum(1 for e in self._entries if e.tab == TAB_ACTIVITY and not e.read)
        news = sum(1 for e in self._entries if e.tab == TAB_NEWS and not e.read)
        if self.unread_activity != activity:
            self.unread_activity = activity
        if self.unread_news != news:
            self.unread_news = news
        if self.unread_count != activity + news:
            self.unread_count = activity + news

    def _changed(self) -> None:
        self._recount()
        self.emit("changed")
        self._schedule_save()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self._path:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("Notification history unreadable; starting empty", exc_info=True)
            return
        if not isinstance(data, dict):
            return
        entries = []
        for raw in data.get("entries") or []:
            if isinstance(raw, dict):
                entry = NotificationEntry.from_json(raw)
                if entry is not None:
                    entries.append(entry)
        dismissed = [str(x) for x in data.get("dismissed_news_ids") or [] if x]
        self._dismissed_news = dismissed[-DISMISSED_NEWS_LIMIT:]
        today = self._today()
        self._entries = [
            e for e in entries
            if not (e.tab == TAB_NEWS and (e.is_expired(today) or e.news_id in dismissed))
        ]
        self._trim_activity()
        self._recount()

    def _schedule_save(self) -> None:
        if not self._path or self._save_source:
            return

        def _run() -> bool:
            self._save_source = 0
            self.flush()
            return False

        self._save_source = self._schedule(_SAVE_DELAY_MS, _run)

    def flush(self) -> None:
        """Write the history now (also called on shutdown)."""
        if self._save_source:
            try:
                GLib.source_remove(self._save_source)
            except Exception:
                pass
            self._save_source = 0
        if not self._path:
            return
        payload = {
            "version": _FORMAT_VERSION,
            "entries": [asdict(e) for e in self._entries],
            "dismissed_news_ids": list(self._dismissed_news),
        }
        try:
            from .ssh_config_utils import atomic_write_text

            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            atomic_write_text(
                self._path,
                json.dumps(payload, ensure_ascii=False, indent=1),
                mode=0o600,
            )
        except Exception:
            logger.warning("Could not save notification history", exc_info=True)


def default_store_path() -> str:
    from .platform_utils import get_state_dir

    return os.path.join(get_state_dir(), STORE_FILENAME)


def get_store() -> Optional[NotificationStore]:
    app = Gio.Application.get_default()
    return getattr(app, "notification_store", None) if app is not None else None


def notify(code: str, **params: Any) -> None:
    """Send a catalogued notification to the app's notification center.

    A no-op (logged at debug) when the code is not in
    :data:`sshpilot.gtk.notification_messages.NOTIFICATIONS`, or when no
    application is running.
    """
    if threading.current_thread() is not threading.main_thread():
        GLib.idle_add(lambda: (notify(code, **params), GLib.SOURCE_REMOVE)[1])
        return
    store = get_store()
    if store is None:
        logger.debug("No notification store; dropping %r", code)
        return
    store.notify(code, **params)
