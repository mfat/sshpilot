"""Registry of available protocol backends.

Core code looks up the backend for a connection via::

    from sshpilot.plugins.registry import protocol_registry
    backend = protocol_registry().get(getattr(connection, "protocol", "ssh"))
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .api import ProtocolBackend

logger = logging.getLogger(__name__)


class ProtocolRegistry:
    def __init__(self) -> None:
        self._backends: Dict[str, ProtocolBackend] = {}
        # protocol_id -> id of the plugin that registered it (for scoping the
        # PluginContext the terminal builds at spawn time).
        self._plugin_ids: Dict[str, Optional[str]] = {}

    def register(self, backend: ProtocolBackend, *,
                 plugin_id: Optional[str] = None) -> None:
        pid = backend.protocol_id
        if not pid:
            raise ValueError(f"{backend!r} has an empty protocol_id")
        if pid in self._backends:
            # First registration wins so a user plugin cannot silently
            # shadow the built-in SSH backend.
            logger.warning("Protocol %r already registered; ignoring %r",
                           pid, type(backend).__name__)
            return
        self._backends[pid] = backend
        self._plugin_ids[pid] = plugin_id
        logger.debug("Registered protocol backend %r (%s) from plugin %r",
                     pid, backend.display_name, plugin_id)

    def plugin_id_for(self, protocol_id: str) -> Optional[str]:
        """Id of the plugin that registered ``protocol_id`` (None if unknown
        or registered without one)."""
        return self._plugin_ids.get(protocol_id)

    def get(self, protocol_id: str) -> ProtocolBackend:
        try:
            return self._backends[protocol_id]
        except KeyError:
            raise KeyError(
                f"No backend for protocol {protocol_id!r}. "
                f"Available: {sorted(self._backends)}"
            ) from None

    def get_or_none(self, protocol_id: str) -> Optional[ProtocolBackend]:
        return self._backends.get(protocol_id)

    def all(self) -> List[ProtocolBackend]:
        return list(self._backends.values())


_registry: Optional[ProtocolRegistry] = None


def protocol_registry() -> ProtocolRegistry:
    global _registry
    if _registry is None:
        _registry = ProtocolRegistry()
    return _registry


def capabilities_for(connection) -> frozenset:
    """Capabilities of the backend handling this connection.

    UI code gates protocol-specific actions (SFTP, ssh-copy-id, port
    forwarding, ...) on these instead of checking ``protocol == 'ssh'``.
    Unknown protocol -> empty set, so SSH-only UI hides rather than breaks."""
    backend = protocol_registry().get_or_none(
        getattr(connection, "protocol", "ssh"))
    return backend.capabilities() if backend is not None else frozenset()
