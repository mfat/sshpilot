"""GTK presentation adapters (must not be imported by core/api/daemon)."""

from .interaction import GtkInteractionProvider, get_default_provider, set_default_provider
from .connection_store import ConnectionPresentationStore

__all__ = ["ConnectionPresentationStore", "GtkInteractionProvider", "get_default_provider", "set_default_provider"]
