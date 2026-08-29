"""Frontend-neutral connection request and response models."""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple, Union

from .common import ConnectionId, SessionId, require_identifier, validate_ssh_host_alias
from .connection_store import validate_safe_metadata

MAX_DISPLAY_NAME_LENGTH = 512


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


def forwarding_rule_to_dict(rule: Union['ForwardingRule', dict]) -> dict:
    """Convert to the dict schema used by the existing formatter/parser.
    Also accepts a dict and returns a validated copy.
    """
    if isinstance(rule, dict):
        d = dict(rule)
        rtype = d.get('type')
        if rtype not in ('local', 'remote', 'dynamic'):
            raise ValueError(f"forwarding rule type must be local/remote/dynamic, got {rtype!r}")
        try:
            d['listen_port'] = int(d.get('listen_port', 0) or 0)
        except (TypeError, ValueError):
            d['listen_port'] = 0
        if rtype == 'local':
            try:
                d['remote_port'] = int(d.get('remote_port', 0) or 0)
            except (TypeError, ValueError):
                d['remote_port'] = 0
        elif rtype == 'remote' and not d.get('socks'):
            try:
                d['local_port'] = int(d.get('local_port', 0) or 0)
            except (TypeError, ValueError):
                d['local_port'] = 0
        return d

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
    "forward_agent_explicit_no", "forward_agent_target",
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
    elif key in (
        "x11_forwarding", "pubkey_auth_no", "forward_agent",
        "forward_agent_explicit_no",
    ):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
    elif key == "forward_agent_target":
        if type(value) is not str:
            raise ValueError("forward_agent_target must be a string")
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
    display_name: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.id, "connection id")
        if not self.nickname.strip():
            raise ValueError("connection nickname must not be empty")
        if type(self.display_name) is not str:
            raise TypeError("connection display name must be a string")
        display_name = self.display_name.strip() or self.nickname
        if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            raise ValueError("connection display name is too long")
        object.__setattr__(self, "display_name", display_name)
        if not 1 <= self.port <= 65535:
            raise ValueError("connection port must be between 1 and 65535")
        if not self.protocol.strip():
            raise ValueError("connection protocol must not be empty")

    @property
    def display_target(self) -> str:
        host = self.hostname or self.host or self.nickname
        return f"{self.username}@{host}" if self.username else host


# Core identity columns persisted alongside plugin FieldSpec values. Excluded
# from ``plugin_data`` projections so editors don't duplicate them.
CONNECTION_CORE_DATA_FIELDS = frozenset(
    {
        "nickname",
        "hostname",
        "host",
        "username",
        "port",
        "protocol",
        "id",
        "uuid",
        "display_name",
        "generation",
        "order",
        "aliases",
        "source",
        "authored_directives",
    }
)
_PLUGIN_DATA_SENSITIVE_PARTS = (
    "password",
    "passphrase",
    "secret",
    "token",
    "credential",
    "private_key",
)


