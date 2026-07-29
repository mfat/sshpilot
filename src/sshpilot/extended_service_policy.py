"""Route policy for Phase 10 extended services (SFTP, transfers, forwards).

Mirrors :mod:`sshpilot.daemon_terminal_policy`: route selection is pure
product/user policy. Daemon mode never silently falls back to a GTK-owned
local ``ssh``/``scp``/``ssh -N`` process. Legacy local ownership is only
available via an explicit setting or in-process client mode.

External paths (GVFS file manager, system terminal) remain external and are
outside this module.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any, Mapping, Optional

# Keep in sync with ``api.client_factory.CLIENT_MODE_ENVIRONMENT`` — duplicated
# here so this policy module stays importable without pulling the daemon stack
# (important for file-manager unit-test harnesses that stub ssh_config_utils).
_CLIENT_MODE_ENVIRONMENT = "SSHPILOT_CLIENT_MODE"


LEGACY_LOCAL_SFTP_SETTING = "file_manager.legacy_local_sftp"
LEGACY_SCP_SETTING = "file_manager.legacy_scp"
LEGACY_LOCAL_FORWARD_SETTING = "forwards.legacy_local_process"


class ExtendedServiceRoute(str, Enum):
    """Ownership route for SFTP / transfer / forward production surfaces."""

    DAEMON = "daemon"
    LEGACY_LOCAL = "legacy_local"


def _config_of(window_or_config: Any):
    nested = getattr(window_or_config, "config", None)
    if nested is not None and callable(getattr(nested, "get_setting", None)):
        return nested
    return window_or_config


def _get_setting(config, key: str, default=None):
    getter = getattr(config, "get_setting", None)
    if callable(getter):
        return getter(key, default)
    return default


def _explicit_client_mode(environment: Optional[Mapping[str, str]] = None) -> str:
    source = os.environ if environment is None else environment
    return (source.get(_CLIENT_MODE_ENVIRONMENT) or "").strip()


def _requested_client_mode_name(
    environment: Optional[Mapping[str, str]] = None,
) -> str:
    """Return ``in_process`` or ``daemon`` (raises ValueError if invalid)."""

    explicit = _explicit_client_mode(environment)
    if not explicit:
        return "in_process"
    normalized = explicit.lower()
    if normalized not in {"in_process", "daemon"}:
        raise ValueError("invalid SSH Pilot client mode")
    return normalized


def prefer_daemon_extended_services(
    window_or_config=None,
    *,
    client=None,
    prefer_daemon: Optional[bool] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    """Return whether extended services should be daemon-owned.

    Precedence:

    1. Explicit ``prefer_daemon`` argument
    2. Live ``DaemonClient`` instance
    3. ``SSHPILOT_CLIENT_MODE=daemon``
    4. Stage C promotion: unset client mode + ``terminal.daemon_backed_ssh``
    5. Otherwise in-process / legacy-compatible
    """

    if prefer_daemon is not None:
        return bool(prefer_daemon)

    try:
        from .api.daemon_client import DaemonClient
    except Exception:  # pragma: no cover - import guard for partial test stubs
        DaemonClient = ()  # type: ignore[misc, assignment]

    if client is not None and isinstance(client, DaemonClient):
        return True

    try:
        mode = _requested_client_mode_name(environment)
    except ValueError:
        return True

    if mode == "daemon":
        return True

    explicit = _explicit_client_mode(environment)
    if explicit:
        # Explicit in_process (or any non-daemon value) wins.
        return False

    # Stage C promotion only when a real config is available to read the
    # daemon-backed setting. Bare factory calls without config stay legacy-
    # compatible (in-process / tests).
    config = _config_of(window_or_config)
    if config is None or not callable(getattr(config, "get_setting", None)):
        return False
    return bool(_get_setting(config, "terminal.daemon_backed_ssh", True))


def allow_legacy_local_sftp(
    window_or_config=None,
    *,
    prefer_daemon: Optional[bool] = None,
    client=None,
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    """True when the GTK-owned OpenSSH SFTP backend may be used."""

    config = _config_of(window_or_config)
    if bool(_get_setting(config, LEGACY_LOCAL_SFTP_SETTING, False)):
        return True
    return not prefer_daemon_extended_services(
        config,
        client=client,
        prefer_daemon=prefer_daemon,
        environment=environment,
    )


def allow_legacy_scp(
    window_or_config=None,
    *,
    prefer_daemon: Optional[bool] = None,
    client=None,
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    """True when the VTE ``scp`` UI may be used.

    In-process mode keeps SCP available. Daemon mode requires the explicit
    ``file_manager.legacy_scp`` setting.
    """

    config = _config_of(window_or_config)
    if bool(_get_setting(config, LEGACY_SCP_SETTING, False)):
        return True
    return not prefer_daemon_extended_services(
        config,
        client=client,
        prefer_daemon=prefer_daemon,
        environment=environment,
    )


def allow_legacy_local_forward(
    window_or_config=None,
    *,
    prefer_daemon: Optional[bool] = None,
    client=None,
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    """True when ControlMaster / ``ssh -N`` local forwards may be used."""

    config = _config_of(window_or_config)
    if bool(_get_setting(config, LEGACY_LOCAL_FORWARD_SETTING, False)):
        return True
    return not prefer_daemon_extended_services(
        config,
        client=client,
        prefer_daemon=prefer_daemon,
        environment=environment,
    )


def resolve_file_manager_route(
    window_or_config=None,
    *,
    prefer_daemon: Optional[bool] = None,
    client=None,
    environment: Optional[Mapping[str, str]] = None,
) -> ExtendedServiceRoute:
    """Resolve file-manager backend ownership (policy only, no readiness)."""

    if allow_legacy_local_sftp(
        window_or_config,
        prefer_daemon=prefer_daemon,
        client=client,
        environment=environment,
    ):
        return ExtendedServiceRoute.LEGACY_LOCAL
    return ExtendedServiceRoute.DAEMON


def daemon_sftp_unavailable_message(*, missing=None) -> str:
    if missing:
        names = ", ".join(sorted(c.value for c in missing))
        return (
            "The running SSH Pilot service does not support the file "
            f"manager's SFTP API (missing: {names}); "
            "refusing to spawn a local SFTP subprocess."
        )
    return (
        "The SSH Pilot background service is required for the file manager "
        "in daemon mode, but no DaemonClient is available. "
        "Refusing to spawn a local SFTP subprocess. "
        f"Set {LEGACY_LOCAL_SFTP_SETTING}=true for explicit legacy OpenSSH SFTP."
    )


def daemon_forward_unavailable_message(*, detail: str = "") -> str:
    base = (
        "Daemon-owned local forwarding failed"
        if detail
        else "Daemon-owned local forwarding is required"
    )
    suffix = f": {detail}" if detail else ""
    return (
        f"{base}{suffix}. "
        "Refusing silent ControlMaster / ssh -N fallback. "
        f"Set {LEGACY_LOCAL_FORWARD_SETTING}=true for explicit legacy local forwards."
    )


def daemon_scp_unavailable_message() -> str:
    return (
        "Legacy SCP transfers are disabled in daemon mode. "
        "Use Manage Files (daemon SFTP/transfers), or set "
        f"{LEGACY_SCP_SETTING}=true to enable the legacy SCP UI."
    )
