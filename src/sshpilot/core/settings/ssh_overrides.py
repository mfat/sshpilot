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
import re
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Canonical field model
# ---------------------------------------------------------------------------

# Semantic public field name -> dotted persisted config key.
FIELD_TO_CONFIG_KEY: Dict[str, str] = {
    "connect_timeout": "ssh.connection_timeout",
    "strict_host_key_checking": "ssh.strict_host_key_checking",
    "compression": "ssh.compression",
    "verbosity": "ssh.verbosity",
    "debug_enabled": "ssh.debug_enabled",
    "extra_options": "ssh.extra_options",
    "address_family": "ssh.address_family",
    "legacy_algorithms": "ssh.legacy_algorithms",
}

# Fields exposed through the API model (the public surface).
EDITABLE_FIELDS = frozenset(FIELD_TO_CONFIG_KEY.keys())

# Integer fields with their documented valid ranges.
INTEGER_RANGES: Dict[str, tuple[int, int]] = {
    "connect_timeout": (0, 600),
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

# AddressFamily override. Empty means not sent (OpenSSH's ``any``); ``inet``
# is IPv4 only and ``inet6`` IPv6 only.
ADDRESS_FAMILY_UNSET = ""
EXPLICIT_ADDRESS_FAMILY_VALUES = frozenset({"inet", "inet6"})
VALID_ADDRESS_FAMILY_VALUES = EXPLICIT_ADDRESS_FAMILY_VALUES | {ADDRESS_FAMILY_UNSET}

# Tri-state OpenSSH yes/no override (Compression). Empty means
# don't inject; ``yes`` / ``no`` inject the matching ``-o`` value.
YES_NO_UNSET = ""
EXPLICIT_YES_NO_VALUES = frozenset({"yes", "no"})
VALID_YES_NO_VALUES = EXPLICIT_YES_NO_VALUES | {YES_NO_UNSET}
YES_NO_FIELDS = frozenset({"compression"})

# Canonical semantic defaults. A missing raw key normalizes to exactly these
# values, so "field absent" and "field set to its default" are equivalent for
# the snapshot and the revision token.
DEFAULT_SSH_OVERRIDES: Dict[str, Any] = {
    "connect_timeout": 0,
    "strict_host_key_checking": HOST_KEY_UNSET,
    "compression": YES_NO_UNSET,
    "verbosity": 0,
    "debug_enabled": False,
    "extra_options": [],
    "address_family": ADDRESS_FAMILY_UNSET,
    "legacy_algorithms": False,
}


# ---------------------------------------------------------------------------
# Free-form extra options
# ---------------------------------------------------------------------------

#: Upper bounds that keep a settings file from growing an argv without limit.
EXTRA_OPTIONS_MAX_COUNT = 64
EXTRA_OPTION_MAX_LENGTH = 1024

_EXTRA_OPTION_LINE = re.compile(
    r"\s*([A-Za-z][A-Za-z0-9]*)(?:\s*=\s*|\s+)(\S.*?)\s*\Z", re.DOTALL
)
# Config-file structure, not options: OpenSSH rejects them after ``-o``.
_EXTRA_NOT_OPTIONS = frozenset({"host", "match", "include"})


# Options that have their own control on the SSH options page. They are set
# there, so a free-form line for one would be a second, hidden source of truth.
EXTRA_OPTIONS_WITH_OWN_SETTING = frozenset({
    "connecttimeout",
    "addressfamily",
    "stricthostkeychecking",
    "compression",
    "serveraliveinterval",
    "serveralivecountmax",
    "controlmaster",
    "controlpath",
    "controlpersist",
    "loglevel",
})


# Options OpenSSH collects instead of keeping the first value. An override for
# one of these cannot replace what a connection sets: both end up in force (the
# override's identity is tried first, forwards and variables are merged).
EXTRA_OPTIONS_ADDED_NOT_REPLACED = frozenset({
    "identityfile",
    "certificatefile",
    "localforward",
    "remoteforward",
    "dynamicforward",
    "sendenv",
})


class ExtraOptionProblem(str, Enum):
    """Why a free-form option line was refused (stable; frontends word it)."""

    MALFORMED = "malformed"
    NOT_AN_OPTION = "not_an_option"
    HAS_OWN_SETTING = "has_own_setting"
    TOO_LONG = "too_long"
    TOO_MANY = "too_many"


class ExtraOptionError(ValueError):
    """A free-form option that cannot become exactly one ``-o`` argument."""

    def __init__(self, problem: ExtraOptionProblem, *, line: int = 0, keyword: str = ""):
        super().__init__(f"extra option {problem.value} (line {line})")
        self.problem = problem
        self.line = line
        self.keyword = keyword


def normalize_extra_option(
    text: Any,
    *,
    line: int = 0,
    reserved: frozenset = EXTRA_OPTIONS_WITH_OWN_SETTING,
) -> str:
    """Return *text* as one canonical ``Keyword=value`` option.

    Accepts the two spellings an SSH config file does (``Keyword value`` and
    ``Keyword=value``). The result is always passed to OpenSSH as the single
    argument after ``-o``, so a line can never add a flag of its own. Which
    options a user sets is their choice: only shape is checked, plus *reserved*,
    the lower-case keywords that have their own control where the option is
    being entered (by default, those of the global overrides).
    """
    if not isinstance(text, str):
        raise ExtraOptionError(ExtraOptionProblem.MALFORMED, line=line)
    if len(text) > EXTRA_OPTION_MAX_LENGTH:
        raise ExtraOptionError(ExtraOptionProblem.TOO_LONG, line=line)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ExtraOptionError(ExtraOptionProblem.MALFORMED, line=line)
    match = _EXTRA_OPTION_LINE.match(text)
    if match is None:
        raise ExtraOptionError(ExtraOptionProblem.MALFORMED, line=line)
    keyword, value = match.group(1), match.group(2)
    if keyword.lower() in _EXTRA_NOT_OPTIONS:
        raise ExtraOptionError(
            ExtraOptionProblem.NOT_AN_OPTION, line=line, keyword=keyword
        )
    if keyword.lower() in reserved:
        raise ExtraOptionError(
            ExtraOptionProblem.HAS_OWN_SETTING, line=line, keyword=keyword
        )
    return f"{keyword}={value}"


def normalize_extra_options(value: Any) -> tuple:
    """Normalize a stored/patched list of option strings to a tuple."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise TypeError(
            f"extra_options must be a list of strings, got {type(value).__name__}"
        )
    if len(value) > EXTRA_OPTIONS_MAX_COUNT:
        raise ExtraOptionError(ExtraOptionProblem.TOO_MANY)
    return tuple(
        normalize_extra_option(item, line=index)
        for index, item in enumerate(value, start=1)
    )


def coerce_yes_no_unset(value: Any) -> str:
    """Normalize the Compression setting to ``\"\"`` / ``yes`` / ``no``.

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


# ---------------------------------------------------------------------------
# Strict normalization (single conversion used by model + revision)
# ---------------------------------------------------------------------------

def normalize_ssh_overrides(ssh: Mapping[str, Any]) -> Dict[str, Any]:
    """Strictly convert a raw ``ssh`` subtree to normalized semantic values.

    Returns exactly the public field names with canonical values. Missing
    keys normalize to :data:`DEFAULT_SSH_OVERRIDES`; ``strict_host_key_checking``
    and the yes/no fields missing or blank normalize to unset.

    Raises ``TypeError`` for wrong types and ``ValueError`` for out-of-range
    integers or unknown enum values. Numeric strings (``"10"``), ``"true"/"false"``
    strings for integers, and bools used for integer fields are rejected rather
    than coerced. Legacy booleans for Compression are accepted and
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
        elif field == "extra_options":
            semantic[field] = list(normalize_extra_options(value))
        elif field == "address_family":
            if value is None or not isinstance(value, str) or not value.strip():
                semantic[field] = ADDRESS_FAMILY_UNSET
                continue
            normalized = value.strip().lower()
            if normalized not in EXPLICIT_ADDRESS_FAMILY_VALUES:
                raise ValueError(
                    f"address_family must be one of "
                    f"{sorted(EXPLICIT_ADDRESS_FAMILY_VALUES)} or empty, got {value!r}"
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
    """Deterministic revision over the normalized semantic values.

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
    legacy_extra: Optional[Sequence[str]] = None,
) -> List[str]:
    """Build the flat ``ssh.ssh_overrides`` argv fragment from SSH settings.

    *legacy_extra* is the ``-o`` fragment that re-enables old algorithms,
    supplied by the caller (``daemon.legacy_algorithms.legacy_algorithm_args()``)
    because it depends on what the installed OpenSSH still supports.

    Mirrors Preferences ▸ SSH Settings persistence. *controlmaster_extra* is
    supplied by the caller (typically ``ssh_multiplex.controlmaster_args()``)
    when ``ssh.controlmaster`` is enabled — core does not import multiplex.
    """
    overrides: List[str] = []

    connect_timeout = ssh_cfg.get("connection_timeout")
    if connect_timeout is not None:
        try:
            ct = int(connect_timeout)
        except (TypeError, ValueError):
            ct = 0
        if ct > 0:
            overrides.extend(["-o", f"ConnectTimeout={ct}"])

    address_family = str(ssh_cfg.get("address_family") or "").strip().lower()
    if address_family in EXPLICIT_ADDRESS_FAMILY_VALUES:
        overrides.extend(["-o", f"AddressFamily={address_family}"])

    strict_host_value = str(ssh_cfg.get("strict_host_key_checking") or "").strip()
    if strict_host_value:
        overrides.extend(["-o", f"StrictHostKeyChecking={strict_host_value}"])

    try:
        compression = coerce_yes_no_unset(ssh_cfg.get("compression"))
    except (TypeError, ValueError):
        compression = YES_NO_UNSET
    if compression:
        # ``-C`` can only switch compression on; the option spelling can also
        # force it off, and obeys OpenSSH's first-value-wins rule.
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

    if ssh_cfg.get("legacy_algorithms") is True and legacy_extra:
        overrides.extend(list(legacy_extra))

    # Free-form options go last within this fragment. OpenSSH keeps the first
    # value it sees, so an option that also has a control on the page (or that
    # the app manages, like multiplexing) keeps the value set there. The whole
    # fragment is emitted ahead of a connection's own options, so every entry
    # still overrides those. A line that is not a
    # well-formed option is skipped: one bad entry in a hand-edited file must
    # not turn into a stray argument or stop every connection.
    #
    # SetEnv is the exception to one row, one argument: OpenSSH keeps only the
    # first ``-o SetEnv=`` and discards the rest, so several rows are joined
    # into the single argument that carries every assignment.
    extra = ssh_cfg.get("extra_options")
    if isinstance(extra, (list, tuple)):
        setenv_at: Optional[int] = None
        for index, item in enumerate(extra[:EXTRA_OPTIONS_MAX_COUNT], start=1):
            try:
                option = normalize_extra_option(item, line=index)
            except ExtraOptionError:
                continue
            keyword, _sep, value = option.partition("=")
            if keyword.lower() == "setenv":
                if setenv_at is None:
                    overrides.extend(["-o", f"SetEnv={value}"])
                    setenv_at = len(overrides) - 1
                else:
                    overrides[setenv_at] += f" {value}"
                continue
            overrides.extend(["-o", option])

    return overrides


