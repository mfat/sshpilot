"""GTK-free settings models: defaults, migration, serialization, SSH overrides."""
from .assistant_access import (
    CONFIG_SECTION as ASSISTANT_ACCESS_SECTION,
    DEFAULT_ASSISTANT_ACCESS,
    assistant_access_from_settings,
    compute_assistant_access_revision,
    normalize_assistant_access,
)
from .defaults import CONFIG_VERSION, get_default_config
from .identity import (
    AUTO_PROVIDER,
    CUSTOM_PROVIDER,
    DEFAULT_IDENTITY_SETTINGS,
    compute_identity_revision,
    normalize_custom_socket,
    normalize_identity_settings,
    normalize_provider,
)
from .identity import (
    EDITABLE_FIELDS as IDENTITY_EDITABLE_FIELDS,
)
from .identity import (
    FIELD_TO_CONFIG_KEY as IDENTITY_FIELD_TO_CONFIG_KEY,
)
from .migration import ensure_config_defaults
from .ssh_overrides import (
    DEFAULT_SSH_OVERRIDES,
    EDITABLE_FIELDS,
    FIELD_TO_CONFIG_KEY,
    INTEGER_RANGES,
    VALID_HOST_KEY_VALUES,
    compose_ssh_overrides,
    compute_ssh_overrides_revision,
    normalize_ssh_overrides,
    ssh_config_from_settings,
    ssh_settings_from_values,
)
from .store import (
    SettingsFileError,
    get_nested,
    load_settings,
    load_settings_strict,
    save_settings,
    set_nested,
    settings_transaction_lock,
)

__all__ = [
    "ASSISTANT_ACCESS_SECTION",
    "AUTO_PROVIDER",
    "CONFIG_VERSION",
    "CUSTOM_PROVIDER",
    "DEFAULT_ASSISTANT_ACCESS",
    "DEFAULT_IDENTITY_SETTINGS",
    "DEFAULT_SSH_OVERRIDES",
    "EDITABLE_FIELDS",
    "FIELD_TO_CONFIG_KEY",
    "IDENTITY_EDITABLE_FIELDS",
    "IDENTITY_FIELD_TO_CONFIG_KEY",
    "INTEGER_RANGES",
    "VALID_HOST_KEY_VALUES",
    "SettingsFileError",
    "assistant_access_from_settings",
    "compose_ssh_overrides",
    "compute_assistant_access_revision",
    "compute_identity_revision",
    "compute_ssh_overrides_revision",
    "ensure_config_defaults",
    "get_default_config",
    "get_nested",
    "load_settings",
    "load_settings_strict",
    "normalize_assistant_access",
    "normalize_custom_socket",
    "normalize_identity_settings",
    "normalize_provider",
    "normalize_ssh_overrides",
    "save_settings",
    "set_nested",
    "settings_transaction_lock",
    "ssh_config_from_settings",
    "ssh_settings_from_values",
]
