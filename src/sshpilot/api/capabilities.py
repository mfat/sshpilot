"""Protocol capability discovery."""

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet

from .models.common import ClientInfo, CompatibilityResult, CoreInfo


class Capability(str, Enum):
    """Stable capability identifiers advertised by client implementations."""

    CONNECTIONS_READ = "connections.read"
    CONNECTIONS_EVENTS = "connections.events"
    CONNECTIONS_WRITE = "connections.write"
    SESSIONS_READ = "sessions.read"
    SESSIONS_WRITE = "sessions.write"
    SESSIONS_EVENTS = "sessions.events"
    TERMINAL = "terminal"
    TERMINAL_ATTACH = "terminal.attach"
    TERMINAL_OUTPUT = "terminal.output"
    TERMINAL_INPUT = "terminal.input"
    TERMINAL_RESIZE = "terminal.resize"
    TERMINAL_REPLAY = "terminal.replay"
    INTERACTIONS = "interactions"
    SFTP = "sftp"
    PORT_FORWARDING = "port_forwarding"
    PLUGINS = "plugins"
    SECRETS = "secrets"


@dataclass(frozen=True)
class Capabilities:
    """Capabilities and version information reported by a client."""

    protocol_version: str
    api_implementation_version: str
    client: ClientInfo
    core: CoreInfo
    supported: FrozenSet[Capability]
    compatibility: CompatibilityResult

    def supports(self, capability: Capability) -> bool:
        return capability in self.supported
