"""Frontend-neutral connection request and response models."""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Mapping, Tuple, Union

from .common import ConnectionId, require_identifier



# -- Patch sentinels --------------------------------------------------------

_UNSET_TYPE = type("_UNSET_TYPE", (), {"__repr__": lambda s: "UNSET"})()
UNSET = _UNSET_TYPE


# -- Forwarding rule --------------------------------------------------------

@dataclass(frozen=True)
class ForwardingRule:
    """One port-forwarding rule.

    The field names exactly match the existing dict schema used by
    ``format_ssh_config_entry`` and ``_parse_forwarding_rules_from_config``
    so that round-tripping through the daemon is lossless.
    """

    type: str  # "local" | "remote" | "dynamic"
    listen_port: int
    listen_addr: str = ""
    remote_host: str = ""
    remote_port: int = 0
    local_host: str = ""  # remote forwards only
    local_port: int = 0  # remote forwards only
    enabled: bool = True
    socks: bool = False

    def __post_init__(self) -> None:
        if self.type not in ("local", "remote", "dynamic"):
            raise ValueError(f"forwarding rule type must be local/remote/dynamic, got {self.type!r}")
        if not isinstance(self.listen_port, int) or not 1 <= self.listen_port <= 65535:
            raise ValueError("forwarding rule listen_port must be 1-65535")


def forwarding_rule_to_dict(rule: ForwardingRule) -> dict:
    """Convert to the dict schema used by the existing formatter/parser."""
    d: dict = {
        "type": rule.type,
        "listen_addr": rule.listen_addr,
        "listen_port": rule.listen_port,
        "enabled": rule.enabled,
    }
    if rule.type == "local":
        d["remote_host"] = rule.remote_host
        d["remote_port"] = rule.remote_port
    elif rule.type == "remote":
        if rule.socks:
            d["socks"] = True
        else:
            d["local_host"] = rule.local_host
            d["local_port"] = rule.local_port
    return d


def forwarding_rule_from_dict(d: dict) -> ForwardingRule:
    """Convert from the dict schema used by the existing formatter/parser."""
    return ForwardingRule(
        type=str(d.get("type", "local")),
        listen_port=int(d.get("listen_port", 0) or 0),
        listen_addr=str(d.get("listen_addr", "") or ""),
        remote_host=str(d.get("remote_host", "") or ""),
        remote_port=int(d.get("remote_port", 0) or 0),
        local_host=str(d.get("local_host", "") or ""),
        local_port=int(d.get("local_port", 0) or 0),
        enabled=bool(d.get("enabled", True)),
        socks=bool(d.get("socks", False)),
    )


# -- Patch validation -------------------------------------------------------

EDITABLE_CONFIG_FIELDS = frozenset({
    "nickname", "hostname", "username", "port", "protocol",
    "aliases",
    "auth_method", "key_select_mode",
    "identity_files", "certificate_files",
    "identity_agent", "add_keys_to_agent",
    "pkcs11_provider", "security_key_provider",
    "pubkey_auth_no",
    "proxy_jump", "forward_agent",
    "forwarding_rules", "x11_forwarding",
    "pre_command", "local_command", "remote_command",
    "extra_ssh_config",
})

FORBIDDEN_IN_PATCH = frozenset({
    "password", "passphrase", "secret", "token",
    "credential", "cookie", "private_key",
    "__split_from_group", "__split_source", "__split_original_nickname",
    "__previous_secret_identity", "__meta", "__save_completion",
    "__secret_storage_done", "__host_tokens",
    "uuid", "id",
})


def validate_config_patch(patch: Mapping[str, Any]) -> None:
    """Reject unknown or forbidden fields in a config patch.

    Raises ``ValueError`` with a descriptive message on failure.
    """
    unknown = set(patch) - EDITABLE_CONFIG_FIELDS
    forbidden = set(patch) & FORBIDDEN_IN_PATCH
    if unknown:
        raise ValueError(f"Unknown config patch fields: {sorted(unknown)}")
    if forbidden:
        raise ValueError(f"Forbidden config patch fields: {sorted(forbidden)}")
    for key, value in patch.items():
        _validate_field_value(key, value)


def _validate_field_value(key: str, value: Any) -> None:
    """Type-check a single config-patch value."""
    if key in ("nickname", "hostname", "username", "protocol",
               "identity_agent", "add_keys_to_agent", "pkcs11_provider",
               "security_key_provider", "pre_command", "local_command",
               "remote_command", "extra_ssh_config"):
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
    elif key == "port":
        if not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError(f"{key} must be an integer 1-65535")
    elif key in ("auth_method", "key_select_mode"):
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    elif key in ("x11_forwarding", "pubkey_auth_no", "forward_agent"):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
    elif key in ("identity_files", "certificate_files", "proxy_jump", "aliases"):
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{key} must be a list")
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"each item in {key} must be a string")
    elif key == "forwarding_rules":
        if not isinstance(value, (list, tuple)):
            raise ValueError("forwarding_rules must be a list")
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("each forwarding rule must be a dict")
            rtype = item.get("type")
            if rtype not in ("local", "remote", "dynamic"):
                raise ValueError(f"forwarding rule type must be local/remote/dynamic, got {rtype!r}")
    elif key == "extra_ssh_config":
        if not isinstance(value, str):
            raise ValueError("extra_ssh_config must be a string")
        if len(value) > 65536:
            raise ValueError("extra_ssh_config exceeds 64KB limit")


