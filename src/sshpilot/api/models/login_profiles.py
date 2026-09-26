"""Frontend-neutral login profile models.

A login profile is a named, reusable authentication bundle (username, key
selection, agent/hardware options, extra directives) owned by the daemon. It
is linked to SSH connections either explicitly or through a group (the
connection *inherits* its primary group's profile). The daemon resolves the
effective profile into each linked connection's ``Host`` block.

No secret values ever appear in these models: profiles expose only
``has_password`` / ``has_sudo_password`` flags, and
:class:`SetLoginProfileSecretRequest` carries the secret out of band through
the protected secret-frame transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import NewType, Optional, Tuple

from .common import ConnectionId, require_identifier

LoginProfileId = NewType("LoginProfileId", str)


class LoginProfileLinkMode(str, Enum):
    """How a connection is linked to a profile."""

    EXPLICIT = "explicit"
    INHERIT = "inherit"


class LoginProfileSource(str, Enum):
    """Where a connection's effective profile comes from."""

    CONNECTION = "connection"
    GROUP = "group"


class LoginProfileSecretKind(str, Enum):
    LOGIN = "login"
    SUDO = "sudo"


class LoginProfileDetachReason(str, Enum):
    DRIFT = "drift"
    PROFILE_MISSING = "profile_missing"
    PROFILE_DELETED = "profile_deleted"


_AUTH_METHODS = (0, 1)
_KEY_SELECT_MODES = (0, 1, 2)
_SETTINGS_TEXT = (
    "username",
    "identity_agent",
    "add_keys_to_agent",
    "pkcs11_provider",
    "security_key_provider",
    "forward_agent_target",
)


def _check_text(value: object, name: str, *, multiline: bool = False) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    if not multiline and ("\n" in value or "\r" in value):
        raise ValueError(f"{name} must be a single line")


