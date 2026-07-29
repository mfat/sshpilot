"""GTK-free settings models: defaults, migration, serialization, SSH overrides."""
from .defaults import CONFIG_VERSION, get_default_config
from .migration import ensure_config_defaults
from .ssh_overrides import compose_ssh_overrides, ssh_settings_from_values
from .store import get_nested, load_settings, save_settings, set_nested

__all__ = [
    "CONFIG_VERSION",
    "compose_ssh_overrides",
    "ensure_config_defaults",
    "get_default_config",
    "get_nested",
    "load_settings",
    "save_settings",
    "set_nested",
    "ssh_settings_from_values",
]
