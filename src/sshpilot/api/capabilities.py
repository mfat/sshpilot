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
    INTERACTIONS_READ = "interactions.read"
    INTERACTIONS_RESPOND = "interactions.respond"
    INTERACTIONS_EVENTS = "interactions.events"
    INTERACTIONS_HOST_KEY = "interactions.host_key"
    INTERACTIONS_PASSWORD = "interactions.password"
    INTERACTIONS_PASSPHRASE = "interactions.passphrase"
    SFTP = "sftp"
    SFTP_READ = "sftp.read"
    SFTP_WRITE = "sftp.write"
    SFTP_EVENTS = "sftp.events"
    SFTP_METADATA = "sftp.metadata"
    SFTP_MUTATE = "sftp.mutate"
    TRANSFERS_READ = "transfers.read"
    TRANSFERS_WRITE = "transfers.write"
    TRANSFERS_EVENTS = "transfers.events"
    TRANSFERS_UPLOAD = "transfers.upload"
    TRANSFERS_DOWNLOAD = "transfers.download"
    PORT_FORWARDING = "port_forwarding"
    FORWARDS_READ = "forwards.read"
    FORWARDS_WRITE = "forwards.write"
    FORWARDS_EVENTS = "forwards.events"
    FORWARDS_LOCAL = "forwards.local"
    FORWARDS_REMOTE = "forwards.remote"
    FORWARDS_DYNAMIC = "forwards.dynamic"
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