def select_override_args(
    overrides: Any,
    *,
    drop: frozenset = frozenset(),
    flags: bool = True,
) -> List[str]:
    """A composed override fragment minus the options an operation owns.

    *drop* holds lower-case keywords; every ``-o Keyword=value`` pair for one
    of them is left out. With ``flags=False`` only ``-o`` pairs survive, for
    consumers that cannot take bare flags such as ``-v`` (``ssh -G``,
    ``ssh-copy-id``). Anything that is not a flag or a complete pair is
    dropped, so the result can never contribute a stray argument.
    """
    selected: List[str] = []
    if not isinstance(overrides, (list, tuple)):
        return selected
    entries = [str(entry) for entry in overrides if entry]
    index = 0
    while index < len(entries):
        entry = entries[index]
        if entry == "-o":
            value = entries[index + 1] if index + 1 < len(entries) else ""
            index += 2
            keyword, separator, _rest = value.partition("=")
            if not separator or not keyword or value.startswith("-"):
                continue
            if keyword.strip().lower() in drop:
                continue
            selected.extend(["-o", value])
            continue
        index += 1
        if flags and re.fullmatch(r"-[A-Za-z0-9]+", entry):
            selected.append(entry)
    return selected


def forced_options(overrides: Any) -> Dict[str, str]:
    """Every option a composed override fragment forces: keyword → value.

    Keys are lower-case. OpenSSH keeps the first value it obtains, so the
    first ``-o`` pair for a keyword is the one in force. The mapping keeps
    the order in which keywords first appear: options that compete across
    keywords (``ProxyJump``/``ProxyCommand``) are decided by it. Options OpenSSH
    collects (:data:`EXTRA_OPTIONS_ADDED_NOT_REPLACED`) are included with
    their first value; whether a connection's own value survives next to them
    is for the caller to tell from that set.
    """
    forced: Dict[str, str] = {}
    args = select_override_args(overrides, flags=False)
    for value in args[1::2]:
        name, _separator, rest = value.partition("=")
        forced.setdefault(name.strip().lower(), rest.strip())
    return forced


