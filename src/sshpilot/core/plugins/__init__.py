"""Core (GTK-free) plugin contracts.

.. deprecated::
    Prefer importing from here for new headless / multi-frontend code.
    Existing plugins may continue to use ``sshpilot.plugins.api``, which
    re-exports these types for compatibility.
"""
from .contracts import (
    ALL_EVENTS,
    API_VERSION,
    Capability,
    ConnectionInfo,
    EventBus,
    Events,
    FieldSpec,
    SessionInfo,
    SpawnSpec,
)

__all__ = [
    "ALL_EVENTS",
    "API_VERSION",
    "Capability",
    "ConnectionInfo",
    "EventBus",
    "Events",
    "FieldSpec",
    "SessionInfo",
    "SpawnSpec",
]
