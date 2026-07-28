"""Frontend-neutral sshPilot client API."""

from .capabilities import Capabilities, Capability
from .client import SshPilotClient
from .errors import ErrorCode, SshPilotError
from .events import CoreEvent, EventType, Subscription
from .in_process_client import InProcessClient
from .version import API_IMPLEMENTATION_VERSION, PROTOCOL_VERSION

__all__ = [
    "API_IMPLEMENTATION_VERSION",
    "PROTOCOL_VERSION",
    "Capabilities",
    "Capability",
    "CoreEvent",
    "ErrorCode",
    "EventType",
    "InProcessClient",
    "SshPilotClient",
    "SshPilotError",
    "Subscription",
]