def _check_str_tuple(value: object, name: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{name} must be a tuple")
    for item in value:
        _check_text(item, name)
        if not item.strip():
            raise ValueError(f"{name} entries must be non-empty")


def _check_bool(value: object, name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a boolean")


def _check_count(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class LoginProfileSettings:
    """The editable fields of a login profile (a complete form)."""

    name: str
    username: str = ""
    auth_method: int = 0  # 0 = key, 1 = password
    key_select_mode: int = 0  # 0 = automatic, 1 = specific keys only, 2 = specific keys + agent
    identity_files: Tuple[str, ...] = ()
    certificate_files: Tuple[str, ...] = ()
    identity_agent: str = ""
    add_keys_to_agent: str = ""
    pkcs11_provider: str = ""
    security_key_provider: str = ""
    pubkey_auth_no: bool = False
    forward_agent: bool = False
    forward_agent_target: str = ""
    extra_ssh_config: str = ""

    def __post_init__(self) -> None:
        _check_text(self.name, "name")
        if not self.name.strip():
            raise ValueError("name must be a non-empty string")
        for name in _SETTINGS_TEXT:
            _check_text(getattr(self, name), name)
        _check_text(self.extra_ssh_config, "extra_ssh_config", multiline=True)
        if type(self.auth_method) is not int or self.auth_method not in _AUTH_METHODS:
            raise ValueError("auth_method must be 0 or 1")
        if type(self.key_select_mode) is not int or self.key_select_mode not in _KEY_SELECT_MODES:
            raise ValueError("key_select_mode must be 0, 1 or 2")
        _check_str_tuple(self.identity_files, "identity_files")
        _check_str_tuple(self.certificate_files, "certificate_files")
        _check_bool(self.pubkey_auth_no, "pubkey_auth_no")
        _check_bool(self.forward_agent, "forward_agent")


@dataclass(frozen=True)
class LoginProfileSummary:
    """One stored profile, with safe secret flags and usage counts."""

    id: LoginProfileId
    settings: LoginProfileSettings
    revision: int
    has_password: bool = False
    has_sudo_password: bool = False
    connection_count: int = 0  # explicitly linked + inheriting connections
    group_count: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.id, "login profile id")
        if type(self.settings) is not LoginProfileSettings:
            raise TypeError("settings must be LoginProfileSettings")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        _check_bool(self.has_password, "has_password")
        _check_bool(self.has_sudo_password, "has_sudo_password")
        _check_count(self.connection_count, "connection_count")
        _check_count(self.group_count, "group_count")

    @property
    def name(self) -> str:
        return self.settings.name


@dataclass(frozen=True)
class ConnectionProfileLink:
    """One connection's link and its resolved effective profile."""

    connection_id: ConnectionId
    mode: LoginProfileLinkMode
    profile_id: Optional[LoginProfileId] = None  # explicit links only
    effective_profile_id: Optional[LoginProfileId] = None
    source: Optional[LoginProfileSource] = None
    group_id: Optional[str] = None  # the group an inherited profile comes from

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if type(self.mode) is not LoginProfileLinkMode:
            raise TypeError("mode must be a LoginProfileLinkMode")
        if (self.mode is LoginProfileLinkMode.EXPLICIT) != (self.profile_id is not None):
            raise ValueError("profile_id is required exactly for explicit links")
        for name in ("profile_id", "effective_profile_id", "group_id"):
            value = getattr(self, name)
            if value is not None:
                require_identifier(value, name)
        if self.source is not None and type(self.source) is not LoginProfileSource:
            raise TypeError("source must be a LoginProfileSource")
        if (self.effective_profile_id is None) != (self.source is None):
            raise ValueError("effective_profile_id and source go together")


@dataclass(frozen=True)
class GroupProfileLink:
    group_id: str
    profile_id: LoginProfileId

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group id")
        require_identifier(self.profile_id, "login profile id")


@dataclass(frozen=True)
class LoginProfileSnapshot:
    """All profiles plus the link state of the active SSH configuration."""

    available: bool
    profiles: Tuple[LoginProfileSummary, ...] = ()
    group_links: Tuple[GroupProfileLink, ...] = ()
    connection_links: Tuple[ConnectionProfileLink, ...] = ()

    def __post_init__(self) -> None:
        _check_bool(self.available, "available")
        for name, expected in (
            ("profiles", LoginProfileSummary),
            ("group_links", GroupProfileLink),
            ("connection_links", ConnectionProfileLink),
        ):
            value = getattr(self, name)
            if type(value) is not tuple:
                raise TypeError(f"{name} must be a tuple")
            for item in value:
                if type(item) is not expected:
                    raise TypeError(f"{name} contains a member of the wrong type")
        ids = [p.id for p in self.profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("profiles must not contain duplicate ids")

    def profile(self, profile_id: Optional[str]) -> Optional[LoginProfileSummary]:
        for item in self.profiles:
            if item.id == profile_id:
                return item
        return None

    def link_for(self, connection_id: str) -> Optional[ConnectionProfileLink]:
        for item in self.connection_links:
            if item.connection_id == connection_id:
                return item
        return None

    def group_profile_id(self, group_id: Optional[str]) -> Optional[LoginProfileId]:
        for item in self.group_links:
            if item.group_id == group_id:
                return item.profile_id
        return None


@dataclass(frozen=True)
class CreateLoginProfileRequest:
    settings: LoginProfileSettings

    def __post_init__(self) -> None:
        if type(self.settings) is not LoginProfileSettings:
            raise TypeError("settings must be LoginProfileSettings")


@dataclass(frozen=True)
class UpdateLoginProfileRequest:
    """Replace a profile's settings; linked Host blocks are re-rendered."""

    profile_id: LoginProfileId
    settings: LoginProfileSettings
    expected_revision: Optional[int] = None

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "login profile id")
        if type(self.settings) is not LoginProfileSettings:
            raise TypeError("settings must be LoginProfileSettings")
        if self.expected_revision is not None and (
            type(self.expected_revision) is not int or self.expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")


@dataclass(frozen=True)
class LoginProfileReassignment:
    """Target for one affected item when deleting a profile.

    ``key`` is a connection id or ``group:<group id>``; ``target_profile_id``
    ``None`` detaches the item (it keeps its current values).
    """

    key: str
    target_profile_id: Optional[LoginProfileId] = None

    def __post_init__(self) -> None:
        require_identifier(self.key, "reassignment key")
        if self.target_profile_id is not None:
            require_identifier(self.target_profile_id, "target login profile id")


@dataclass(frozen=True)
class DeleteLoginProfileRequest:
    profile_id: LoginProfileId
    replacement_profile_id: Optional[LoginProfileId] = None
    overrides: Tuple[LoginProfileReassignment, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "login profile id")
        if self.replacement_profile_id is not None:
            require_identifier(self.replacement_profile_id, "replacement login profile id")
        if type(self.overrides) is not tuple:
            raise TypeError("overrides must be a tuple")
        keys = []
        for item in self.overrides:
            if type(item) is not LoginProfileReassignment:
                raise TypeError("overrides must contain LoginProfileReassignment")
            keys.append(item.key)
        if len(set(keys)) != len(keys):
            raise ValueError("overrides must not repeat a key")


@dataclass(frozen=True)
class DetachedConnectionInfo:
    connection_id: ConnectionId
    profile_name: str
    reason: LoginProfileDetachReason

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        _check_text(self.profile_name, "profile_name")
        if type(self.reason) is not LoginProfileDetachReason:
            raise TypeError("reason must be a LoginProfileDetachReason")


@dataclass(frozen=True)
class DeleteLoginProfileResult:
    detached: Tuple[DetachedConnectionInfo, ...] = ()

    def __post_init__(self) -> None:
        if type(self.detached) is not tuple or any(
            type(item) is not DetachedConnectionInfo for item in self.detached
        ):
            raise TypeError("detached must be a tuple of DetachedConnectionInfo")


def _check_connection_ids(value: object) -> None:
    if type(value) is not tuple or not value:
        raise ValueError("connection_ids must be a non-empty tuple")
    for item in value:
        require_identifier(item, "connection id")
    if len(set(value)) != len(value):
        raise ValueError("connection_ids must not contain duplicates")


def _check_link_target(mode: object, profile_id: object) -> None:
    if mode is not None and type(mode) is not LoginProfileLinkMode:
        raise TypeError("mode must be a LoginProfileLinkMode or None")
    if (mode is LoginProfileLinkMode.EXPLICIT) != (profile_id is not None):
        raise ValueError("profile_id is required exactly for explicit links")
    if profile_id is not None:
        require_identifier(profile_id, "login profile id")


@dataclass(frozen=True)
class AssignLoginProfileRequest:
    """Link connections explicitly, to their group (inherit), or unlink (``mode=None``)."""

    connection_ids: Tuple[ConnectionId, ...]
    mode: Optional[LoginProfileLinkMode] = None
    profile_id: Optional[LoginProfileId] = None

    def __post_init__(self) -> None:
        _check_connection_ids(self.connection_ids)
        _check_link_target(self.mode, self.profile_id)


@dataclass(frozen=True)
class PreviewLoginProfileAssignmentRequest:
    connection_ids: Tuple[ConnectionId, ...]
    mode: Optional[LoginProfileLinkMode] = None
    profile_id: Optional[LoginProfileId] = None

    def __post_init__(self) -> None:
        _check_connection_ids(self.connection_ids)
        _check_link_target(self.mode, self.profile_id)


@dataclass(frozen=True)
class LoginProfileFieldChange:
    """One Host-block setting an assignment would change (display strings)."""

    field: str
    label: str
    before: str
    after: str

    def __post_init__(self) -> None:
        for name in ("field", "label"):
            _check_text(getattr(self, name), name)
        for name in ("before", "after"):
            _check_text(getattr(self, name), name, multiline=True)


@dataclass(frozen=True)
class LoginProfileAssignmentPreview:
    connection_id: ConnectionId
    profile_name: str = ""  # empty when the target resolves to no profile
    changes: Tuple[LoginProfileFieldChange, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        _check_text(self.profile_name, "profile_name")
        if type(self.changes) is not tuple or any(
            type(item) is not LoginProfileFieldChange for item in self.changes
        ):
            raise TypeError("changes must be a tuple of LoginProfileFieldChange")


@dataclass(frozen=True)
class SetGroupLoginProfileRequest:
    """Assign or clear (``profile_id=None``) a group's profile.

    ``link_members`` are switched to inherit the group's profile.
    """

    group_id: str
    profile_id: Optional[LoginProfileId] = None
    link_members: Tuple[ConnectionId, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.group_id, "group id")
        if self.profile_id is not None:
            require_identifier(self.profile_id, "login profile id")
        if type(self.link_members) is not tuple:
            raise TypeError("link_members must be a tuple")
        for item in self.link_members:
            require_identifier(item, "connection id")


@dataclass(frozen=True)
class SetLoginProfileSecretRequest:
    """Store or clear one profile secret.

    The secret value is never part of this model: clients send it through the
    protected secret-frame transport. ``clear=True`` deletes the secret and
    takes no secret input.
    """

    profile_id: LoginProfileId
    kind: LoginProfileSecretKind = LoginProfileSecretKind.LOGIN
    clear: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "login profile id")
        if type(self.kind) is not LoginProfileSecretKind:
            raise TypeError("kind must be a LoginProfileSecretKind")
        _check_bool(self.clear, "clear")


@dataclass(frozen=True)
class LoginProfilesChangedEvent:
    """Profiles or links changed; ``detached`` lists links dropped by the daemon."""

    detached: Tuple[DetachedConnectionInfo, ...] = field(default=())

    def __post_init__(self) -> None:
        if type(self.detached) is not tuple or any(
            type(item) is not DetachedConnectionInfo for item in self.detached
        ):
            raise TypeError("detached must be a tuple of DetachedConnectionInfo")
