"""Where an RDP connection opens: an sshPilot tab or FreeRDP's own window.

The tab embeds the gtk4-frdp widget (GObject Introspection namespace
``GtkFrdp-4.0``). It supports the core of an RDP session but not every
option of the RDP editor; a connection that uses one of those keeps opening
in FreeRDP's window unless the user explicitly asked for a tab.

GTK-free so it can be tested headless; ``rdp_tab`` does the presentation.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

#: Connection setting choosing the view. ``auto`` is the default.
VIEWER_KEY = "viewer"
VIEWER_AUTO = "auto"
VIEWER_TAB = "tab"
VIEWER_WINDOW = "window"


class RdpView(enum.Enum):
    TAB = "tab"
    WINDOW = "window"


class RdpViewReason(enum.Enum):
    NONE = "none"
    #: gtk4-frdp (GtkFrdp-4.0) is not installed.
    NOT_INSTALLED = "not-installed"
    #: The connection uses options the tab does not support.
    UNSUPPORTED_OPTIONS = "unsupported-options"


@dataclass(frozen=True)
class RdpViewDecision:
    view: RdpView
    reason: RdpViewReason = RdpViewReason.NONE
    #: Editor field keys behind ``UNSUPPORTED_OPTIONS``.
    options: Tuple[str, ...] = field(default_factory=tuple)
    #: Whether the user explicitly chose ``tab`` (notices are only shown
    #: when the result differs from what they asked for).
    explicit_tab: bool = False


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return str(value).strip() if value is not None else ""


def unsupported_options(data: Mapping[str, Any]) -> Tuple[str, ...]:
    """Editor field keys the tab cannot honour, in editor order."""

    found = []
    if _text(data, "display_mode") == "fullscreen":
        found.append("display_mode")
    if _text(data, "scale"):
        found.append("scale")
    if data.get("multimon"):
        found.append("multimon")
    if data.get("microphone"):
        found.append("microphone")
    if data.get("printer"):
        found.append("printer")
    if _text(data, "shared_folder"):
        found.append("shared_folder")
    if _text(data, "security") not in ("", "auto"):
        found.append("security")
    if data.get("admin"):
        found.append("admin")
    if _text(data, "gateway"):
        found.append("gateway")
    if _text(data, "extra_rdp_args"):
        found.append("extra_rdp_args")
    return tuple(found)


def decide_view(data: Mapping[str, Any], *, embedded_available: bool) -> RdpViewDecision:
    """Choose the view for a connection's data."""

    choice = _text(data, VIEWER_KEY) or VIEWER_AUTO
    if choice == VIEWER_WINDOW:
        return RdpViewDecision(RdpView.WINDOW)

    explicit = choice == VIEWER_TAB
    if not embedded_available:
        return RdpViewDecision(RdpView.WINDOW, RdpViewReason.NOT_INSTALLED,
                               explicit_tab=explicit)

    options = unsupported_options(data)
    if explicit:
        # The user asked for a tab: open it, and say which settings it ignores.
        reason = RdpViewReason.UNSUPPORTED_OPTIONS if options else RdpViewReason.NONE
        return RdpViewDecision(RdpView.TAB, reason, options, explicit_tab=True)
    if options:
        return RdpViewDecision(RdpView.WINDOW, RdpViewReason.UNSUPPORTED_OPTIONS, options)
    return RdpViewDecision(RdpView.TAB)


def display_properties(data: Mapping[str, Any]) -> Dict[str, Any]:
    """``GtkFrdp.Display`` properties for a connection's settings."""

    mode = _text(data, "display_mode") or "resize"
    return {
        # "resize": the server follows the tab size (Display Control).
        "allow_resize": mode in ("resize", "fullscreen"),
        # "original" shows the desktop 1:1, everything else fits the tab.
        "scaling": mode != "original",
        "clipboard": bool(data.get("clipboard", True)),
        "audio_playback": bool(data.get("sound", True)),
    }


#: Answers for GtkFrdp's certificate prompts.
CERT_REJECT = 0
CERT_ACCEPT_AND_STORE = 1
CERT_ACCEPT_ONCE = 2


def automatic_certificate_answer(policy: str, *, changed: bool) -> Optional[int]:
    """The answer the connection's certificate policy gives without asking,
    or ``None`` when the user has to decide.

    ``tofu`` stores an unknown certificate but never silently accepts a
    *changed* one; ``ignore`` accepts anything for this session only (the
    editor labels it insecure)."""

    if policy == "ignore":
        return CERT_ACCEPT_ONCE
    if policy == "tofu" and not changed:
        return CERT_ACCEPT_AND_STORE
    return None


def connection_data_from_details(details: Any) -> Dict[str, Any]:
    """Settings of an RDP connection from the daemon's ``ConnectionDetails``.

    Sidebar rows are summaries without the plugin fields, so the view
    decision and the tab both work from the full details (the same merge
    the connection editor does)."""

    data: Dict[str, Any] = dict(getattr(details, "data", None) or {})
    plugin_data = getattr(details, "plugin_data", None) or {}
    if isinstance(plugin_data, Mapping):
        data.update(dict(plugin_data))
    for key in ("hostname", "port", "username"):
        value = getattr(details, key, None)
        if value not in (None, ""):
            data.setdefault(key, value)
    return data


def host_and_port(data: Mapping[str, Any], default_port: int = 3389) -> Tuple[str, int]:
    host = _text(data, "host") or _text(data, "hostname")
    try:
        port = int(data.get("port") or default_port)
    except (TypeError, ValueError):
        port = default_port
    return host, port
