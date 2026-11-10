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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

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

# Unset (empty string): no ``StrictHostKeyChecking`` option is sent, so OpenSSH
# resolves it from the user's SSH configuration (and falls back to its own
# default, ``ask``). Persisted as the empty string, which builds older than
# this value already read as "no explicit choice".
HOST_KEY_UNSET = ""

# Explicit host-key-checking values accepted by OpenSSH.
EXPLICIT_HOST_KEY_VALUES = frozenset({"accept-new", "yes", "no", "ask"})

# Every value the setting may hold: an explicit OpenSSH value, or unset.
VALID_HOST_KEY_VALUES = EXPLICIT_HOST_KEY_VALUES | {HOST_KEY_UNSET}

# Tri-state OpenSSH yes/no overrides (BatchMode, Compression). Empty means
# don't inject; ``yes`` / ``no`` inject the matching ``-o`` value.
YES_NO_UNSET = ""
EXPLICIT_YES_NO_VALUES = frozenset({"yes", "no"})
VALID_YES_NO_VALUES = EXPLICIT_YES_NO_VALUES | {YES_NO_UNSET}
YES_NO_FIELDS = frozenset({"batch_mode", "compression"})

# Canonical semantic defaults. A missing raw key normalizes to exactly these
# values, so "field absent" and "field set to its default" are equivalent for
# the snapshot and the revision token.
DEFAULT_SSH_OVERRIDES: Dict[str, Any] = {
    "connect_timeout": 0,
    "connection_attempts": 0,
    "server_alive_interval": 0,
    "server_alive_count_max": 0,
    "strict_host_key_checking": HOST_KEY_UNSET,
    "batch_mode": YES_NO_UNSET,
    "compression": YES_NO_UNSET,
    "verbosity": 0,
    "debug_enabled": False,
}


def coerce_yes_no_unset(value: Any) -> str:
    """Normalize a BatchMode/Compression setting to ``\"\"`` / ``yes`` / ``no``.

    Legacy booleans are accepted: ``True`` → ``yes``, ``False`` → unset.
    """
    if value is None:
        return YES_NO_UNSET
    if value is True:
        return "yes"
    if value is False:
        return YES_NO_UNSET
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return YES_NO_UNSET
        if normalized in EXPLICIT_YES_NO_VALUES:
            return normalized
        raise ValueError(
            f"must be one of {sorted(EXPLICIT_YES_NO_VALUES)} or empty, "
            f"got {value!r}"
        )
    raise TypeError(
        f"must be '', 'yes', 'no', or a legacy boolean, got {type(value).__name__}"
    )


def is_yes_no_enabled(value: Any) -> bool:
    """True when the setting injects ``=yes`` (legacy ``True`` counts)."""
    try:
        return coerce_yes_no_unset(value) == "yes"
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Strict normalization (single conversion used by model + revision)
# ---------------------------------------------------------------------------

def normalize_ssh_overrides(ssh: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly convert a raw ``ssh`` subtree to normalized semantic values.

    Returns exactly the nine public field names with canonical values. Missing
    keys normalize to :data:`DEFAULT_SSH_OVERRIDES`; ``strict_host_key_checking``
    and the yes/no fields missing or blank normalize to unset.

    Raises ``TypeError`` for wrong types and ``ValueError`` for out-of-range
    integers or unknown enum values. Numeric strings (``"10"``), ``"true"/"false"``
    strings for integers, and bools used for integer fields are rejected rather
    than coerced. Legacy booleans for BatchMode/Compression are accepted and
    mapped to ``yes`` / unset.
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
                semantic[field] = HOST_KEY_UNSET
                continue
            normalized = value.strip()
            if normalized not in EXPLICIT_HOST_KEY_VALUES:
                raise ValueError(
                    f"strict_host_key_checking must be one of "
                    f"{sorted(EXPLICIT_HOST_KEY_VALUES)} or empty, got {value!r}"
                )
            semantic[field] = normalized
        elif field in YES_NO_FIELDS:
            if value is None:
                semantic[field] = DEFAULT_SSH_OVERRIDES[field]
                continue
            try:
                semantic[field] = coerce_yes_no_unset(value)
            except TypeError as exc:
                raise TypeError(f"{field} {exc}") from exc
            except ValueError as exc:
                raise ValueError(f"{field} {exc}") from exc
        else:  # remaining boolean fields (debug_enabled)
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
    ``strict_host_key_checking`` vs an explicit empty one) always share
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

    try:
        batch_mode = coerce_yes_no_unset(ssh_cfg.get("batch_mode"))
    except (TypeError, ValueError):
        batch_mode = YES_NO_UNSET
    if batch_mode:
        overrides.extend(["-o", f"BatchMode={batch_mode}"])

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

    try:
        compression = coerce_yes_no_unset(ssh_cfg.get("compression"))
    except (TypeError, ValueError):
        compression = YES_NO_UNSET
    if compression:
        # ``-C`` force-enables compression regardless of where it sits in argv,
        # so a connection's own ``Compression no`` could never win against it.
        # The equivalent option obeys OpenSSH's first-value-wins rule.
        overrides.extend(["-o", f"Compression={compression}"])

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
    strict_host_key_checking: str = HOST_KEY_UNSET,
    batch_mode: str = YES_NO_UNSET,
    compression: str = YES_NO_UNSET,
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
        "batch_mode": coerce_yes_no_unset(batch_mode),
        "compression": coerce_yes_no_unset(compression),
        "verbosity": verbosity,
        "debug_enabled": debug_enabled,
        "controlmaster": controlmaster,
    }