def extract_plugin_data(
    protocol: str, data: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Return secret-free protocol FieldSpec values from a connection record."""
    if (protocol or "ssh") == "ssh":
        return {}
    result: Dict[str, Any] = {}
    for key, value in dict(data or {}).items():
        if key in CONNECTION_CORE_DATA_FIELDS or key.startswith("__"):
            continue
        lowered = key.lower()
        if any(part in lowered for part in _PLUGIN_DATA_SENSITIVE_PARTS):
            continue
        result[key] = value
    return result


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
    # Non-SSH protocol FieldSpec values (device, container, pod, …). Empty for SSH.
    plugin_data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.forwarding_rule_count < 0:
            raise ValueError("forwarding rule count must not be negative")
        if type(self.plugin_data) is not dict:
            object.__setattr__(self, "plugin_data", dict(self.plugin_data))


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
class ConnectionMutationResult:
    """The authoritative result of a successful connection save."""

    connection_id: str
    nickname: str
    generation: int
    changed: bool = True
    changed_fields: Tuple[str, ...] = ()
    display_name: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if not self.nickname.strip():
            raise ValueError("connection nickname must not be empty")
        if type(self.display_name) is not str:
            raise TypeError("connection display name must be a string")
        display_name = self.display_name.strip() or self.nickname
        if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            raise ValueError("connection display name is too long")
        object.__setattr__(self, "display_name", display_name)
        if self.generation < 0:
            raise ValueError("generation must not be negative")
        if type(self.changed) is not bool:
            raise TypeError("changed must be a boolean")
        if type(self.changed_fields) is not tuple:
            raise TypeError("changed fields must be a tuple")
        if any(type(field) is not str or not field.strip() for field in self.changed_fields):
            raise ValueError("changed fields must contain non-empty strings")
        if not self.changed and self.changed_fields:
            raise ValueError("unchanged result must not contain changed fields")


@dataclass(frozen=True)
class EffectiveConfigComparison:
    """Daemon-owned authored-vs-effective OpenSSH comparison."""

    connection_id: str
    host: str
    available: bool
    has_diff: bool = False
    changes: Tuple[Dict[str, Any], ...] = ()
    own: Tuple[str, ...] = ()
    full: Tuple[str, ...] = ()
    generation: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if type(self.host) is not str or "\x00" in self.host:
            raise ValueError("effective config host is invalid")
        if type(self.available) is not bool or type(self.has_diff) is not bool:
            raise TypeError("effective config flags must be booleans")
        if type(self.changes) is not tuple:
            raise TypeError("effective config changes must be a tuple")
        if type(self.own) is not tuple or type(self.full) is not tuple:
            raise TypeError("effective config lines must be tuples")
        if any(type(line) is not str or "\x00" in line for line in (*self.own, *self.full)):
            raise ValueError("effective config lines are invalid")
        if self.generation < 0:
            raise ValueError("effective config generation must not be negative")


@dataclass(frozen=True)
class UnsavedHostCheckRequest:
    """Semantic destination facts for daemon-owned save-prompt detection."""

    hostname: str
    username: str = ""
    connection_id: Optional[str] = None
    # ``None`` preserves the distinction between an omitted CLI option and an
    # explicit ``-p 22``.  The daemon must not turn an omitted port into an
    # OpenSSH override before resolving Host/Include/Match rules.
    port: Optional[int] = None
    protocol: str = "ssh"
    proxy_jump: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.hostname) is not str or not self.hostname.strip() or "\x00" in self.hostname:
            raise ValueError("hostname must be non-empty")
        if type(self.username) is not str or "\x00" in self.username:
            raise ValueError("username is invalid")
        if self.port is not None and (type(self.port) is not int or not 1 <= self.port <= 65535):
            raise ValueError("port must be between 1 and 65535")
        if type(self.protocol) is not str or not self.protocol.strip():
            raise ValueError("protocol must not be empty")
        if type(self.proxy_jump) is not tuple:
            raise TypeError("proxy_jump must be a tuple")
        if any(type(item) is not str or not item.strip() or "\x00" in item for item in self.proxy_jump):
            raise ValueError("proxy_jump contains an invalid destination")
        if self.connection_id is not None:
            require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class UnsavedHostCheckResult:
    """Authoritative result for whether a destination is already saved."""

    saved: bool
    hostname: str
    username: str
    generation: int

    def __post_init__(self) -> None:
        if type(self.saved) is not bool:
            raise TypeError("saved must be a boolean")
        if type(self.hostname) is not str or type(self.username) is not str:
            raise TypeError("destination identity must be text")
        if self.generation < 0:
            raise ValueError("generation must not be negative")


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
    forward_agent_explicit_no: bool = False
    forward_agent_target: str = ""  # socket path / $ENV; empty when yes/no
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
    x11_forwarding_explicit_no: bool = False
    identities_only_explicit_no: bool = False
    preferred_authentications: str = ""  # preserved for round-trip
    # Context
    source: str = ""  # config file owning this block
    generation: int = 0  # revision counter for stale detection
    # Lowercased directives this Host block authored. Everything else the
    # editor shows is inherited — OpenSSH resolves it from a global block or
    # its own defaults — so the editor must not present those values as if the
    # user had set them. Empty means "no evidence" (a record that did not come
    # from a parsed Host block), never "authored nothing".
    authored_directives: Tuple[str, ...] = ()


# -- Secret request / response models --------------------------------------

@dataclass(frozen=True)
class StoreConnectionPasswordRequest:
    """Store or update a login password for a connection."""

    connection_id: ConnectionId
    password: str = field(repr=False)
    previous_hostname: str = ""
    previous_host: str = ""
    previous_username: str = ""

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")


@dataclass(frozen=True)
class SetSessionConnectionPasswordRequest:
    """Identify a daemon-memory-only password supplied in a protected frame."""

    connection_id: ConnectionId

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
    """Store a key passphrase supplied through a protected interaction."""

    key_path: str
    interaction_scope_id: SessionId

    def __post_init__(self) -> None:
        if not self.key_path.strip():
            raise ValueError("key_path must not be empty")
        require_identifier(
            self.interaction_scope_id,
            "key interaction scope id",
        )
        if not str(self.interaction_scope_id).startswith("key-operation-"):
            raise ValueError(
                "key interaction scope id must start with 'key-operation-'"
            )


@dataclass(frozen=True)
class LookupKeyPassphraseRequest:
    """Look up a stored key passphrase."""

    key_path: str

    def __post_init__(self) -> None:
        require_identifier(self.key_path, "key path")


@dataclass(frozen=True)
class DeleteKeyPassphraseRequest:
    """Request to delete an SSH key passphrase."""

    key_path: str

    def __post_init__(self) -> None:
        require_identifier(self.key_path, "key path")


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
    display_name: str = ""
    config_patch: Mapping[str, Any] = field(default_factory=dict)
    plugin_data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_ssh_host_alias(self.nickname)
        if type(self.hostname) is not str:
            raise TypeError("connection hostname must be a string")
        if type(self.username) is not str:
            raise TypeError("connection username must be a string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("connection port must be between 1 and 65535")
        if type(self.protocol) is not str or not self.protocol.strip():
            raise ValueError("connection protocol must not be empty")
        if type(self.display_name) is not str:
            raise TypeError("connection display name must be a string")
        if self.display_name and (
            not self.display_name.strip()
            or len(self.display_name) > MAX_DISPLAY_NAME_LENGTH
        ):
            raise ValueError("connection display name is invalid")
        if self.config_patch:
            normalized_patch = dict(self.config_patch)
            object.__setattr__(self, "config_patch", normalized_patch)
            validate_config_patch(self.config_patch)
        if type(self.plugin_data) is not dict:
            object.__setattr__(self, "plugin_data", dict(self.plugin_data))



@dataclass(frozen=True)
class UpdateConnectionRequest:
    """Patch a connection's configuration.

    Core identity fields use ``None`` = preserve (backward-compatible).
    Newer fields use ``UNSET`` = preserve, ``""`` = clear, value = apply.

    ``config_patch`` is a presence-aware dict: keys present in the dict
    are applied; absent keys are preserved.  The codec builds this from
    explicitly present keys in the incoming JSON — omission means preserve.

    ``expected_generation`` guards stale editors: ``None`` means no
    generation supplied (the check is skipped); zero is a legitimate
    first-generation value and is enforced like any other.
    """

    nickname: Union[str, None, _UNSET_TYPE] = UNSET
    hostname: Union[str, None, _UNSET_TYPE] = UNSET
    username: Union[str, None, _UNSET_TYPE] = UNSET
    port: Union[int, str, None, _UNSET_TYPE] = UNSET  # "" clears (inherit)
    display_name: Union[str, None, _UNSET_TYPE] = UNSET
    config_patch: Mapping[str, Any] = field(default_factory=dict)
    plugin_data: Mapping[str, Any] = field(default_factory=dict)
    expected_generation: Optional[int] = None  # stale-editor detection

    def __post_init__(self) -> None:
        has_core = any(
            v is not None and v is not UNSET
            for v in (self.nickname, self.hostname, self.username, self.port, self.display_name)
        )
        has_patch = bool(self.config_patch) or bool(self.plugin_data)
        if not has_core and not has_patch:
            raise ValueError("connection update must contain at least one field")
        if self.nickname is not None and self.nickname is not UNSET and (
            type(self.nickname) is not str or not self.nickname.strip()
        ):
            raise ValueError("connection nickname must not be empty")
        if self.nickname is not None and self.nickname is not UNSET:
            validate_ssh_host_alias(self.nickname)
        if self.hostname is not None and self.hostname is not UNSET and type(self.hostname) is not str:
            raise TypeError("connection hostname must be a string")
        if self.username is not None and self.username is not UNSET and type(self.username) is not str:
            raise TypeError("connection username must be a string")
        # ``""`` clears an authored Port so the host inherits again, matching
        # how an emptied username clears ``User``. Any other string is invalid.
        if self.port is not None and self.port is not UNSET:
            if type(self.port) is str:
                if self.port.strip():
                    raise ValueError("connection port must be an integer or empty")
            elif type(self.port) is not int or not 1 <= self.port <= 65535:
                raise ValueError("connection port must be between 1 and 65535")
        if self.display_name is not None and self.display_name is not UNSET:
            if type(self.display_name) is not str or not self.display_name.strip():
                raise ValueError("connection display name must not be empty")
            if len(self.display_name) > MAX_DISPLAY_NAME_LENGTH:
                raise ValueError("connection display name is too long")
        if has_patch:
            normalized_patch = dict(self.config_patch)
            for k, v in list(normalized_patch.items()):
                if k in ("proxy_jump", "aliases") and isinstance(v, str):
                    normalized_patch[k] = [h.strip() for h in re.split(r'[\s,]+', v) if h.strip()]
                elif k in ("identity_files", "certificate_files") and isinstance(v, str):
                    normalized_patch[k] = [f.strip() for f in re.split(r'[\r\n,]+', v) if f.strip()]
            object.__setattr__(self, "config_patch", normalized_patch)
            validate_config_patch(self.config_patch)
        if type(self.plugin_data) is not dict:
            object.__setattr__(self, "plugin_data", dict(self.plugin_data))



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
class SplitConnectionRequest:
    """Split a connection out of a multi-host SSH config block.

    Removes ``original_host_token`` from the block identified by
    ``source_config_path`` and appends a new standalone ``Host`` block
    built from ``config_patch``.  ``expected_generation`` is reserved
    for stale-editor detection: ``None`` means no generation supplied
    (the check is skipped); zero is a legitimate generation and is
    enforced like any other value.
    """

    connection_id: ConnectionId
    original_host_token: str
    source_config_path: str
    nickname: str
    hostname: Optional[str] = None
    username: Optional[str] = None
    port: Optional[int] = None
    config_patch: Mapping[str, Any] = field(default_factory=dict)
    expected_generation: Optional[int] = None

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if not self.original_host_token or not self.original_host_token.strip():
            raise ValueError("original_host_token must be a non-empty string")
        if not self.source_config_path or not self.source_config_path.strip():
            raise ValueError("source_config_path must be a non-empty string")
        if not isinstance(self.config_patch, Mapping):
            raise TypeError("config_patch must be a mapping")


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

@dataclass(frozen=True)
class StorePluginSecretRequest:
    plugin_id: str
    key: str
    value: str = field(repr=False)

@dataclass(frozen=True)
class GetPluginSecretRequest:
    plugin_id: str
    key: str

@dataclass(frozen=True)
class DeletePluginSecretRequest:
    plugin_id: str
    key: str

def store_plugin_secret_request_to_wire(request: StorePluginSecretRequest) -> dict:
    return {"plugin_id": request.plugin_id, "key": request.key, "value": request.value}

def store_plugin_secret_request_from_wire(payload: dict) -> StorePluginSecretRequest:
    return StorePluginSecretRequest(
        plugin_id=payload["plugin_id"],
        key=payload["key"],
        value=payload["value"],
    )

def get_plugin_secret_request_to_wire(request: GetPluginSecretRequest) -> dict:
    return {"plugin_id": request.plugin_id, "key": request.key}

def get_plugin_secret_request_from_wire(payload: dict) -> GetPluginSecretRequest:
    return GetPluginSecretRequest(
        plugin_id=payload["plugin_id"],
        key=payload["key"],
    )

def delete_plugin_secret_request_to_wire(request: DeletePluginSecretRequest) -> dict:
    return {"plugin_id": request.plugin_id, "key": request.key}

def delete_plugin_secret_request_from_wire(payload: dict) -> DeletePluginSecretRequest:
    return DeletePluginSecretRequest(
        plugin_id=payload["plugin_id"],
        key=payload["key"],
    )


# -- Raw SSH config text (daemon-resolved editor document) -------------------

@dataclass(frozen=True)
class SshConfigText:
    """The daemon-selected active SSH config text plus editor metadata.

    The daemon resolves which file is active (normal or isolated mode) and
    never accepts a filesystem path from the client. ``display_name`` is the
    daemon-computed display label (home-collapsed); ``writable`` reflects
    whether the daemon's hardened atomic write path can replace the file.
    """

    text: str = field(repr=False)
    revision: str
    display_name: str
    writable: bool

    def __post_init__(self) -> None:
        if type(self.text) is not str or "\x00" in self.text:
            raise ValueError("SSH config text must be safe text")
        require_identifier(self.revision, "SSH config revision")
        if type(self.display_name) is not str or not self.display_name.strip():
            raise ValueError("SSH config display name must be a non-empty string")
        if type(self.writable) is not bool:
            raise TypeError("SSH config writable must be a boolean")


@dataclass(frozen=True)
class SaveSshConfigTextRequest:
    """Optimistic raw-text replacement of the daemon-selected SSH config.

    ``expected_revision`` is the revision returned by the preceding load;
    the daemon rejects the save when any participating config file changed
    since the editor loaded it.
    """

    text: str = field(repr=False)
    expected_revision: str

    def __post_init__(self) -> None:
        if type(self.text) is not str or "\x00" in self.text:
            raise ValueError("SSH config text must be safe text")
        require_identifier(self.expected_revision, "expected SSH config revision")


# -- UpdateConnectionMetadataRequest hardening ------------------------------

@dataclass(frozen=True)
class UpdateConnectionMetadataRequest:
    """Update non-SSH metadata (tags, WoL settings) for a connection."""

    connection_id: ConnectionId
    meta: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        object.__setattr__(self, "meta", validate_safe_metadata(self.meta))
