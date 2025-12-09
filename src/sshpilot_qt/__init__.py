"""Qt compatibility shims for sshPilot.

This package provides lightweight wrappers that mirror common Qt concepts
such as signals, timers, and simple UI helpers. They allow the existing GTK-
centric business logic to be reused while progressively introducing Qt
patterns without requiring the full Qt runtime during headless testing.
"""

from .signals import ConnectionSignals, Signal, QObject
from .async_utils import QTimer

__all__ = ["ConnectionSignals", "Signal", "QObject", "QTimer"]

