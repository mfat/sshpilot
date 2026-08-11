"""Runtime MCP authorization policy.

The runtime server is explicitly opt-in. The daemon's capability model answers
"can a frontend technically do this?"; this policy answers "should an AI model
be allowed to invoke this operation?". Three levels, each strictly above the
previous:

* READ — observation only. May be enabled broadly.
* OPERATE — starts/stops interactive resources (sessions, SFTP services) and
  observes operations/interactions. Requires explicit opt-in.
* MUTATE — remote file writes/deletes, transfers, connection edits, forwards,
  keys. Disabled by default; requires explicit opt-in. MUTATE tools also
  require human confirmation at the tool layer.

No level ever exposes raw secrets.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Dict


class PermissionLevel(IntEnum):
    """Permission levels, ordered from least to most privileged."""

    READ = 1
    OPERATE = 2
    MUTATE = 3


class PolicyError(RuntimeError):
    """Raised when a tool is denied by the runtime policy."""


class RuntimePolicy:
    """Enforce READ/OPERATE/MUTATE granularity for runtime MCP tools."""

    def __init__(
        self,
        allow_read: bool = True,
        allow_operate: bool = False,
        allow_mutate: bool = False,
        *,
        name: str = "runtime-mcp",
    ) -> None:
        self._name = name
        self._levels: Dict[PermissionLevel, bool] = {
            PermissionLevel.READ: bool(allow_read),
            PermissionLevel.OPERATE: bool(allow_operate),
            PermissionLevel.MUTATE: bool(allow_mutate),
        }

    @property
    def name(self) -> str:
        return self._name

    def allows(self, level: PermissionLevel) -> bool:
        """Whether *level* (and every level below it) is enabled."""
        return all(self._levels[l] for l in PermissionLevel if l <= level)

    def require(self, level: PermissionLevel) -> None:
        """Raise :class:`PolicyError` unless *level* is permitted."""
        if not self.allows(level):
            raise PolicyError(
                f"{self._name} is not permitted at {level.name}; it requires "
                "explicit opt-in at configuration time"
            )

    def summary(self) -> dict:
        """Return a configuration snapshot for capability/tool discovery."""
        return {
            name: self.allows(level)
            for level, name in (
                (PermissionLevel.READ, "read"),
                (PermissionLevel.OPERATE, "operate"),
                (PermissionLevel.MUTATE, "mutate"),
            )
        }

    @classmethod
    def from_environment(
        cls,
        dct: Dict[str, str],
        *,
        allow_read: bool = True,
        name: str = "runtime-mcp",
    ) -> "RuntimePolicy":
        """Build a policy from an environment mapping (opt-in booleans).

        ``SSHPILOT_MCP_READ``/``SSHPILOT_MCP_OPERATE``/``SSHPILOT_MCP_MUTATE``
        each accept ``1``/``0`` or ``true``/``false``; anything else means the
        feature stays disabled.
        """
        read = _env_bool(dct.get("SSHPILOT_MCP_READ"), allow_read)
        operate = _env_bool(dct.get("SSHPILOT_MCP_OPERATE"), False)
        mutate = _env_bool(dct.get("SSHPILOT_MCP_MUTATE"), False)
        return cls(read, operate, mutate, name=name)


def _env_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}