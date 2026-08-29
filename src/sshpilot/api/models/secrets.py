"""Immutable API models for daemon-owned secret-backend management.

These are **metadata and management** DTOs.  They never carry secret values:
no passwords, master passwords, session tokens, two-factor codes, API client
secrets, auth-challenge secrets, transformed keys, or decrypted data.  A
backend's locked/unlocked state is reported as a boolean, never as a token.

The canonical field model (field→config-key mapping, valid ranges, canonical
defaults, strict normalization, revision) is owned by
``sshpilot.core.secrets.management`` so the API values, the persisted
``secrets.*`` config keys, and the revision token always agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from sshpilot.core.secrets.management import (
    EDITABLE_FIELDS,
    INTEGER_RANGES,
    VALID_BACKEND_VALUES,
)

# Re-exported field-model contracts (consumed by the daemon service).
EDITABLE_FIELDS = EDITABLE_FIELDS
_VALID_BACKEND_VALUES = VALID_BACKEND_VALUES


# ---------------------------------------------------------------------------
# Error codes narrowly scoped to this module.
# ---------------------------------------------------------------------------

REVISION_CONFLICT = "revision_conflict"
SETTINGS_MALFORMED = "settings_malformed"
SETTINGS_PERSISTENCE_FAILED = "settings_persistence_failed"
BACKEND_UNAVAILABLE = "backend_unavailable"
INTERACTION_REQUIRED = "interaction_required"
INTERACTION_CANCELLED = "interaction_cancelled"
LOGIN_REQUIRED = "login_required"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_boolean(value: Any, field: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean, got {type(value).__name__}")


def _validate_integer(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer, got {type(value).__name__}")
    lo, hi = INTEGER_RANGES[field]
    if not (lo <= value <= hi):
        raise ValueError(f"{field} must be between {lo} and {hi}, got {value}")


def _validate_text(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, got {type(value).__name__}")


def _validate_patch_fields(patch: Mapping[str, Any]) -> None:
    """Reject unknown patch fields and type-check values."""
    unknown = set(patch.keys()) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"unknown patch fields: {sorted(unknown)}")
    for key, value in patch.items():
        if key == "remember_in_keyring":
            _validate_boolean(value, key)
        elif key in INTEGER_RANGES:
            _validate_integer(value, key)
        elif key == "backend":
            if not isinstance(value, str) or value not in _VALID_BACKEND_VALUES:
                raise ValueError(
                    f"backend must be one of {sorted(_VALID_BACKEND_VALUES)}, "
                    f"got {value!r}"
                )
        else:
            _validate_text(value, key)


# ---------------------------------------------------------------------------
# Public DTOs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SecretConfiguration:
    """Immutable snapshot of daemon-owned ``secrets.*`` configuration.

    Paths and profile names only — never secret values.
    """

    revision: str
    backend: str
    session_timeout: int
    remember_in_keyring: bool
    bitwarden_profile: str
    bitwarden_server: str
    keepassxc_database: str
    keepassxc_keyfile: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "revision": self.revision,
            "backend": self.backend,
            "session_timeout": self.session_timeout,
            "remember_in_keyring": self.remember_in_keyring,
            "bitwarden_profile": self.bitwarden_profile,
            "bitwarden_server": self.bitwarden_server,
            "keepassxc_database": self.keepassxc_database,
            "keepassxc_keyfile": self.keepassxc_keyfile,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("revision must be a non-empty string")
        _validate_integer(self.session_timeout, "session_timeout")
        _validate_boolean(self.remember_in_keyring, "remember_in_keyring")
        if not isinstance(self.backend, str) or self.backend not in _VALID_BACKEND_VALUES:
            raise ValueError(
                f"backend must be one of {sorted(_VALID_BACKEND_VALUES)}, "
                f"got {self.backend!r}"
            )
        _validate_text(self.bitwarden_profile, "bitwarden_profile")
        _validate_text(self.bitwarden_server, "bitwarden_server")
        _validate_text(self.keepassxc_database, "keepassxc_database")
        _validate_text(self.keepassxc_keyfile, "keepassxc_keyfile")


@dataclass(frozen=True)
class UpdateSecretConfigurationRequest:
    """A partial update to daemon-owned secret configuration.

    ``patch`` contains only the fields to change.  ``expected_revision`` enables
    optimistic concurrency control: the service rejects the update if the
    current revision does not match.
    """

    patch: Mapping[str, object]
    expected_revision: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.patch, Mapping):
            raise TypeError("patch must be a mapping")
        if self.patch:
            _validate_patch_fields(self.patch)
        if self.expected_revision is not None and (
            not isinstance(self.expected_revision, str)
            or not self.expected_revision.strip()
        ):
            raise ValueError("expected_revision must be a non-empty string or None")
        object.__setattr__(self, "patch", MappingProxyType(dict(self.patch)))


@dataclass(frozen=True)
class SecretBackendDescriptor:
    """Metadata describing one registered secret backend.  No secret values."""

    name: str
    label: str
    available: bool
    selected: bool
    session_backed: bool
    locked: bool
    needs_unlock: bool
    login_required: bool
    persists_secrets: bool
    capabilities: Tuple[str, ...]
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "available": self.available,
            "selected": self.selected,
            "session_backed": self.session_backed,
            "locked": self.locked,
            "needs_unlock": self.needs_unlock,
            "login_required": self.login_required,
            "persists_secrets": self.persists_secrets,
            "capabilities": list(self.capabilities),
            "diagnostic": self.diagnostic,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("backend name must be a non-empty string")
        _validate_text(self.label, "label")
        _validate_boolean(self.available, "available")
        _validate_boolean(self.selected, "selected")
        _validate_boolean(self.session_backed, "session_backed")
        _validate_boolean(self.locked, "locked")
        _validate_boolean(self.needs_unlock, "needs_unlock")
        _validate_boolean(self.login_required, "login_required")
        _validate_boolean(self.persists_secrets, "persists_secrets")
        _validate_text(self.diagnostic, "diagnostic")
        object.__setattr__(
            self, "capabilities", tuple(self.capabilities)
        )


@dataclass(frozen=True)
class SecretBackendRegistry:
    """The full backend registry plus the effective/selected state."""

    backends: Tuple[SecretBackendDescriptor, ...]
    effective_backend: str
    selected_backend: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backends": [b.to_dict() for b in self.backends],
            "effective_backend": self.effective_backend,
            "selected_backend": self.selected_backend,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.effective_backend, str) or not self.effective_backend:
            raise ValueError("effective_backend must be a non-empty string")
        if not isinstance(self.selected_backend, str) or not self.selected_backend:
            raise ValueError("selected_backend must be a non-empty string")
        object.__setattr__(self, "backends", tuple(self.backends))
        for backend in self.backends:
            if type(backend) is not SecretBackendDescriptor:
                raise TypeError("backends must be SecretBackendDescriptor values")


@dataclass(frozen=True)
class SecretBackendState:
    """Runtime state of the selected secret backend.  No secret values."""

    selected_backend: str
    effective_backend: str
    locked: bool
    needs_unlock: bool
    login_required: bool
    session_timeout: int
    remember_in_keyring: bool
    persists_secrets: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_backend": self.selected_backend,
            "effective_backend": self.effective_backend,
            "locked": self.locked,
            "needs_unlock": self.needs_unlock,
            "login_required": self.login_required,
            "session_timeout": self.session_timeout,
            "remember_in_keyring": self.remember_in_keyring,
            "persists_secrets": self.persists_secrets,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.selected_backend, str) or not self.selected_backend:
            raise ValueError("selected_backend must be a non-empty string")
        if not isinstance(self.effective_backend, str) or not self.effective_backend:
            raise ValueError("effective_backend must be a non-empty string")
        for field in ("locked", "needs_unlock", "login_required",
                      "remember_in_keyring", "persists_secrets"):
            _validate_boolean(getattr(self, field), field)
        _validate_integer(self.session_timeout, "session_timeout")


class UnlockResultKind(str, Enum):
    UNLOCKED = "unlocked"
    INTERACTION_REQUIRED = "interaction_required"
    LOGIN_REQUIRED = "login_required"
    BACKEND_UNAVAILABLE = "backend_unavailable"


class SecretMessageCode(str, Enum):
    """Stable presentation reasons for secret status and operation results."""

    SECRET_BACKEND_UNAVAILABLE = "secret_backend_unavailable"
    VAULT_SIGN_IN_REQUIRED = "vault_sign_in_required"
    UNLOCK_CANCELLED = "unlock_cancelled"
    VAULT_UNLOCK_FAILED = "vault_unlock_failed"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BITWARDEN_SERVER_CONFIGURATION_FAILED = "bitwarden_server_configuration_failed"
    BITWARDEN_LOGIN_CANCELLED = "bitwarden_login_cancelled"
    BITWARDEN_AUTHENTICATION_CHALLENGE_CANCELLED = (
        "bitwarden_authentication_challenge_cancelled"
    )
    BITWARDEN_TWO_STEP_LOGIN_CANCELLED = "bitwarden_two_step_login_cancelled"
    BITWARDEN_SIGN_IN_FAILED = "bitwarden_sign_in_failed"
    BITWARDEN_UNLOCK_CANCELLED = "bitwarden_unlock_cancelled"
    BITWARDEN_UNLOCK_FAILED = "bitwarden_unlock_failed"
    BITWARDEN_SYNC_FAILED = "bitwarden_sync_failed"
    RBW_CONFIGURATION_FAILED = "rbw_configuration_failed"
    RBW_UNLOCK_FAILED = "rbw_unlock_failed"
    RBW_SYNC_FAILED = "rbw_sync_failed"
    RBW_LOCK_FAILED = "rbw_lock_failed"
    DATABASE_PATH_REQUIRED = "database_path_required"
    DATABASE_CREATION_CANCELLED = "database_creation_cancelled"
    KEEPASS_DATABASE_CREATE_OR_UNLOCK_FAILED = (
        "keepass_database_create_or_unlock_failed"
    )
    KEEPASS_UNLOCK_CANCELLED = "keepass_unlock_cancelled"
    KEEPASS_DATABASE_UNLOCK_FAILED = "keepass_database_unlock_failed"
    REMEMBER_SESSION_BACKEND_REQUIRED = "remember_session_backend_required"
    REMEMBER_CANCELLED = "remember_cancelled"
    MASTER_PASSWORD_SAVE_FAILED = "master_password_save_failed"
    MASTER_PASSWORD_REMEMBER_FAILED = "master_password_remember_failed"
    REMEMBERED_MASTER_PASSWORD_REMOVE_FAILED = (
        "remembered_master_password_remove_failed"
    )
    REMEMBERED_MASTER_PASSWORD_FORGET_FAILED = (
        "remembered_master_password_forget_failed"
    )
    REMEMBERED_MASTER_PASSWORD_NOT_FOUND = "remembered_master_password_not_found"


class SecretTransferMessageCode(str, Enum):
    """Stable presentation reasons for backup/export/import outcomes."""

    BACKUP_ITEMS_REQUIRED = "backup_items_required"
    NOTHING_SELECTED_TO_EXPORT = "nothing_selected_to_export"
    BITWARDEN_BACKUP_UNAVAILABLE = "bitwarden_backup_unavailable"
    BITWARDEN_NOTE_TOO_LARGE = "bitwarden_note_too_large"
    BITWARDEN_BACKUP_TOO_LARGE = "bitwarden_backup_too_large"
    BITWARDEN_NOTE_LARGEST_SECTION = "bitwarden_note_largest_section"
    BITWARDEN_NOTE_REDUCE = "bitwarden_note_reduce"
    EXPORT_SPBK_INSTEAD = "export_spbk_instead"
    BITWARDEN_EXPORT_FAILED = "bitwarden_export_failed"
    BACKUP_EXPORT_FAILED = "backup_export_failed"
    SSH_BACKUP_EXPORT_FAILED = "ssh_backup_export_failed"
    SSH_CONFIG_FILES_SKIPPED = "ssh_config_files_skipped"
    REFERENCED_KEY_FILES_MISSING = "referenced_key_files_missing"
    BACKUP_FILE_NOT_FOUND = "backup_file_not_found"
    CONFIGURATION_IMPORT_FAILED = "configuration_import_failed"
    CONFIGURATION_IMPORT_FAILED_GENERIC = "configuration_import_failed_generic"
    ARCHIVE_DECRYPT_OR_READ_FAILED = "archive_decrypt_or_read_failed"
    WRONG_PASSPHRASE_OR_CORRUPT_BACKUP = "wrong_passphrase_or_corrupt_backup"
    BACKUP_IMPORT_FAILED = "backup_import_failed"
    BACKUP_IMPORT_FAILED_GENERIC = "backup_import_failed_generic"
    SECRETS_NOT_PERSISTED = "secrets_not_persisted"
    BITWARDEN_BACKUP_LIST_FAILED = "bitwarden_backup_list_failed"
    BITWARDEN_BACKUP_NOT_FOUND = "bitwarden_backup_not_found"
    BITWARDEN_BACKUP_READ_FAILED = "bitwarden_backup_read_failed"
    INVALID_SSHPILOT_BACKUP = "invalid_sshpilot_backup"
    SSH_BACKUP_LIST_FAILED = "ssh_backup_list_failed"
    SSH_BACKUP_NOT_FOUND = "ssh_backup_not_found"
    SSH_BACKUP_READ_FAILED = "ssh_backup_read_failed"
    ENCRYPTION_REQUEST_TIMED_OUT = "encryption_request_timed_out"
    ENCRYPTION_CANCELLED = "encryption_cancelled"
    DECRYPTION_CANCELLED = "decryption_cancelled"
    BITWARDEN_NOTE_SAVE_FAILED = "bitwarden_note_save_failed"
    SSH_SERVER_CONNECTION_FAILED = "ssh_server_connection_failed"
    SSH_SERVER_DIRECTORY_UNAVAILABLE = "ssh_server_directory_unavailable"
    SSH_SERVER_FREE_SPACE_INSUFFICIENT = "ssh_server_free_space_insufficient"
    SSH_SERVER_WRITE_FAILED = "ssh_server_write_failed"
    INVALID_JSON_FILE = "invalid_json_file"
    IMPORT_DATA_NOT_OBJECT = "import_data_not_object"
    IMPORT_VERSION_MISSING = "import_version_missing"
    BACKUP_VERSION_UNSUPPORTED = "backup_version_unsupported"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    APP_CONFIG_MISSING = "app_config_missing"
    APP_CONFIG_NOT_OBJECT = "app_config_not_object"
    CONNECTIONS_NOT_LIST = "connections_not_list"
    CONNECTION_ENTRY_NOT_OBJECT = "connection_entry_not_object"
    CONNECTION_NICKNAME_REQUIRED = "connection_nickname_required"
    CONNECTION_NICKNAME_WHITESPACE = "connection_nickname_whitespace"
    CONFIGURATION_REPLACE_FAILED = "configuration_replace_failed"
    CONFIGURATION_MERGE_FAILED = "configuration_merge_failed"
    CONNECTION_STORE_RESTORE_FAILED = "connection_store_restore_failed"
    CONNECTION_STORE_VERSION_UNSUPPORTED = "connection_store_version_unsupported"
    CONNECTION_RESTORE_FAILED = "connection_restore_failed"
    CONNECTION_UPDATE_FAILED = "connection_update_failed"
    GROUP_RESTORE_FAILED = "group_restore_failed"
    GROUP_UPDATE_FAILED = "group_update_failed"
    GROUP_REMOVE_FAILED = "group_remove_failed"
    GROUP_ORDER_FAILED = "group_order_failed"
    STALE_MEMBERSHIP_REMOVE_FAILED = "stale_membership_remove_failed"
    RESTORED_GROUP_CONNECTION_MISSING = "restored_group_connection_missing"
    BACKUP_ROOT_CONNECTION_MISSING = "backup_root_connection_missing"
    UNKNOWN_CONNECTION_METADATA_SKIPPED = "unknown_connection_metadata_skipped"
    METADATA_RESTORE_FAILED = "metadata_restore_failed"
    DISPLAY_NAME_RESTORE_FAILED = "display_name_restore_failed"
    CONNECTION_REMOVE_FAILED = "connection_remove_failed"


_SECRET_TRANSFER_MESSAGE_PARAMETER_TYPES = {
    code: {}
    for code in SecretTransferMessageCode
}
_SECRET_TRANSFER_MESSAGE_PARAMETER_TYPES.update(
    {
        SecretTransferMessageCode.BITWARDEN_NOTE_TOO_LARGE: {
            "length": int,
            "limit": int,
        },
        SecretTransferMessageCode.BITWARDEN_NOTE_LARGEST_SECTION: {
            "section": str,
            "cost": int,
        },
        SecretTransferMessageCode.SSH_CONFIG_FILES_SKIPPED: {
            "count": int,
            "paths": str,
        },
        SecretTransferMessageCode.REFERENCED_KEY_FILES_MISSING: {
            "count": int,
            "paths": str,
        },
        SecretTransferMessageCode.BACKUP_FILE_NOT_FOUND: {"source": str},
        SecretTransferMessageCode.SSH_SERVER_DIRECTORY_UNAVAILABLE: {
            "directory": str,
        },
        SecretTransferMessageCode.SSH_SERVER_FREE_SPACE_INSUFFICIENT: {
            "required": str,
            "available": str,
            "directory": str,
        },
        SecretTransferMessageCode.BACKUP_VERSION_UNSUPPORTED: {"version": str},
        SecretTransferMessageCode.SCHEMA_VERSION_UNSUPPORTED: {"version": str},
        SecretTransferMessageCode.CONNECTION_RESTORE_FAILED: {"connection": str},
        SecretTransferMessageCode.CONNECTION_UPDATE_FAILED: {"connection": str},
        SecretTransferMessageCode.GROUP_RESTORE_FAILED: {"group": str},
        SecretTransferMessageCode.GROUP_UPDATE_FAILED: {"group": str},
        SecretTransferMessageCode.GROUP_REMOVE_FAILED: {"group": str},
        SecretTransferMessageCode.GROUP_ORDER_FAILED: {"group": str},
        SecretTransferMessageCode.STALE_MEMBERSHIP_REMOVE_FAILED: {
            "connection": str,
        },
        SecretTransferMessageCode.RESTORED_GROUP_CONNECTION_MISSING: {
            "connection": str,
        },
        SecretTransferMessageCode.BACKUP_ROOT_CONNECTION_MISSING: {
            "connection": str,
        },
        SecretTransferMessageCode.UNKNOWN_CONNECTION_METADATA_SKIPPED: {
            "connection": str,
        },
        SecretTransferMessageCode.METADATA_RESTORE_FAILED: {"connection": str},
        SecretTransferMessageCode.DISPLAY_NAME_RESTORE_FAILED: {
            "connection": str,
        },
        SecretTransferMessageCode.CONNECTION_REMOVE_FAILED: {"connection": str},
    }
)

_BACKUP_SECTION_CODES = frozenset(
    {"app_settings", "ssh_config", "known_hosts", "credentials", "private_keys"}
)


@dataclass(frozen=True)
class SecretTransferMessage:
    """One localizable transfer message plus an optional opaque diagnostic."""

    code: SecretTransferMessageCode
    parameters: Mapping[str, object] = field(default_factory=dict)
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "parameters": dict(self.parameters),
            "diagnostic": self.diagnostic,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.code, SecretTransferMessageCode):
            raise TypeError("transfer message code is invalid")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("transfer message parameters must be a mapping")
        parameters = dict(self.parameters)
        expected = _SECRET_TRANSFER_MESSAGE_PARAMETER_TYPES[self.code]
        if set(parameters) != set(expected):
            raise ValueError("transfer message parameters do not match the message code")
        for key, expected_type in expected.items():
            value = parameters[key]
            if expected_type is int:
                if type(value) is not int or value < 0:
                    raise ValueError(
                        f"transfer message parameter {key} must be a non-negative integer"
                    )
            elif type(value) is not str or not value:
                raise ValueError(
                    f"transfer message parameter {key} must be a non-empty string"
                )
        if self.code is SecretTransferMessageCode.BITWARDEN_NOTE_LARGEST_SECTION:
            if parameters["section"] not in _BACKUP_SECTION_CODES:
                raise ValueError("transfer message section is invalid")
        _validate_text(self.diagnostic, "diagnostic")
        object.__setattr__(self, "parameters", MappingProxyType(parameters))


_SECRET_MESSAGE_PARAMETER_KEYS = {
    code: frozenset()
    for code in SecretMessageCode
}
_SECRET_MESSAGE_PARAMETER_KEYS.update(
    {
        SecretMessageCode.SECRET_BACKEND_UNAVAILABLE: frozenset({"backend"}),
        SecretMessageCode.BACKEND_UNAVAILABLE: frozenset({"backend"}),
    }
)


def _validate_secret_message(
    message_code: Optional[SecretMessageCode],
    message_parameters: Mapping[str, str],
    diagnostic: str,
) -> Mapping[str, str]:
    if not isinstance(message_parameters, Mapping):
        raise TypeError("secret message parameters must be a mapping")
    parameters = dict(message_parameters)
    if message_code is None:
        if parameters:
            raise ValueError("a secret message code is required for parameters")
        if diagnostic:
            raise ValueError("a secret message code is required for a diagnostic")
    else:
        if not isinstance(message_code, SecretMessageCode):
            raise TypeError("secret message code is invalid")
        expected = _SECRET_MESSAGE_PARAMETER_KEYS[message_code]
        if set(parameters) != expected:
            raise ValueError("secret message parameters do not match the message code")
        for key, value in parameters.items():
            _validate_text(value, f"secret message parameter {key}")
            if not value:
                raise ValueError(f"secret message parameter {key} cannot be empty")
    _validate_text(diagnostic, "diagnostic")
    return MappingProxyType(parameters)


@dataclass(frozen=True)
class SecretUnlockResult:
    """Outcome of a secret-backend unlock request.  Never carries a secret."""

    kind: UnlockResultKind
    backend: str
    message_code: Optional[SecretMessageCode] = None
    message_parameters: Mapping[str, str] = field(default_factory=dict)
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "backend": self.backend,
            "message_code": (
                self.message_code.value if self.message_code is not None else None
            ),
            "message_parameters": dict(self.message_parameters),
            "diagnostic": self.diagnostic,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.kind, UnlockResultKind):
            raise TypeError("kind must be an UnlockResultKind")
        _validate_text(self.backend, "backend")
        object.__setattr__(
            self,
            "message_parameters",
            _validate_secret_message(
                self.message_code, self.message_parameters, self.diagnostic
            ),
        )


class SecretOperationState(str, Enum):
    SUCCESS = "success"
    LOGIN_REQUIRED = "login_required"
    INTERACTION_REQUIRED = "interaction_required"
    FAILED = "failed"


@dataclass(frozen=True)
class SecretOperationResult:
    """Safe outcome of a backend lifecycle operation.  No secret values."""

    state: SecretOperationState
    backend: str
    message_code: Optional[SecretMessageCode] = None
    message_parameters: Mapping[str, str] = field(default_factory=dict)
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "backend": self.backend,
            "message_code": (
                self.message_code.value if self.message_code is not None else None
            ),
            "message_parameters": dict(self.message_parameters),
            "diagnostic": self.diagnostic,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.state, SecretOperationState):
            raise TypeError("state must be a SecretOperationState")
        _validate_text(self.backend, "backend")
        object.__setattr__(
            self,
            "message_parameters",
            _validate_secret_message(
                self.message_code, self.message_parameters, self.diagnostic
            ),
        )


@dataclass(frozen=True)
class BitwardenStatus:
    """Safe Bitwarden account/lifecycle status."""

    logged_in: bool
    unlocked: bool
    needs_login: bool
    email: str
    server_url: str
    profile: str
    twofa_required: bool = False
    message_code: Optional[SecretMessageCode] = None
    message_parameters: Mapping[str, str] = field(default_factory=dict)
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "logged_in": self.logged_in,
            "unlocked": self.unlocked,
            "needs_login": self.needs_login,
            "email": self.email,
            "server_url": self.server_url,
            "profile": self.profile,
            "twofa_required": self.twofa_required,
            "message_code": (
                self.message_code.value if self.message_code is not None else None
            ),
            "message_parameters": dict(self.message_parameters),
            "diagnostic": self.diagnostic,
        }

    def __post_init__(self) -> None:
        for field in ("logged_in", "unlocked", "needs_login", "twofa_required"):
            _validate_boolean(getattr(self, field), field)
        _validate_text(self.email, "email")
        _validate_text(self.server_url, "server_url")
        _validate_text(self.profile, "profile")
        object.__setattr__(
            self,
            "message_parameters",
            _validate_secret_message(
                self.message_code, self.message_parameters, self.diagnostic
            ),
        )


@dataclass(frozen=True)
class RbwStatus:
    """Safe rbw account/lifecycle status."""

    installed: bool
    configured: bool
    unlocked: bool
    email: str
    base_url: str
    message_code: Optional[SecretMessageCode] = None
    message_parameters: Mapping[str, str] = field(default_factory=dict)
    diagnostic: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "configured": self.configured,
            "unlocked": self.unlocked,
            "email": self.email,
            "base_url": self.base_url,
            "message_code": (
                self.message_code.value if self.message_code is not None else None
            ),
            "message_parameters": dict(self.message_parameters),
            "diagnostic": self.diagnostic,
        }

    def __post_init__(self) -> None:
        for field in ("installed", "configured", "unlocked"):
            _validate_boolean(getattr(self, field), field)
        _validate_text(self.email, "email")
        _validate_text(self.base_url, "base_url")
        object.__setattr__(
            self,
            "message_parameters",
            _validate_secret_message(
                self.message_code, self.message_parameters, self.diagnostic
            ),
        )


@dataclass(frozen=True)
class SecretTransferResult:
    """Safe outcome of a daemon-owned secret export/import.

    Contains only paths, counts, structured presentation messages, and opaque
    diagnostics — never secret values or credential records.
    """

    operation: str  # "export" | "import"
    path: str
    counts: Mapping[str, int]
    warnings: Tuple[SecretTransferMessage, ...]
    status: SecretOperationState
    message: Optional[SecretTransferMessage] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "path": self.path,
            "counts": dict(self.counts),
            "warnings": [warning.to_dict() for warning in self.warnings],
            "status": self.status.value,
            "message": self.message.to_dict() if self.message is not None else None,
        }

    def __post_init__(self) -> None:
        if self.operation not in ("export", "import"):
            raise ValueError("operation must be 'export' or 'import'")
        _validate_text(self.path, "path")
        if not isinstance(self.counts, Mapping):
            raise TypeError("counts must be a mapping")
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        for warning in self.warnings:
            if type(warning) is not SecretTransferMessage:
                raise TypeError("warnings must be SecretTransferMessage values")
        if not isinstance(self.status, SecretOperationState):
            raise TypeError("status must be a SecretOperationState")
        if self.message is not None and type(self.message) is not SecretTransferMessage:
            raise TypeError("message must be a SecretTransferMessage or None")


@dataclass(frozen=True)
class SecretTransferPreview:
    """Safe metadata-only preview of one backup source."""

    kind: str  # "spbk" | "json" | "bitwarden" | "ssh" | "unknown"
    encrypted: bool = False
    included: Mapping[str, bool] = field(default_factory=dict)
    error: Optional[SecretTransferMessage] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "encrypted": self.encrypted,
            "included": dict(self.included),
            "error": self.error.to_dict() if self.error is not None else None,
        }

    def __post_init__(self) -> None:
        if self.kind not in ("spbk", "json", "bitwarden", "ssh", "unknown"):
            raise ValueError("backup preview kind is invalid")
        _validate_boolean(self.encrypted, "encrypted")
        if not isinstance(self.included, Mapping) or not all(
            type(key) is str and type(value) is bool
            for key, value in self.included.items()
        ):
            raise TypeError("included must be a mapping of boolean values")
        object.__setattr__(self, "included", MappingProxyType(dict(self.included)))
        if self.error is not None and type(self.error) is not SecretTransferMessage:
            raise TypeError("error must be a SecretTransferMessage or None")
