"""Local sshPilot daemon transport."""

from typing import TYPE_CHECKING, Any
from importlib import import_module

from .lifecycle import (
    DaemonAlreadyRunningError,
    SocketSecurityError,
    resolve_socket_path,
)

if TYPE_CHECKING:
    from .server import DaemonServer
    from .session_runtime import (
        SessionLaunchSpec,
        SessionProcessHandle,
        SessionProcessRunner,
        SessionRuntime,
        SubprocessSessionProcessRunner,
    )

__all__ = [
    "DaemonAlreadyRunningError",
    "DaemonServer",
    "SessionLaunchSpec",
    "SessionProcessHandle",
    "SessionProcessRunner",
    "SessionRuntime",
    "SocketSecurityError",
    "SubprocessSessionProcessRunner",
    "resolve_socket_path",
]

_LAZY_EXPORTS = {
    "DaemonServer": ("server", "DaemonServer"),
    "SessionLaunchSpec": ("session_runtime", "SessionLaunchSpec"),
    "SessionProcessHandle": ("session_runtime", "SessionProcessHandle"),
    "SessionProcessRunner": ("session_runtime", "SessionProcessRunner"),
    "SessionRuntime": ("session_runtime", "SessionRuntime"),
    "SubprocessSessionProcessRunner": ("session_runtime", "SubprocessSessionProcessRunner"),
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
