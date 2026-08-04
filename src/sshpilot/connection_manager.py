"""Deprecated compatibility imports for the ephemeral connection model.

Saved connection state and all SSH configuration persistence are daemon-owned.
"""

from .connection_model import Connection, ConnectionState

__all__ = ["Connection", "ConnectionState"]
