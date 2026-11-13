"""What the status bar is allowed to say -- the one place to curate it.

The status bar shows nothing on its own. A message appears only when it has an
entry in :data:`STATUS_MESSAGES` and something posts its code:

* GTK code, anywhere in the app::

      from sshpilot.status_bar import post_status
      post_status("transfer.finished", count=3)

* or a daemon event, by adding a row to :data:`EVENT_ROUTES` (the controller
  subscribes to the daemon only for the event types listed there).

Adding a message is therefore one catalogue entry plus one call (or one route)::

    STATUS_MESSAGES["transfer.finished"] = StatusMessageSpec(
        template=N_("Transferred {count} item"),
        plural=N_("Transferred {count} items"),   # picks ngettext on ``count``
        level=StatusLevel.SUCCESS,
        timeout_s=5,                              # None: stays until replaced
    )

    def _route_transfer_completed(event, lookup):
        return "transfer.finished", {"count": event.payload.completed_items}

    EVENT_ROUTES[EventType.TRANSFER_COMPLETED] = _route_transfer_completed

Set ``enabled=False`` to silence a message without deleting it. Codes are plain
stable strings, never English sentences, so posting code stays translatable.
Wording is translated when it is shown, then formatted -- translate before
formatting, as everywhere else in the app.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from ..i18n import gettext as _, ngettext
from ..i18n import N_  # noqa: F401  (used by entries added to the catalogue)


class StatusLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


#: Symbolic icon per level. All ship with the Adwaita icon theme on the oldest
#: supported stack.
LEVEL_ICONS: Mapping[StatusLevel, str] = {
    StatusLevel.INFO: "dialog-information-symbolic",
    StatusLevel.SUCCESS: "object-select-symbolic",
    StatusLevel.WARNING: "dialog-warning-symbolic",
    StatusLevel.ERROR: "dialog-error-symbolic",
}

#: libadwaita style classes for each level (``dim-label`` keeps info quiet).
LEVEL_CSS_CLASSES: Mapping[StatusLevel, str] = {
    StatusLevel.INFO: "dim-label",
    StatusLevel.SUCCESS: "success",
    StatusLevel.WARNING: "warning",
    StatusLevel.ERROR: "error",
}


@dataclass(frozen=True)
class StatusMessageSpec:
    """One curated status-bar message.

    ``template`` and ``plural`` are English source strings marked with ``N_``;
    they are translated when shown. With ``plural`` set, the ``count``
    parameter selects the form through ``ngettext``.
    """

    template: str
    level: StatusLevel = StatusLevel.INFO
    timeout_s: Optional[int] = 5
    enabled: bool = True
    plural: Optional[str] = None


@dataclass(frozen=True)
class RenderedStatus:
    text: str
    level: StatusLevel
    timeout_s: Optional[int]


#: code -> spec. Ships empty: nothing reaches the status bar until we add it.
STATUS_MESSAGES: Dict[str, StatusMessageSpec] = {}

#: A route turns one daemon event into ``(code, params)``, or ``None`` to skip
#: it. ``lookup`` resolves a connection id to its display name.
EventRoute = Callable[[Any, Callable[[Optional[str]], str]], Optional[Tuple[str, Dict[str, Any]]]]

#: EventType -> route. Ships empty, so the status bar subscribes to nothing.
EVENT_ROUTES: Dict[Any, EventRoute] = {}


def format_status_message(
    code: str, params: Optional[Mapping[str, Any]] = None
) -> Optional[RenderedStatus]:
    """Translate and format ``code``; ``None`` if it is unknown or disabled."""
    spec = STATUS_MESSAGES.get(code)
    if spec is None or not spec.enabled:
        return None
    values = dict(params or {})
    if spec.plural is not None:
        count = int(values.get("count", 0) or 0)
        template = ngettext(spec.template, spec.plural, count)
    else:
        template = _(spec.template)
    try:
        text = template.format(**values)
    except (KeyError, IndexError, ValueError):
        # A translation that drops or misspells a placeholder must not take the
        # status bar down; show the untranslated sentence instead.
        try:
            text = spec.template.format(**values)
        except (KeyError, IndexError, ValueError):
            return None
    return RenderedStatus(text=text, level=spec.level, timeout_s=spec.timeout_s)
