"""System ssh-agent identity provider.

Wraps the long-standing behaviour of inheriting ``SSH_AUTH_SOCK`` from the
environment so a spawned ssh process talks to the user's running ssh-agent. This
formalises that previously-implicit inheritance behind the
:class:`~sshpilot.identity.IdentityProvider` contract.

It deliberately does **not** start an agent or add keys to one (see
``askpass_utils.ensure_key_in_agent``); it only reports the agent and injects the
socket variables into a child environment.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Dict, List, Optional

from ..identity import Identity, IdentityProvider

logger = logging.getLogger(__name__)

# Variables that point a child process at the running agent. SSH_AUTH_SOCK is the
# socket ssh talks to; SSH_AGENT_PID is carried alongside for completeness.
_AGENT_ENV_VARS = ("SSH_AUTH_SOCK", "SSH_AGENT_PID")


class SystemAgentProvider(IdentityProvider):
    name = "system-agent"

    def is_available(self) -> bool:
        return bool(os.environ.get("SSH_AUTH_SOCK"))

    def apply_to_env(self, env: Dict[str, str]) -> Dict[str, str]:
        new_env = dict(env)
        for var in _AGENT_ENV_VARS:
            value = os.environ.get(var)
            if value:
                new_env[var] = value
        return new_env

    def list_identities(self) -> List[Identity]:
        if not self.is_available():
            return []
        try:
            result = subprocess.run(
                ["ssh-add", "-l"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception as exc:
            logger.debug("ssh-add -l failed: %s", exc)
            return []
        # rc 1 => "agent has no identities"; rc 2 => cannot connect to the agent.
        if result.returncode != 0:
            return []
        identities: List[Identity] = []
        for line in result.stdout.splitlines():
            identity = self._parse_agent_line(line)
            if identity is not None:
                identities.append(identity)
        return identities

    @staticmethod
    def _parse_agent_line(line: str) -> Optional[Identity]:
        """Parse one ``ssh-add -l`` line: ``<bits> <fingerprint> <comment> (<type>)``."""
        parts = line.split()
        if len(parts) < 3:
            return None
        fingerprint = parts[1]
        has_type = parts[-1].startswith("(") and parts[-1].endswith(")")
        key_type = parts[-1].strip("()") if has_type else ""
        comment = " ".join(parts[2:-1] if has_type else parts[2:])
        display_name = comment or key_type or fingerprint
        return Identity(
            id=fingerprint,
            display_name=display_name,
            fingerprint=fingerprint,
            provider_name="system-agent",
        )
