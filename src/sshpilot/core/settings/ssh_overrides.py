"""Compose global SSH CLI overrides from typed settings (GTK-free)."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence


def compose_ssh_overrides(
    ssh_cfg: Mapping[str, Any],
    *,
    controlmaster_extra: Optional[Sequence[str]] = None,
) -> List[str]:
    """Build the flat ``ssh.ssh_overrides`` argv fragment from SSH settings.

    Mirrors Preferences ▸ SSH Settings persistence. *controlmaster_extra* is
    supplied by the caller (typically ``ssh_multiplex.controlmaster_args()``)
    when ``ssh.controlmaster`` is enabled — core does not import multiplex.
    """
    overrides: List[str] = []

    if bool(ssh_cfg.get("batch_mode")):
        overrides.extend(["-o", "BatchMode=yes"])

    connect_timeout = ssh_cfg.get("connection_timeout")
    if connect_timeout is not None:
        try:
            ct = int(connect_timeout)
        except (TypeError, ValueError):
            ct = 0
        if ct > 0:
            overrides.extend(["-o", f"ConnectTimeout={ct}"])

    connection_attempts = ssh_cfg.get("connection_attempts")
    if connection_attempts is not None:
        try:
            ca = int(connection_attempts)
        except (TypeError, ValueError):
            ca = 0
        if ca > 0:
            overrides.extend(["-o", f"ConnectionAttempts={ca}"])

    keepalive_interval = ssh_cfg.get("keepalive_interval")
    if keepalive_interval is not None:
        try:
            ki = int(keepalive_interval)
        except (TypeError, ValueError):
            ki = 0
        if ki > 0:
            overrides.extend(["-o", f"ServerAliveInterval={ki}"])

    keepalive_count = ssh_cfg.get("keepalive_count_max")
    if keepalive_count is not None:
        try:
            kc = int(keepalive_count)
        except (TypeError, ValueError):
            kc = 0
        if kc > 0:
            overrides.extend(["-o", f"ServerAliveCountMax={kc}"])

    strict_host_value = str(ssh_cfg.get("strict_host_key_checking") or "").strip()
    if strict_host_value:
        overrides.extend(["-o", f"StrictHostKeyChecking={strict_host_value}"])

    if bool(ssh_cfg.get("compression")):
        overrides.append("-C")

    try:
        verbosity_value = int(ssh_cfg.get("verbosity") or 0)
    except (TypeError, ValueError):
        verbosity_value = 0
    safe_verbosity = max(0, min(3, verbosity_value))
    for _ in range(safe_verbosity):
        overrides.append("-v")

    debug_enabled = bool(ssh_cfg.get("debug_enabled"))
    log_level = None
    if safe_verbosity == 1:
        log_level = "VERBOSE"
    elif safe_verbosity == 2:
        log_level = "DEBUG2"
    elif safe_verbosity >= 3:
        log_level = "DEBUG3"
    elif debug_enabled:
        log_level = "DEBUG"
    if log_level:
        overrides.extend(["-o", f"LogLevel={log_level}"])

    if bool(ssh_cfg.get("controlmaster")) and controlmaster_extra:
        overrides.extend(list(controlmaster_extra))

    return overrides


def ssh_settings_from_values(
    *,
    connect_timeout: Optional[int] = None,
    connection_attempts: Optional[int] = None,
    keepalive_interval: Optional[int] = None,
    keepalive_count_max: Optional[int] = None,
    strict_host_key_checking: str = "accept-new",
    batch_mode: bool = False,
    compression: bool = False,
    verbosity: int = 0,
    debug_enabled: bool = False,
    controlmaster: bool = False,
) -> Dict[str, Any]:
    """Build an ssh settings fragment from typed values (for tests / CLI)."""
    return {
        "connection_timeout": connect_timeout,
        "connection_attempts": connection_attempts,
        "keepalive_interval": keepalive_interval,
        "keepalive_count_max": keepalive_count_max,
        "strict_host_key_checking": strict_host_key_checking,
        "batch_mode": batch_mode,
        "compression": compression,
        "verbosity": verbosity,
        "debug_enabled": debug_enabled,
        "controlmaster": controlmaster,
    }
