"""GTK presentation adapters (must not be imported by core/api/daemon)."""

from .interaction import GtkInteractionProvider, get_default_provider, set_default_provider

__all__ = ["GtkInteractionProvider", "get_default_provider", "set_default_provider"]
