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


def allow_daemon_sftp(*args, **kwargs) -> bool:
    """Compatibility query: GTK-owned OpenSSH SFTP is never permitted."""
    del args, kwargs
    return False


def allow_legacy_scp(*args, **kwargs) -> bool:
    """GTK-owned SCP transfer workers are never permitted."""
    del args, kwargs
    return False


def allow_legacy_local_forward(*args, **kwargs) -> bool:
    """GTK-owned forwarding processes are never permitted."""
    del args, kwargs
    return False


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


def daemon_scp_unavailable_message() -> str:
    return "Use the daemon-backed file transfer service."