def forced_option(overrides: Any, keyword: str) -> Optional[str]:
    """The value a composed override fragment forces for *keyword*, if any."""
    return forced_options(overrides).get(keyword.lower())


def keepalive_fallback_args(
    ssh_cfg: Mapping[str, Any], effective: Optional[Mapping[str, Any]]
) -> List[str]:
    """The ``-o ServerAlive*`` pairs a launch adds when the config sets none.

    The app's keepalive (Settings ▸ SSH Options ▸ Keepalive) is a fallback,
    not an override: a command-line option beats every configuration file, so
    sending it unconditionally would replace a ``ServerAliveInterval`` the
    user wrote in ``Host *`` or in the host's own block.

    *effective* is what OpenSSH resolved for the host (``ssh -G``), without
    this fallback. Nothing is returned when

    * the switch (``apply_default_keepalive``) is off,
    * *effective* is empty (nothing is known, so nothing is imposed), or
    * it resolves a ``ServerAliveInterval`` other than 0, OpenSSH's "unset".

    ``ServerAliveCountMax`` is added only while the resolved one is OpenSSH's
    default (3), so a count the user configured is kept.
    """
    if not bool(ssh_cfg.get("apply_default_keepalive", True)):
        return []
    if not effective:
        return []

    def _resolved(keyword: str) -> str:
        value = effective.get(keyword)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        return str(value or "").strip()

    if _resolved("serveraliveinterval") not in ("", "0"):
        return []
    interval = int(ssh_cfg.get("default_keepalive_interval", 15) or 15)
    count = int(ssh_cfg.get("default_keepalive_count", 3) or 3)
    args: List[str] = []
    if interval > 0:
        args.extend(["-o", f"ServerAliveInterval={interval}"])
        if count > 0 and _resolved("serveralivecountmax") in ("", "3"):
            args.extend(["-o", f"ServerAliveCountMax={count}"])
    return args


