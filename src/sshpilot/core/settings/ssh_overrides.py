"""Global SSH overrides: field model, strict normalization, revision, compose.

This is the single GTK-free owner of the semantic SSH override fields:

* the public field name → persisted config key mapping,
* the canonical defaults for every editable field,
* the valid value constraints (integer ranges, host-key-checking enum),
* the strict conversion that turns a raw ``ssh`` subtree into normalized
  semantic values, and
* the deterministic revision token over those normalized values.

The API layer and the daemon service both consume these contracts so the
values used by ``GlobalSshOverrides`` and the revision token always agree.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Canonical field model
# ---------------------------------------------------------------------------

# Semantic public field name -> dotted persisted config key.
FIELD_TO_CONFIG_KEY: Dict[str, str] = {
    "connect_timeout": "ssh.connection_timeout",
    "connection_attempts": "ssh.connection_attempts",
    "server_alive_interval": "ssh.keepalive_interval",
    "server_alive_count_max": "ssh.keepalive_count_max",
    "strict_host_key_checking": "ssh.strict_host_key_checking",
    "batch_mode": "ssh.batch_mode",
    "compression": "ssh.compression",
    "verbosity": "ssh.verbosity",
    "debug_enabled": "ssh.debug_enabled",
}

# Fields exposed through the API model (the public surface).
EDITABLE_FIELDS = frozenset(FIELD_TO_CONFIG_KEY.keys())

# Integer fields with their documented valid ranges.
INTEGER_RANGES: Dict[str, tuple[int, int]] = {
    "connect_timeout": (0, 600),
    "connection_attempts": (0, 100),
    "server_alive_interval": (0, 86400),
    "server_alive_count_max": (0, 100),
    "verbosity": (0, 3),
}

# Valid host-key-checking values accepted by OpenSSH.
VALID_HOST_KEY_VALUES = frozenset({"accept-new", "yes", "no", "ask"})

# Canonical semantic defaults. A missing raw key normalizes to exactly these
# values, so "field absent" and "field set to its default" are equivalent for
# the snapshot and the revision token.
DEFAULT_SSH_OVERRIDES: Dict[str, Any] = {
    "connect_timeout": 0,
    "connection_attempts": 0,
    "server_alive_interval": 0,
    "server_alive_count_max": 0,
    "strict_host_key_checking": "accept-new",
    "batch_mode": False,
    "compression": False,
    "verbosity": 0,
    "debug_enabled": False,
}


# ---------------------------------------------------------------------------
# Strict normalization (single conversion used by model + revision)
# ---------------------------------------------------------------------------

def normalize_ssh_overrides(ssh: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly convert a raw ``ssh`` subtree to normalized semantic values.

    Returns exactly the nine public field names with canonical values. Missing
    keys normalize to :data:`DEFAULT_SSH_OVERRIDES`; ``strict_host_key_checking``
    missing or blank normalizes to ``"accept-new"``.

    Raises ``TypeError`` for non-boolean booleans / non-integer integers and
    ``ValueError`` for out-of-range integers or an unknown host-key value.
    Numeric strings (``"10"``), ``"true"/"false"`` strings, and bools used for
    integer fields are rejected rather than coerced.
    """
    semantic: Dict[str, Any] = {}
    for field in sorted(FIELD_TO_CONFIG_KEY):
        raw_key = FIELD_TO_CONFIG_KEY[field].split(".", 1)[1]
        value = ssh.get(raw_key) if isinstance(ssh, Mapping) else None

        if field in INTEGER_RANGES:
            if value is None:
                semantic[field] = DEFAULT_SSH_OVERRIDES[field]
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"{field} must be an integer, got {type(value).__name__}"
                )
            lo, hi = INTEGER_RANGES[field]
            if not (lo <= value <= hi):
                raise ValueError(f"{field} must be between {lo} and {hi}")
            semantic[field] = value
        elif field == "strict_host_key_checking":
            if value is None or not isinstance(value, str) or not value.strip():
                semantic[field] = "accept-new"
                continue
            normalized = value.strip()
            if normalized not in VALID_HOST_KEY_VALUES:
                raise ValueError(
                    f"strict_host_key_checking must be one of "
                    f"{sorted(VALID_HOST_KEY_VALUES)}, got {value!r}"
                )
            semantic[field] = normalized
        else:  # boolean fields
            if value is None:
                semantic[field] = DEFAULT_SSH_OVERRIDES[field]
                continue
            if not isinstance(value, bool):
                raise TypeError(
                    f"{field} must be a boolean, got {type(value).__name__}"
                )
            semantic[field] = value
    return semantic


def compute_ssh_overrides_revision(semantic: Mapping[str, Any]) -> str:
    """Deterministic revision over the nine normalized semantic values.

    The revision is a hex SHA-256 prefix (12 chars) of the canonical JSON of
    the sorted field set, so two semantically equal states (including a missing
    ``strict_host_key_checking`` vs an explicit ``"accept-new"``) always share
    a revision and no-op updates keep it stable.
    """
    canonical = json.dumps(
        {k: semantic[k] for k in sorted(FIELD_TO_CONFIG_KEY)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


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
        # ``-C`` force-enables compression regardless of where it sits in argv,
        # so a connection's own ``Compression no`` could never win against it.
        # The equivalent option obeys OpenSSH's first-value-wins rule.
        overrides.extend(["-o", "Compression=yes"])

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
