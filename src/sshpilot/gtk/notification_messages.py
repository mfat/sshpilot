"""What the notification center is allowed to record -- the one place to curate it.

The Activity tab of the notification center records nothing on its own. An
entry appears only when its code has a :data:`NOTIFICATIONS` entry and
something sends it:

* GTK code, anywhere in the app::

      from sshpilot.notification_store import notify
      notify("transfer.failed", name="web-01", error=str(exc))

* or a daemon event, by adding a row to :data:`NOTIFY_EVENT_ROUTES` below
  (same shape as the status bar's ``EVENT_ROUTES``).

Adding one is one catalogue entry plus one call::

    NOTIFICATIONS["transfer.failed"] = NotificationSpec(
        title=N_("Transfer to “{name}” failed"),
        details=N_("{error}"),
        severity=NotificationSeverity.ERROR,
        action=ActionSpec(label=N_("Open Log"), action_name="win.view-logs"),
    )

An entry's button runs an existing ``Gio`` action (``win.…`` / ``app.…``),
optionally with a string target taken from one of the entry's parameters
(``target_param``). Entries store their code and parameters, not the rendered
sentence, so they show in the current language after a restart.

Parameters are written to disk with the history: never pass a secret, and keep
them short. They pass through the log redactor before they are stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from ..i18n import gettext as _, ngettext
from ..i18n import N_  # noqa: F401  (used by entries added to the catalogue)


class NotificationSeverity(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    IMPORTANT = "important"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


SEVERITY_ICONS: Mapping[NotificationSeverity, str] = {
    NotificationSeverity.INFO: "dialog-information-symbolic",
    NotificationSeverity.SUCCESS: "object-select-symbolic",
    NotificationSeverity.IMPORTANT: "starred-symbolic",
    NotificationSeverity.WARNING: "dialog-warning-symbolic",
    NotificationSeverity.ERROR: "dialog-error-symbolic",
    NotificationSeverity.CRITICAL: "dialog-error-symbolic",
}

SEVERITY_CSS_CLASSES: Mapping[NotificationSeverity, str] = {
    NotificationSeverity.INFO: "dim-label",
    NotificationSeverity.SUCCESS: "success",
    NotificationSeverity.IMPORTANT: "accent",
    NotificationSeverity.WARNING: "warning",
    NotificationSeverity.ERROR: "error",
    NotificationSeverity.CRITICAL: "error",
}


@dataclass(frozen=True)
class ActionSpec:
    """The one button an entry may carry: an existing Gio action."""

    label: str
    action_name: str
    target_param: Optional[str] = None


@dataclass(frozen=True)
class NotificationSpec:
    title: str
    severity: NotificationSeverity = NotificationSeverity.INFO
    details: Optional[str] = None
    plural: Optional[str] = None
    action: Optional[ActionSpec] = None
    enabled: bool = True


@dataclass(frozen=True)
class RenderedNotification:
    title: str
    details: str
    severity: NotificationSeverity
    action_label: Optional[str] = None
    action_name: Optional[str] = None
    action_target: Optional[str] = None


#: code -> spec. Ships empty: nothing is recorded until we add it.
NOTIFICATIONS: Dict[str, NotificationSpec] = {}

#: EventType -> route returning ``(code, params)`` or ``None``, like the status
#: bar's routes. Ships empty.
NOTIFY_EVENT_ROUTES: Dict[
    Any,
    Callable[[Any, Callable[[Optional[str]], str]], Optional[Tuple[str, Dict[str, Any]]]],
] = {}


def _format(template: str, source: str, values: Mapping[str, Any]) -> Optional[str]:
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        try:
            return source.format(**values)
        except (KeyError, IndexError, ValueError):
            return None


def is_enabled(code: str) -> bool:
    spec = NOTIFICATIONS.get(code)
    return spec is not None and spec.enabled


def format_notification(
    code: str, params: Optional[Mapping[str, Any]] = None
) -> Optional[RenderedNotification]:
    """Translate and format ``code``; ``None`` if it is unknown or disabled."""
    spec = NOTIFICATIONS.get(code)
    if spec is None or not spec.enabled:
        return None
    values = dict(params or {})
    if spec.plural is not None:
        count = int(values.get("count", 0) or 0)
        title = _format(ngettext(spec.title, spec.plural, count), spec.title, values)
    else:
        title = _format(_(spec.title), spec.title, values)
    if title is None:
        return None
    details = ""
    if spec.details:
        details = _format(_(spec.details), spec.details, values) or ""
    action_label = action_name = action_target = None
    if spec.action is not None:
        action_label = _(spec.action.label)
        action_name = spec.action.action_name
        if spec.action.target_param:
            target = values.get(spec.action.target_param)
            action_target = None if target is None else str(target)
    return RenderedNotification(
        title=title,
        details=details,
        severity=spec.severity,
        action_label=action_label,
        action_name=action_name,
        action_target=action_target,
    )
