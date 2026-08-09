"""GTK presentation adapters (must not be imported by core/api/daemon)."""

from .interaction import GtkInteractionProvider, get_default_provider, set_default_provider
from .connection_store import ConnectionPresentationStore
from .daemon_connection_services import DaemonConnectionServices
from .connection_runtime_status import (
    ConnectionRuntimeStatus,
    ConnectionRuntimeStatusStore,
)

__all__ = [
    "ConnectionPresentationStore",
    "ConnectionRuntimeStatus",
    "ConnectionRuntimeStatusStore",
    "DaemonConnectionServices",
    "GtkInteractionProvider",
    "get_default_provider",
    "set_default_provider",
]
