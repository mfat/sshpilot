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
    CONNECTIONS_CONFIG_READ = "connections.config.read"
    CONNECTIONS_CONFIG_WRITE = "connections.config.write"
    CONNECTIONS_SECRETS_WRITE = "connections.secrets.write"
    CONNECTIONS_SECRETS_STATUS_READ = "connections.secrets.status.read"
    CONNECTIONS_SECRETS_REVEAL = "connections.secrets.reveal"
    CONNECTIONS_METADATA_WRITE = "connections.metadata.write"
    CONNECTIONS_GROUPS = "connections.groups"
    CONNECTIONS_SPLIT = "connections.split"
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
    SFTP_PRIVILEGED_FILE = "sftp.privileged_file"
    TRANSFERS_READ = "transfers.read"
    TRANSFERS_WRITE = "transfers.write"
    TRANSFERS_EVENTS = "transfers.events"
    TRANSFERS_UPLOAD = "transfers.upload"
    TRANSFERS_DOWNLOAD = "transfers.download"
    TRANSFERS_SCP = "transfers.scp"
    PORT_FORWARDING = "port_forwarding"
    FORWARDS_READ = "forwards.read"
    FORWARDS_WRITE = "forwards.write"
    FORWARDS_EVENTS = "forwards.events"
    FORWARDS_LOCAL = "forwards.local"
    FORWARDS_REMOTE = "forwards.remote"
    FORWARDS_DYNAMIC = "forwards.dynamic"
    DAEMON_STATUS = "daemon.status"
    DAEMON_CONTROL = "daemon.control"
    DAEMON_EVENTS = "daemon.events"
    KNOWN_HOSTS_READ = "known_hosts.read"
    KNOWN_HOSTS_WRITE = "known_hosts.write"
    KEYS_READ = "keys.read"
    KEYS_WRITE = "keys.write"
    IDENTITY_READ = "identity.read"
    IDENTITY_WRITE = "identity.write"
    IDENTITY_OPERATE = "identity.operate"
    OPERATIONS_READ = "operations.read"
    OPERATIONS_CONTROL = "operations.control"
    BROADCAST_READ = "broadcast.read"
    BROADCAST_WRITE = "broadcast.write"
    BROADCAST_EVENTS = "broadcast.events"
    SSH_OVERRIDES_READ = "ssh_overrides.read"
    SSH_OVERRIDES_WRITE = "ssh_overrides.write"
    PLUGINS = "plugins"
    PLUGIN_SETTINGS_READ = "plugins.settings.read"
    PLUGIN_SETTINGS_WRITE = "plugins.settings.write"
    SECRETS = "secrets"
    SECRETS_READ = "secrets.read"
    SECRETS_WRITE = "secrets.write"
    SECRETS_OPERATE = "secrets.operate"
    SECRETS_TRANSFER = "secrets.transfer"


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
