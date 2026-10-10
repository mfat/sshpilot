"""Subscribe to assistant MCP activity and drive the AI Activity panel."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from gi.repository import GLib

from sshpilot.api.events import EventType
from sshpilot.api.models.assistant_activity import AssistantActivityEntry

logger = logging.getLogger(__name__)


class AssistantActivityController:
    """Poll + event-driven bridge from daemon activity into the GTK UI."""

    def __init__(
        self,
        client: Any,
        *,
        on_entry: Callable[[AssistantActivityEntry], None],
        on_toast: Optional[Callable[[AssistantActivityEntry], None]] = None,
    ) -> None:
        self._client = client
        self._on_entry = on_entry
        self._on_toast = on_toast
        self._subscription = None
        self._seen: set[str] = set()

    def start(self) -> None:
        self.refresh()
        subscribe = getattr(self._client, "subscribe_events", None)
        if not callable(subscribe):
            return
        try:
            self._subscription = subscribe(self._on_event)
        except Exception:  # noqa: BLE001
            logger.debug("assistant activity subscribe failed", exc_info=True)

    def stop(self) -> None:
        sub = self._subscription
        self._subscription = None
        if sub is None:
            return
        try:
            unsubscribe = getattr(sub, "unsubscribe", None)
            if callable(unsubscribe):
                unsubscribe()
            elif callable(sub):
                sub()
        except Exception:  # noqa: BLE001
            pass

    def refresh(self) -> None:
        lister = getattr(self._client, "list_assistant_activity", None)
        if not callable(lister):
            return
        try:
            snapshot = lister()
        except Exception:  # noqa: BLE001
            logger.debug("assistant activity list failed", exc_info=True)
            return
        for entry in getattr(snapshot, "entries", ()) or ():
            self._deliver(entry, toast=False)

    def _on_event(self, event: Any) -> None:
        if getattr(event, "type", None) is not EventType.ASSISTANT_ACTIVITY:
            return
        payload = getattr(event, "payload", None)
        if type(payload) is AssistantActivityEntry:
            self._deliver(payload, toast=True)

    def _deliver(self, entry: AssistantActivityEntry, *, toast: bool) -> None:
        key = f"{entry.activity_id}:{entry.state.value}"
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 2000:
            self._seen = set(list(self._seen)[-1000:])

        def _ui() -> bool:
            try:
                self._on_entry(entry)
            except Exception:  # noqa: BLE001
                logger.exception("assistant activity UI callback failed")
            if toast and entry.level in {"operate", "mutate"} and self._on_toast:
                if entry.state.value == "started":
                    try:
                        self._on_toast(entry)
                    except Exception:  # noqa: BLE001
                        logger.debug("assistant activity toast failed", exc_info=True)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_ui)
