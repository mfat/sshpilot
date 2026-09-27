"""One-line target descriptions for non-SSH connections.

A protocol backend knows how to say what its connection reaches (``web ·
podman``, ``/dev/ttyUSB0 @ 115200``), but backends are plugins and core does
not import plugins. The daemon installs a describer at startup (see
``sshpilot.daemon.server``); until then, and in tests that build a repository
directly, every summary is empty and list views fall back to the host.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

Describer = Callable[[str, Mapping[str, Any]], str]

_describer: Optional[Describer] = None


def set_describer(describer: Optional[Describer]) -> None:
    global _describer
    _describer = describer


def describe(protocol: str, data: Mapping[str, Any]) -> str:
    """The record's summary, or ``""`` for SSH and whenever none is known.

    SSH is never described here: its list line is built from the host
    columns as it always was.
    """
    describer = _describer
    if describer is None or (protocol or "ssh") == "ssh":
        return ""
    try:
        text = describer(protocol, data)
    except Exception:
        logger.debug("Target summary failed for protocol %r", protocol, exc_info=True)
        return ""
    if type(text) is not str:
        return ""
    # One line, bounded, and safe for the summary DTO's validation.
    return " ".join(text.replace("\x00", "").split())[:200]
