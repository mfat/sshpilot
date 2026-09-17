"""Frontend-neutral sshPilot client API."""

from typing import TYPE_CHECKING, Any
from importlib import import_module

if TYPE_CHECKING:
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

_LAZY_EXPORTS = {
    "Capabilities": ("capabilities", "Capabilities"),
    "Capability": ("capabilities", "Capability"),
    "SshPilotClient": ("client", "SshPilotClient"),
    "DaemonClient": ("daemon_client", "DaemonClient"),
    "ErrorCode": ("errors", "ErrorCode"),
    "SshPilotError": ("errors", "SshPilotError"),
    "CoreEvent": ("events", "CoreEvent"),
    "EventType": ("events", "EventType"),
    "Subscription": ("events", "Subscription"),
    "TerminalSubscription": ("terminal_events", "TerminalSubscription"),
    "API_IMPLEMENTATION_VERSION": ("version", "API_IMPLEMENTATION_VERSION"),
    "PROTOCOL_VERSION": ("version", "PROTOCOL_VERSION"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        mod_name, attr_name = target
        mod = import_module(f".{mod_name}", __name__)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | set(__all__))
