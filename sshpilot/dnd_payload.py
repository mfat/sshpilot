"""Internal drag-and-drop payloads as pasteboard-safe strings.

GTK's macOS backend serializes ``Gdk.ContentProvider`` values onto
NSPasteboard when a drag begins. ``GObject.TYPE_PYOBJECT`` (Python dicts)
has no pasteboard representation and aborts immediately with::

    -[%s localObject]: unrecognized selector sent to instance
    in NSCoreDragManager beginDraggingSessionWithItems:

See GitHub issues #704, #847, #876. The file-manager path already avoided
this by advertising a JSON string (PR #495); sidebar / split-view /
command-block drags must do the same.

Encode every in-app drag as a JSON string under ``format`` =
``sshpilot_internal_drag``, and receive it with ``Gtk.DropTarget`` typed as
``GObject.TYPE_STRING``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from gi.repository import Gdk, GObject, Gtk

logger = logging.getLogger(__name__)

DND_FORMAT = "sshpilot_internal_drag"


def encode_dnd_payload(payload: dict) -> str:
    """Serialize an internal drag payload to a pasteboard-safe JSON string."""
    return json.dumps(
        {"format": DND_FORMAT, "payload": payload},
        separators=(",", ":"),
        sort_keys=True,
    )


def content_provider_for_payload(payload: dict) -> Gdk.ContentProvider:
    """Return a ``TYPE_STRING`` content provider for *payload*."""
    return Gdk.ContentProvider.new_for_value(encode_dnd_payload(payload))


def new_internal_drop_target(
    actions: Gdk.DragAction = Gdk.DragAction.MOVE,
) -> Gtk.DropTarget:
    """``DropTarget`` that accepts the JSON strings from our drag sources."""
    return Gtk.DropTarget.new(type=GObject.TYPE_STRING, actions=actions)


def _unwrap_gvalue(value: Any) -> Any:
    if isinstance(value, GObject.Value):
        for getter in ("get_string", "get_boxed", "get_object", "get"):
            try:
                extracted = getattr(value, getter)()
                if extracted is not None:
                    return extracted
            except Exception:
                continue
        return None
    if hasattr(value, "get_value"):
        try:
            return value.get_value()
        except Exception:
            pass
    return value


def _payload_from_mapping(container: dict) -> Optional[dict]:
    if container.get("format") == DND_FORMAT:
        inner = container.get("payload")
        return inner if isinstance(inner, dict) else None
    # Bare payload dict (unit tests / in-process callers).
    if "type" in container:
        return container
    return None


def decode_dnd_payload(value: Any) -> Optional[dict]:
    """Return the inner payload dict, or ``None`` if *value* is not ours.

    Accepts:
    - JSON string from :func:`encode_dnd_payload`
    - bare payload ``dict`` (for unit tests)
    - ``GObject.Value`` wrapping either of the above
    """
    value = _unwrap_gvalue(value)
    if value is None:
        return None

    if isinstance(value, dict):
        return _payload_from_mapping(value)

    if isinstance(value, str):
        try:
            container = json.loads(value)
        except json.JSONDecodeError:
            return None
        if not isinstance(container, dict):
            return None
        return _payload_from_mapping(container)

    return None
