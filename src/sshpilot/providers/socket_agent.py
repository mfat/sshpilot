"""Fixed-socket SSH agent identity providers.

Unlike the OS/desktop agent (:mod:`sshpilot.providers.system_agent`), whose
``SSH_AUTH_SOCK`` is a volatile per-session path inherited from the environment,
these agents listen on a **stable, well-known socket** (e.g. the 1Password SSH
agent at ``~/.1password/agent.sock``). For those the source of truth is an
``IdentityAgent`` directive in ``~/.ssh/config`` — persistent, ssh-native, and
honoured by *every* ssh invocation (not just app-spawned ones). ssh_config(5):
``IdentityAgent`` overrides ``SSH_AUTH_SOCK``.

So these providers contribute their socket via :meth:`ssh_config_directives`
(written to a managed ``Host *`` block) rather than via :meth:`apply_to_env`.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

from ..identity import Identity, IdentityProvider
from .system_agent import WINDOWS_AGENT_PIPE


class SocketAgentProvider(IdentityProvider):
    """An ssh-agent reachable at a fixed UNIX-domain socket path.

    Parameterised so presets (1Password, …) and a user-supplied "custom" socket
    share one implementation. The socket path is expressed as ``IdentityAgent`` in
    ssh config; nothing is injected into the environment.
    """

    def __init__(self, name: str, display_name: str, socket_path: str) -> None:
        self._name = name
        self._display_name = display_name
        self._socket_path = (socket_path or "").strip()
        self._expanded = os.path.expanduser(self._socket_path) if self._socket_path else ""

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def socket_path(self) -> str:
        """The configured socket path, unexpanded (as written to ssh config)."""
        return self._socket_path

    def is_available(self) -> bool:
        # Cheap and side-effect free: the agent is usable if its socket exists.
        if not self._expanded:
            return False
        if _is_windows_pipe(self._expanded):
            return _windows_pipe_exists(self._expanded)
        try:
            return os.path.exists(self._expanded)
        except Exception:
            return False


    def apply_to_env(self, env: Dict[str, str]) -> Dict[str, str]:
        # Config-driven (IdentityAgent), not env-driven — see module docstring.
        return dict(env)

    def ssh_config_directives(self) -> List[Tuple[str, str]]:
        if not self._socket_path:
            return []
        if _is_windows_pipe(self._socket_path) and (
            self._socket_path.replace("/", "\\").lower() == WINDOWS_AGENT_PIPE.lower()
        ):
            # OpenSSH for Windows uses this pipe by default. Writing it would
            # also need its backslashes doubled: ssh_config reads "\\" as "\".
            return []
        return [("IdentityAgent", self._socket_path)]

    def list_identities(self) -> List[Identity]:
        # Identity enumeration would require talking to the agent; keep this cheap and
        # non-throwing. The agent's keys still authenticate via the IdentityAgent socket.
        return []


_PIPE_NAMESPACE = "\\\\.\\pipe\\"  # \\.\pipe\
_PIPE_PREFIXES = (_PIPE_NAMESPACE, "//./pipe/")


def _is_windows_pipe(path: str) -> bool:
    return sys.platform == "win32" and path.lower().startswith(_PIPE_PREFIXES)


def _windows_pipe_exists(path: str) -> bool:
    # List the pipe namespace instead of opening the pipe: opening it would
    # consume one of the agent's server instances (see system_agent).
    name = path.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    try:
        return name in (entry.lower() for entry in os.listdir(_PIPE_NAMESPACE))
    except OSError:
        return False


# -- built-in presets ---------------------------------------------------------
ONEPASSWORD = "onepassword"
#: 1Password's agent socket. On Windows it serves the OpenSSH agent pipe
#: instead (the OpenSSH agent service must be stopped for it to take over).
ONEPASSWORD_SOCKET = (
    WINDOWS_AGENT_PIPE if sys.platform == "win32" else "~/.1password/agent.sock"
)


def onepassword_provider() -> SocketAgentProvider:
    """The 1Password SSH agent preset."""
    return SocketAgentProvider(ONEPASSWORD, "1Password", ONEPASSWORD_SOCKET)