# ---------------------------------------------------------------------------
# Effective SSH settings for command builders
# ---------------------------------------------------------------------------


def ssh_config_from_settings(get_setting: Callable[[str, Any], Any]) -> Dict[str, Any]:
    """Return the effective ``ssh.*`` settings, coerced and defaulted.

    ``get_setting`` is a ``(dotted_key, default) -> value`` reader over the app
    settings. Every consumer of app-level SSH preferences (the GTK ``Config``
    and the daemon's headless settings shim) goes through here, so a builder
    sees the same dict whichever side of the daemon boundary it runs on.
    """

    defaults: Dict[str, Any] = {
        'auto_add_host_keys': True,
        'batch_mode': YES_NO_UNSET,
        'compression': YES_NO_UNSET,
        'debug_enabled': False,
        'strict_host_key_checking': HOST_KEY_UNSET,
        'use_isolated_config': False,
        'verbosity': 0,
        'ssh_overrides': [],
        'apply_default_keepalive': True,
        'default_keepalive_interval': 15,
        'default_keepalive_count': 3,
    }

    optional_int_keys = {
        'connection_attempts',
        'connection_timeout',
        'keepalive_count_max',
        'keepalive_interval',
    }

    # Internal keepalive defaults applied when the user hasn't set their own
    # keepalive. Always present (non-optional) so the builder can rely on them.
    positive_int_keys = {
        'default_keepalive_interval',
        'default_keepalive_count',
    }

    bool_keys = {
        'auto_add_host_keys',
        'debug_enabled',
        'use_isolated_config',
        'apply_default_keepalive',
    }

    yes_no_keys = YES_NO_FIELDS

    config: Dict[str, Any] = {}

    for key, default_value in defaults.items():
        value = get_setting(f'ssh.{key}', default_value)

        if key in bool_keys:
            if isinstance(value, bool):
                pass
            elif isinstance(value, str):
                lowered = value.strip().lower()
                value = lowered in {'1', 'true', 'yes', 'on'}
            else:
                value = bool(value)
        elif key in yes_no_keys:
            try:
                value = coerce_yes_no_unset(value)
            except (TypeError, ValueError):
                value = default_value
        elif key in optional_int_keys:
            # handled separately after defaults loop
            pass
        elif key in positive_int_keys:
            try:
                coerced_int = int(value)
            except (TypeError, ValueError):
                coerced_int = int(default_value)
            value = coerced_int if coerced_int > 0 else int(default_value)
        elif key == 'strict_host_key_checking':
            if value is None:
                value = default_value
            else:
                strict_value = str(value).strip()
                if not strict_value:
                    value = ''
                else:
                    normalized = strict_value.lower()
                    if normalized in {'accept-new', 'yes', 'no', 'ask'}:
                        value = 'accept-new' if normalized == 'accept-new' else normalized
                    else:
                        value = default_value
        elif key == 'ssh_overrides':
            if isinstance(value, (list, tuple)):
                coerced: List[str] = []
                for entry in value:
                    if entry is None:
                        continue
                    coerced.append(str(entry))
                value = coerced
            else:
                value = []

        config[key] = value

    for key in optional_int_keys:
        raw_value = get_setting(f'ssh.{key}', None)
        if raw_value in (None, ''):
            config[key] = None
            continue
        try:
            coerced = int(raw_value)
        except (TypeError, ValueError):
            config[key] = None
            continue
        if coerced <= 0:
            config[key] = None
        else:
            config[key] = coerced

    # verbosity remains treated as integer, defaulting to 0 when unset
    verbosity_value = get_setting('ssh.verbosity', defaults['verbosity'])
    try:
        config['verbosity'] = int(verbosity_value)
    except (TypeError, ValueError):
        config['verbosity'] = defaults['verbosity']

    return config
