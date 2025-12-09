"""SSH helpers that are aware of the Qt signal bridge."""

from .session import SessionEventEmitter

__all__ = ["SessionEventEmitter"]