def forced_destination(
    overrides: Any,
    *,
    hostname: str,
    username: str,
    port: int,
    alias: Optional[str] = None,
) -> tuple:
    """``(hostname, username, port)`` after the overrides that replace them.

    For callers that describe where a launch goes without running ``ssh -G``.
    ``HostName`` may use ``%h`` and ``%%``. ``%h`` is the name ssh is given,
    which is *alias* for a saved Host block (default: *hostname*); a value with any
    other token, or a ``Port`` that is not a port, is left to OpenSSH and the
    given value is kept.
    """
    forced_host = forced_option(overrides, "hostname")
    if forced_host:
        expanded = forced_host.replace("%%", "\0").replace("%h", alias or hostname)
        if "%" not in expanded:
            hostname = expanded.replace("\0", "%")
    forced_user = forced_option(overrides, "user")
    if forced_user and "%" not in forced_user:
        username = forced_user
    forced_port = forced_option(overrides, "port")
    if forced_port and forced_port.isdigit() and 1 <= int(forced_port) <= 65535:
        port = int(forced_port)
    return hostname, username, port


def ssh_settings_from_values(
    *,
    connect_timeout: Optional[int] = None,
    strict_host_key_checking: str = HOST_KEY_UNSET,
    compression: str = YES_NO_UNSET,
    verbosity: int = 0,
    debug_enabled: bool = False,
    controlmaster: bool = False,
) -> Dict[str, Any]:
    """Build an ssh settings fragment from typed values (for tests / CLI)."""
    return {
        "connection_timeout": connect_timeout,
        "strict_host_key_checking": strict_host_key_checking,
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
        'connection_timeout',
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
