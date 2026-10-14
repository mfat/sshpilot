"""Daemon-owned assistant-access ceiling for the runtime MCP server.

The Preferences / API policy is the ceiling. Environment ``SSHPILOT_MCP_*``
flags can only tighten it (D011).

When the connected daemon does not advertise assistant-access (older builds
or minimal test daemons), the gate falls back to env-only policy with no
connection allowlist so contributor tooling keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Optional, Tuple

from .policy import PermissionLevel, PolicyError, RuntimePolicy


class AssistantAccessError(RuntimeError):
    """Raised when the daemon assistant-access policy refuses a call."""


@dataclass(frozen=True)
class _LegacyAccess:
    """Synthetic snapshot used when the daemon has no assistant-access API."""

    revision: str = "legacy"
    enabled: bool = True
    allowed_connection_ids: Optional[Tuple[str, ...]] = None
    operate: bool = True
    mutate: bool = True
    content: bool = True


class AssistantAccessGate:
    """Combine daemon assistant-access settings with the env RuntimePolicy."""

    def __init__(self, client: Any, env_policy: RuntimePolicy) -> None:
        self._client = client
        self._env_policy = env_policy
        self._cached: Optional[Any] = None
        self._legacy = False

    def refresh(self) -> Any:
        """Load the latest daemon snapshot (best-effort)."""
        getter = getattr(self._client, "get_assistant_access", None)
        if not callable(getter):
            self._legacy = True
            self._cached = _LegacyAccess()
            return self._cached
        try:
            self._cached = getter()
            self._legacy = False
        except Exception as error:  # noqa: BLE001
            # Unsupported capability / older daemon → env-only legacy mode.
            message = str(error).lower()
            if "unsupported" in message or "unavailable" in message:
                self._legacy = True
                self._cached = _LegacyAccess()
                return self._cached
            raise AssistantAccessError(
                "Could not read assistant access settings from the daemon. "
                "Enable AI Assistants in SSH Pilot Preferences."
            ) from error
        return self._cached

    @property
    def snapshot(self) -> Any:
        if self._cached is None:
            return self.refresh()
        return self._cached

    def invalidate(self) -> None:
        self._cached = None

    def is_enabled(self) -> bool:
        snap = self.snapshot
        return bool(snap is not None and getattr(snap, "enabled", False))

    def allowed_ids(self) -> Optional[FrozenSet[str]]:
        """Return the allowlist, or ``None`` when unrestricted (legacy)."""
        snap = self.snapshot
        if snap is None:
            return frozenset()
        ids = getattr(snap, "allowed_connection_ids", ())
        if ids is None:
            return None
        return frozenset(str(item) for item in ids)

    def effective_policy(self) -> RuntimePolicy:
        """Env policy AND daemon ceiling (daemon cannot be raised by env)."""
        snap = self.snapshot
        if snap is None or not getattr(snap, "enabled", False):
            return RuntimePolicy(
                allow_read=False,
                allow_operate=False,
                allow_mutate=False,
                allow_content=False,
                name="assistant-access",
            )
        daemon_operate = bool(getattr(snap, "operate", False))
        daemon_mutate = bool(getattr(snap, "mutate", False))
        daemon_content = bool(getattr(snap, "content", False))
        return RuntimePolicy(
            allow_read=self._env_policy.allows(PermissionLevel.READ),
            allow_operate=(
                self._env_policy.allows(PermissionLevel.OPERATE) and daemon_operate
            ),
            allow_mutate=(
                self._env_policy.allows(PermissionLevel.MUTATE)
                and daemon_operate
                and daemon_mutate
            ),
            allow_content=(
                self._env_policy.allows_content and daemon_content
            ),
            name="assistant-access",
        )

    def require_enabled(self) -> None:
        self.refresh()
        if not self.is_enabled():
            raise AssistantAccessError(
                "AI assistant access is disabled. Turn it on in SSH Pilot "
                "Preferences ▸ AI Assistants, then allowlist the servers "
                "you want assistants to use."
            )

    def require_connection(self, connection_id: str) -> None:
        self.require_enabled()
        allowed = self.allowed_ids()
        if allowed is None:
            return
        if connection_id not in allowed:
            raise AssistantAccessError(
                f"Connection {connection_id!r} is not allowlisted for AI "
                "assistants. Check Preferences ▸ AI Assistants or the "
                "connection's Allow AI assistants action."
            )

    def filter_connection_summaries(self, items: Any) -> Tuple[Any, ...]:
        allowed = self.allowed_ids()
        if not isinstance(items, (list, tuple)):
            return ()
        if allowed is None:
            return tuple(items)
        return tuple(
            item
            for item in items
            if str(getattr(item, "id", "")) in allowed
        )

    def status_dict(self) -> dict:
        snap = self.refresh()
        policy = self.effective_policy()
        allowed = self.allowed_ids()
        return {
            "enabled": bool(snap is not None and getattr(snap, "enabled", False)),
            "allowed_connection_ids": (
                None if allowed is None else sorted(allowed)
            ),
            "legacy_unrestricted": self._legacy,
            "daemon": {
                "operate": bool(getattr(snap, "operate", False)) if snap else False,
                "mutate": bool(getattr(snap, "mutate", False)) if snap else False,
                "content": bool(getattr(snap, "content", False)) if snap else False,
                "revision": getattr(snap, "revision", None) if snap else None,
            },
            "effective": policy.summary(),
            "environment": self._env_policy.summary(),
        }


def raise_as_tool_error(error: Exception, tool_error_cls: type) -> None:
    """Re-raise access/policy errors as tool errors."""
    if isinstance(error, (AssistantAccessError, PolicyError)):
        raise tool_error_cls(str(error)) from error
    raise error