# -- Enums and base types ---------------------------------------------------

class ConnectionHealth(str, Enum):
    """Persistent host availability, distinct from terminal session state."""

    UNKNOWN = "unknown"
    CHECKING = "checking"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class AuthenticationMethod(str, Enum):
    KEY = "key"
    PASSWORD = "password"


@dataclass(frozen=True)
class GroupReference:
    id: str
    name: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.id, "group id")


# -- Connection models ------------------------------------------------------

@dataclass(frozen=True)
class ConnectionSummary:
    """The deliberate, secret-free connection shape used by list views."""

    id: ConnectionId
    nickname: str
    host: str
    hostname: str
    username: str
    port: int
    protocol: str = "ssh"
    health: ConnectionHealth = ConnectionHealth.UNKNOWN
    groups: Tuple[GroupReference, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.id, "connection id")
        if not self.nickname.strip():
            raise ValueError("connection nickname must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("connection port must be between 1 and 65535")
        if not self.protocol.strip():
            raise ValueError("connection protocol must not be empty")

    @property
    def display_target(self) -> str:
        host = self.hostname or self.host or self.nickname
        return f"{self.username}@{host}" if self.username else host


@dataclass(frozen=True)
class ConnectionDetails(ConnectionSummary):
    """Full v1 connection response without secret values or sensitive paths."""

    aliases: Tuple[str, ...] = ()
    authentication_method: AuthenticationMethod = AuthenticationMethod.KEY
    identity_configured: bool = False
    certificate_configured: bool = False
    x11_forwarding: bool = False
    forwarding_rule_count: int = 0
    proxy_jump: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.forwarding_rule_count < 0:
            raise ValueError("forwarding rule count must not be negative")


# -- Editor capabilities and details ----------------------------------------

@dataclass(frozen=True)
class ConnectionEditorCapabilities:
    """Which connection-editor features the daemon currently supports.

    Advertised in the handshake response. The frontend enables/disables
    controls based on this.  During incremental rollout ``writable_fields``
    grows as each category lands.
    """

    writable_fields: FrozenSet[str] = field(default_factory=frozenset)
    supports_secrets: bool = False
    supports_metadata: bool = False
    supports_groups: bool = False
    supports_split: bool = False


@dataclass(frozen=True)
class ConnectionEditorDetails(ConnectionDetails):
    """Full editor state for local authenticated clients.

    Contains filesystem paths and complete configuration.  Not safe for
    untrusted consumers.  Gated behind ``CONNECTIONS_CONFIG_READ``.
    """

    # Authentication
    key_select_mode: int = 0
    identity_files: Tuple[str, ...] = ()
    certificate_files: Tuple[str, ...] = ()
    identity_agent: str = ""
    add_keys_to_agent: str = ""
    pkcs11_provider: str = ""
    security_key_provider: str = ""
    pubkey_auth_no: bool = False
    # Routing
    forward_agent: bool = False
    forward_agent_target: str = ""  # preserved, no widget
    proxy_command: str = ""  # preserved, no widget
    # Forwarding
    forwarding_rules: Tuple[ForwardingRule, ...] = ()
    # Commands
    pre_command: str = ""
    local_command: str = ""
    remote_command: str = ""
    request_tty: str = ""  # preserved, no widget
    # Advanced
    extra_ssh_config: str = ""
    identity_file_none: bool = False  # IdentityFile none suppression
    preferred_authentications: str = ""  # preserved for round-trip
    # Context
    source: str = ""  # config file owning this block
    generation: int = 0  # revision counter for stale detection


# -- Secret request / response models --------------------------------------

@dataclass(frozen=True)
class StoreConnectionPasswordRequest:
    """Store or update a login password for a connection."""

    connection_id: ConnectionId
    password: str
    previous_hostname: str = ""
    previous_host: str = ""
    previous_username: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class DeleteConnectionPasswordRequest:
    """Delete all stored login passwords for a connection."""

    connection_id: ConnectionId
    previous_hostname: str = ""
    previous_host: str = ""
    previous_username: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class StoreKeyPassphraseRequest:
    """Store or update a key passphrase."""

    key_path: str
    passphrase: str

    def __post_init__(self) -> None:
        if not self.key_path.strip():
            raise ValueError("key_path must not be empty")


@dataclass(frozen=True)
class LookupKeyPassphraseRequest:
    """Look up a stored key passphrase."""

    key_path: str

    def __post_init__(self) -> None:
        if not self.key_path.strip():
            raise ValueError("key_path must not be empty")


@dataclass(frozen=True)
class UpdateConnectionMetadataRequest:
    """Update non-SSH metadata (tags, WoL settings) for a connection."""

    connection_id: ConnectionId
    meta: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if not isinstance(self.meta, Mapping):
            raise TypeError("meta must be a mapping")


# -- Group request / response models ---------------------------------------

@dataclass(frozen=True)
class AssignConnectionToGroupRequest:
    """Move a connection to a group (or root if group_id is empty)."""

    connection_id: ConnectionId
    group_id: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class CreateGroupRequest:
    """Create a new group."""

    name: str
    parent_id: str = ""
    color: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("group name must not be empty")


@dataclass(frozen=True)
class DeleteGroupRequest:
    """Delete a group."""

    group_id: str

    def __post_init__(self) -> None:
        if not self.group_id.strip():
            raise ValueError("group_id must not be empty")


@dataclass(frozen=True)
class RenameGroupRequest:
    """Rename a group."""

    group_id: str
    new_name: str

    def __post_init__(self) -> None:
        if not self.group_id.strip():
            raise ValueError("group_id must not be empty")
        if not self.new_name.strip():
            raise ValueError("new_name must not be empty")


# -- Request / Response models ----------------------------------------------

@dataclass(frozen=True)
class CreateConnectionRequest:
    nickname: str
    hostname: str
    username: str = ""
    port: int = 22
    protocol: str = "ssh"
    config_patch: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.nickname) is not str or not self.nickname.strip():
            raise ValueError("connection nickname must not be empty")
        if type(self.hostname) is not str or not self.hostname.strip():
            raise ValueError("connection hostname must not be empty")
        if type(self.username) is not str:
            raise TypeError("connection username must be a string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("connection port must be between 1 and 65535")
        if type(self.protocol) is not str or not self.protocol.strip():
            raise ValueError("connection protocol must not be empty")
        if self.config_patch:
            normalized_patch = dict(self.config_patch)
            object.__setattr__(self, "config_patch", normalized_patch)
            validate_config_patch(self.config_patch)



@dataclass(frozen=True)
class UpdateConnectionRequest:
    """Patch a connection's configuration.

    Core identity fields use ``None`` = preserve (backward-compatible).
    Newer fields use ``UNSET`` = preserve, ``""`` = clear, value = apply.

    ``config_patch`` is a presence-aware dict: keys present in the dict
    are applied; absent keys are preserved.  The codec builds this from
    explicitly present keys in the incoming JSON — omission means preserve.
    """

    nickname: Union[str, None, _UNSET_TYPE] = UNSET
    hostname: Union[str, None, _UNSET_TYPE] = UNSET
    username: Union[str, None, _UNSET_TYPE] = UNSET
    port: Union[int, None, _UNSET_TYPE] = UNSET
    config_patch: Mapping[str, Any] = field(default_factory=dict)
    expected_generation: int = 0  # stale-editor detection

    def __post_init__(self) -> None:
        has_core = any(
            v is not None and v is not UNSET
            for v in (self.nickname, self.hostname, self.username, self.port)
        )
        has_patch = bool(self.config_patch)
        if not has_core and not has_patch:
            raise ValueError("connection update must contain at least one field")
        if self.nickname is not None and self.nickname is not UNSET and (
            type(self.nickname) is not str or not self.nickname.strip()
        ):
            raise ValueError("connection nickname must not be empty")
        if self.hostname is not None and self.hostname is not UNSET and (
            type(self.hostname) is not str or not self.hostname.strip()
        ):
            raise ValueError("connection hostname must not be empty")
        if self.username is not None and self.username is not UNSET and type(self.username) is not str:
            raise TypeError("connection username must be a string")
        if self.port is not None and self.port is not UNSET and (
            type(self.port) is not int or not 1 <= self.port <= 65535
        ):
            raise ValueError("connection port must be between 1 and 65535")
        if has_patch:
            normalized_patch = dict(self.config_patch)
            for k, v in list(normalized_patch.items()):
                if k in ("proxy_jump", "aliases") and isinstance(v, str):
                    normalized_patch[k] = [h.strip() for h in re.split(r'[\s,]+', v) if h.strip()]
                elif k in ("identity_files", "certificate_files") and isinstance(v, str):
                    normalized_patch[k] = [f.strip() for f in re.split(r'[\r\n,]+', v) if f.strip()]
            object.__setattr__(self, "config_patch", normalized_patch)
            validate_config_patch(self.config_patch)



@dataclass(frozen=True)
class DeleteConnectionRequest:
    connection_id: ConnectionId

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class DeleteConnectionResult:
    connection_id: ConnectionId
    deleted: bool

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if type(self.deleted) is not bool:
            raise TypeError("connection deleted result must be a boolean")


@dataclass(frozen=True)
class ConnectionValidationError:
    field: str
    code: str
    message: str


@dataclass(frozen=True)
class ConnectionValidationResult:
    valid: bool
    errors: Tuple[ConnectionValidationError, ...] = ()

    def __post_init__(self) -> None:
        if self.valid and self.errors:
            raise ValueError("a valid result cannot contain validation errors")
