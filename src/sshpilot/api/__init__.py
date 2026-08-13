"""Frontend-neutral sshPilot client API."""

from .capabilities import Capabilities, Capability
from .client import SshPilotClient
from .daemon_client import DaemonClient
from .errors import ErrorCode, SshPilotError
from .events import CoreEvent, EventType, Subscription
from .terminal_events import TerminalSubscription
from .version import API_IMPLEMENTATION_VERSION, PROTOCOL_VERSION

__all__ = [
    "API_IMPLEMENTATION_VERSION",
    "PROTOCOL_VERSION",
    "Capabilities",
    "Capability",
    "CoreEvent",
    "DaemonClient",
    "ErrorCode",
    "EventType",
    "SshPilotClient",
    "SshPilotError",
    "Subscription",
    "TerminalSubscription",
]
