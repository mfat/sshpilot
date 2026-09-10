"""Daemon-only ownership policy for SFTP, transfers, and forwarding."""
from __future__ import annotations
from enum import Enum
from typing import Mapping, Optional


class ExtendedServiceRoute(str, Enum):
    DAEMON = "daemon"


def prefer_daemon_extended_services(
    window_or_config=None, *, client=None, prefer_daemon: Optional[bool] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    """All internal extended services are daemon-owned in production."""
    del window_or_config, client, prefer_daemon, environment
    return True


def resolve_file_manager_route(*args, **kwargs) -> ExtendedServiceRoute:
    del args, kwargs
    return ExtendedServiceRoute.DAEMON


def daemon_sftp_unavailable_message(*, missing=None) -> str:
    suffix = ""
    if missing:
        suffix = " Missing capabilities: " + ", ".join(
            sorted(capability.value for capability in missing)
        )
    return "The SSH Pilot daemon SFTP service is required." + suffix


def daemon_forward_unavailable_message(*, detail: str = "") -> str:
    suffix = f" ({detail})" if detail else ""
    return "The SSH Pilot daemon forwarding service is required." + suffix


def format_forward_failure_detail(summary) -> str:
    """Human-readable reason when a forward ends FAILED/CLOSED.

    Prefers the daemon ``ServiceFailure`` payload so toasts and ``--verbose``
    logs show the real cause instead of only the state name.
    """

    state = getattr(summary, "state", None)
    state_value = getattr(state, "value", None) or str(state or "unknown")
    failure = getattr(summary, "failure", None)
    if failure is None:
        return f"forward ended in state {state_value}"
    message = str(getattr(failure, "message", "") or "").strip()
    code = str(getattr(failure, "code", "") or "").strip()
    if message and code:
        return f"{message} [{code}] (state {state_value})"
    if message:
        return f"{message} (state {state_value})"
    if code:
        return f"forward ended in state {state_value} [{code}]"
    return f"forward ended in state {state_value}"


def daemon_scp_unavailable_message() -> str:
    return "Use the daemon-backed file transfer service."
