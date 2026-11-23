"""Login profile value objects and pure connection-data helpers (GTK-free).

A :class:`LoginProfile` is a named, reusable bundle of authentication settings
(username, key selection, agent/hardware options, an optional ProxyCommand,
extra directives) that can be linked to any number of SSH connections,
directly or through a group.

The daemon resolves a linked profile *into* the connection's ``Host`` block,
so plain ``ssh`` keeps working; the link itself is sshPilot metadata. Field
names deliberately match the connection payload consumed by
:func:`sshpilot.ssh_config_formatter.format_ssh_config_entry`, so applying a
profile is an overlay onto the connection's existing data.

No secrets live here: only ``has_*`` flags. Secret values are stored in the
active secret backend under :func:`profile_secret_host`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ...ssh_config_document import _split_config_option, split_config_lines
from ..connections.auth_preferences import (
    apply_legacy_auth_choice,
    password_comes_first,
    preferred_order,
    pubkey_disabled,
    split_option,
)

# Connection metadata key holding the profile link. Its value is one of:
#   {"mode": "explicit", "id": <profile id>, "applied": <fingerprint>, "rev": <id:revision>}
#   {"mode": "inherit", "applied": <fingerprint>, "rev": <id:revision>}
# ``applied`` fingerprints the Host block as last written by sshPilot (drift
# detection); ``rev`` names the profile revision it was rendered from, so a
# profile edit or a group move is re-applied rather than mistaken for drift.
# An absent key means the connection uses its own (custom) settings.
LINK_METADATA_KEY = "login_profile"
LINK_EXPLICIT = "explicit"
LINK_INHERIT = "inherit"

AUTH_KEY = 0
AUTH_PASSWORD = 1
_UNSET_AWARE = "unset_aware"
# Stored beside ``forward_agent`` (a plain boolean, for versions that read
# nothing else): True when the profile sets "don't forward", False when it
# leaves forwarding unset.
_FORWARD_AGENT_NO = "forward_agent_explicit_no"

# What the Key/Password choice hid, and never wrote, in password mode.
LEGACY_PASSWORD_MODE_UNSET = {
    "key_select_mode": None,
    "identity_files": (),
    "certificate_files": (),
    "identity_agent": "",
    "add_keys_to_agent": "",
    "pkcs11_provider": "",
    "security_key_provider": "",
}

_ID_PREFIX = "lp-"
_ID_RE = re.compile(r"^lp-[0-9a-f]{16}$")
_MAX_NAME = 128
_VALID_ADD_KEYS = {"", "yes", "no", "ask", "confirm"}


def new_profile_id() -> str:
    return f"{_ID_PREFIX}{secrets.token_hex(8)}"


def is_profile_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def revision_token(profile: "LoginProfile") -> str:
    return f"{profile.id}:{profile.revision}"


PROFILE_SECRET_HOST_PREFIX = "sshpilot-login-profile/"


def profile_id_from_secret_host(host: object) -> Optional[str]:
    """Profile id for a :func:`profile_secret_host` value, else ``None``."""
    if isinstance(host, str) and host.startswith(PROFILE_SECRET_HOST_PREFIX):
        candidate = host[len(PROFILE_SECRET_HOST_PREFIX):]
        return candidate if is_profile_id(candidate) else None
    return None


def profile_secret_host(profile_id: str) -> str:
    """Pseudo host under which a profile's secrets are keyed.

    Mirrors the plugin-secret convention (``sshpilot-plugin/<id>``) so every
    secret backend, backup, and the Credential Manager handle it unchanged.
    """
    return f"{PROFILE_SECRET_HOST_PREFIX}{profile_id}"


# Secret "usernames" under :func:`profile_secret_host`.
PROFILE_PASSWORD_ACCOUNT = "login"
PROFILE_SUDO_ACCOUNT = "sudo"


# Stable reasons for rejected profile values and requests (error details
# ``reason``); the daemon forwards them as ``LoginProfileErrorReason``.
REASON_PROFILE_NOT_FOUND = "profile_not_found"
REASON_NAME_EXISTS = "name_exists"
REASON_NAME_EMPTY = "name_empty"
REASON_NAME_TOO_LONG = "name_too_long"
REASON_USERNAME_WHITESPACE = "username_whitespace"
REASON_EXTRA_FORBIDDEN_BLOCK = "extra_forbidden_block"
REASON_EXTRA_MANAGED_OPTION = "extra_managed_option"
REASON_NOT_SSH_CONNECTION = "not_ssh_connection"
REASON_GROUP_NOT_FOUND = "group_not_found"
REASON_REPLACEMENT_INVALID = "replacement_invalid"
REASON_BACKUP_INVALID = "backup_invalid"
REASON_PROFILES_UNAVAILABLE = "profiles_unavailable"
REASON_SECRET_STORAGE_UNAVAILABLE = "secret_storage_unavailable"
REASON_INVALID_VALUE = "invalid_value"


class LoginProfileValueError(ValueError):
    """A rejected profile value with a stable ``reason`` and its parameters."""

    def __init__(self, message: str, reason: str, **details: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = dict(details)


def _clean_text(value: Any, name: str, *, single_line: bool = True) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    if single_line and ("\n" in value or "\r" in value):
        raise ValueError(f"{name} must be a single line")
    return value.strip()


def _clean_paths(values: Any, name: str) -> Tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{name} must be a list of paths")
    cleaned: List[str] = []
    for item in values:
        text = _clean_text(item, name)
        if text and text not in cleaned:
            cleaned.append(text)
    return tuple(cleaned)


def _clean_proxy_command(value: Any) -> str:
    """A single ProxyCommand value, or ``""`` when the profile leaves it alone."""
    return _clean_text(value, "proxy_command")


def _partition_proxy_command(extra: str) -> Tuple[str, str, bool]:
    """Split ``ProxyCommand`` lines out of extra text.

    Returns ``(first command, remaining text, found)``. OpenSSH uses the first
    value, so later commands are dropped. A bare ``ProxyCommand`` is refused.
    """
    commands: List[str] = []
    kept: List[str] = []
    found = False
    for line in split_config_lines(extra or ""):
        if _extra_keyword(line) != "proxycommand":
            if line.strip():
                kept.append(line)
            continue
        found = True
        _key, value = _split_config_option(line)
        if not value:
            raise ValueError("ProxyCommand requires a value")
        commands.append(_clean_proxy_command(value))
    return (commands[0] if commands else ""), "\n".join(kept), found


def _drop_keyword(extra: str, keyword: str) -> str:
    return "\n".join(
        line
        for line in split_config_lines(extra or "")
        if line.strip() and _extra_keyword(line) != keyword
    )


def _extra_keyword(line: str) -> str:
    return split_option(line)[0]


def _clean_extra(value: Any) -> str:
    text = _clean_text(value, "extra_ssh_config", single_line=False)
    lines = [line.strip() for line in text.splitlines()]
    from ...ssh_config_formatter import MANAGED_HOST_OPTIONS

    for line in lines:
        keyword = _extra_keyword(line)
        if keyword in ("host", "match", "include"):
            raise LoginProfileValueError(
                "extra_ssh_config must not contain Host, Match or Include lines",
                REASON_EXTRA_FORBIDDEN_BLOCK,
            )
        if keyword in MANAGED_HOST_OPTIONS:
            raise LoginProfileValueError(
                f"{keyword} has a dedicated field and cannot be set in extra_ssh_config",
                REASON_EXTRA_MANAGED_OPTION,
                keyword=keyword,
            )
    return "\n".join(line for line in lines if line and not line.startswith("#"))


def legacy_key_select_mode(profile: "LoginProfile") -> int:
    """The key selection mode a version before ``identities_only`` reads."""
    if profile.identities_only is True:
        return 1
    return 2 if profile.identity_files else 0


def without_legacy_defaults(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Profile values from before fields could be left unset, read as meant.

    Those versions had no "not set": Automatic key selection and "don't
    forward the agent" were the form's defaults, and password mode hid the
    key settings without clearing them (and never wrote them to a host). All
    of these read as not set, so a linked connection keeps its own.
    Automatic with a stale key list stays Automatic, which drops the list
    (those keys were never written either).
    """
    out = dict(values)
    if out.get("auth_method") == AUTH_PASSWORD:
        out.update(LEGACY_PASSWORD_MODE_UNSET)
    elif out.get("key_select_mode") == 0 and not out.get("identity_files"):
        out["key_select_mode"] = None
    if out.get("forward_agent") is False:
        out["forward_agent"] = None
    return out


@dataclass(frozen=True)
class LoginProfile:
    """One reusable login profile. Immutable; use :meth:`with_changes`."""

    id: str
    name: str
    username: str = ""
    auth_method: int = AUTH_KEY
    # Legacy input only: the key selection mode of profiles stored before
    # IdentitiesOnly was its own setting. __post_init__ turns it into
    # ``identities_only`` and clears it.
    key_select_mode: Optional[int] = None
    identity_files: Tuple[str, ...] = ()
    certificate_files: Tuple[str, ...] = ()
    identity_agent: str = ""
    add_keys_to_agent: str = ""
    pkcs11_provider: str = ""
    security_key_provider: str = ""
    pubkey_auth_no: bool = False
    forward_agent: Optional[bool] = None  # None = not set
    forward_agent_target: str = ""
    proxy_command: str = ""
    extra_ssh_config: str = ""
    has_password: bool = False
    has_sudo_password: bool = False
    revision: int = 1
    # IdentitiesOnly, as in the connection editor. None = not set: a linked
    # connection keeps its own value.
    identities_only: Optional[bool] = None

    def __post_init__(self) -> None:
        if not is_profile_id(self.id):
            raise ValueError("login profile id is invalid")
        name = _clean_text(self.name, "name")
        if not name:
            raise LoginProfileValueError(
                "login profile name must not be empty", REASON_NAME_EMPTY
            )
        if len(name) > _MAX_NAME:
            raise LoginProfileValueError(
                "login profile name is too long", REASON_NAME_TOO_LONG
            )
        object.__setattr__(self, "name", name)
        username = _clean_text(self.username, "username")
        if any(ch.isspace() for ch in username):
            raise LoginProfileValueError(
                "username must not contain whitespace", REASON_USERNAME_WHITESPACE
            )
        object.__setattr__(self, "username", username)
        if self.auth_method not in (AUTH_KEY, AUTH_PASSWORD):
            raise ValueError("auth_method must be 0 (key) or 1 (password)")
        if self.key_select_mode is not None and self.key_select_mode not in (0, 1, 2):
            raise ValueError("key_select_mode must be 0, 1, 2 or None")
        if self.identities_only is not None and type(self.identities_only) is not bool:
            raise TypeError("identities_only must be a boolean or None")
        if self.key_select_mode is not None:
            # A stored key selection mode: "specific keys only" was
            # IdentitiesOnly yes; Automatic never wrote the keys it listed,
            # so they go; the other modes leave IdentitiesOnly unset.
            if self.identities_only is None and self.key_select_mode == 1:
                object.__setattr__(self, "identities_only", True)
            if self.key_select_mode == 0:
                object.__setattr__(self, "identity_files", ())
            object.__setattr__(self, "key_select_mode", None)
        if self.forward_agent is not None and type(self.forward_agent) is not bool:
            raise TypeError("forward_agent must be a boolean or None")
        object.__setattr__(
            self, "identity_files", _clean_paths(self.identity_files, "identity_files")
        )
        object.__setattr__(
            self,
            "certificate_files",
            _clean_paths(self.certificate_files, "certificate_files"),
        )
        for name_ in (
            "identity_agent",
            "pkcs11_provider",
            "security_key_provider",
            "forward_agent_target",
        ):
            object.__setattr__(self, name_, _clean_text(getattr(self, name_), name_))
        object.__setattr__(self, "proxy_command", _clean_proxy_command(self.proxy_command))
        add_keys = _clean_text(self.add_keys_to_agent, "add_keys_to_agent").lower()
        if add_keys not in _VALID_ADD_KEYS and not re.fullmatch(
            r"(yes|no|ask|confirm)?\s*\d+[smhdw]?", add_keys
        ):
            raise ValueError("add_keys_to_agent is invalid")
        object.__setattr__(self, "add_keys_to_agent", add_keys)
        # The authentication order and switches are plain lines of the
        # profile's options, as in a Host block. The older Key/Password choice
        # and "Disable public key authentication" become those lines here.
        extra = self.extra_ssh_config
        if self.auth_method == AUTH_PASSWORD or self.pubkey_auth_no is True:
            extra = apply_legacy_auth_choice(
                str(extra or ""),
                auth_method=AUTH_PASSWORD if self.auth_method == AUTH_PASSWORD else None,
                pubkey_auth_no=True if self.pubkey_auth_no is True else None,
            )
            object.__setattr__(self, "auth_method", AUTH_KEY)
            object.__setattr__(self, "pubkey_auth_no", False)
        object.__setattr__(self, "extra_ssh_config", _clean_extra(extra))
        for flag in ("pubkey_auth_no", "has_password", "has_sudo_password"):
            if type(getattr(self, flag)) is not bool:
                raise TypeError(f"{flag} must be a boolean")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "username": self.username,
            "auth_method": self.auth_method,
            # For versions that read the key selection mode.
            "key_select_mode": legacy_key_select_mode(self),
            "identities_only": self.identities_only,
            "identity_files": list(self.identity_files),
            "certificate_files": list(self.certificate_files),
            "identity_agent": self.identity_agent,
            "add_keys_to_agent": self.add_keys_to_agent,
            "pkcs11_provider": self.pkcs11_provider,
            "security_key_provider": self.security_key_provider,
            "pubkey_auth_no": self.pubkey_auth_no,
            # A boolean, for versions that accept nothing else; the flag
            # after it tells "don't forward" from "not set".
            "forward_agent": bool(self.forward_agent),
            _FORWARD_AGENT_NO: self.forward_agent is False,
            "forward_agent_target": self.forward_agent_target,
            "proxy_command": self.proxy_command,
            "extra_ssh_config": self.extra_ssh_config,
            "has_password": self.has_password,
            "has_sudo_password": self.has_sudo_password,
            "revision": self.revision,
            # Written by versions where an empty field means "not set".
            _UNSET_AWARE: True,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LoginProfile":
        if not isinstance(data, Mapping):
            raise TypeError("login profile must be an object")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        values = {k: v for k, v in data.items() if k in known}
        if "identities_only" in data:
            # Written by a version with IdentitiesOnly of its own; the mode
            # beside it is only for older readers.
            values.pop("key_select_mode", None)
        for key in ("identity_files", "certificate_files"):
            if key in values and isinstance(values[key], list):
                values[key] = tuple(values[key])
        if not data.get(_UNSET_AWARE):
            values = without_legacy_defaults(values)
        elif _FORWARD_AGENT_NO in data and values.get("forward_agent") is False:
            # (Without the flag the stored value is already None/True/False.)
            if not data[_FORWARD_AGENT_NO]:
                values["forward_agent"] = None
        # Profiles saved while ProxyCommand lived in extra text keep working:
        # the first command becomes the dedicated field.
        extra = values.get("extra_ssh_config")
        if isinstance(extra, str) and extra:
            command, cleaned, found = _partition_proxy_command(extra)
            if found:
                values["extra_ssh_config"] = cleaned
                current = values.get("proxy_command")
                if command and not (isinstance(current, str) and current.strip()):
                    values["proxy_command"] = command
        return cls(**values)

    def with_changes(self, **changes: Any) -> "LoginProfile":
        return replace(self, **changes)

    # -- editable fields ------------------------------------------------------

    def settings(self) -> Dict[str, Any]:
        """Editable, non-identity fields (what an update request may change)."""
        data = self.to_dict()
        for key in ("id", "revision", "has_password", "has_sudo_password",
                    _UNSET_AWARE, _FORWARD_AGENT_NO):
            data.pop(key, None)
        # Legacy input only (see the field); never read back as a setting.
        data["key_select_mode"] = None
        data["forward_agent"] = self.forward_agent
        return data


EDITABLE_FIELDS = frozenset(
    LoginProfile(id="lp-0000000000000000", name="x").settings().keys()
)


# ---------------------------------------------------------------------------
# Applying a profile to connection data
# ---------------------------------------------------------------------------

def _merge_extra(connection_extra: str, profile_extra: str) -> str:
    """Profile extra directives replace same-keyword lines of the connection."""
    profile_lines = [line for line in split_config_lines(profile_extra or "") if line.strip()]
    owned = set(extra_keywords(profile_extra))
    kept = [
        line
        for line in split_config_lines(connection_extra or "")
        if line.strip() and _extra_keyword(line) not in owned
    ]
    return "\n".join(kept + profile_lines)


def apply_profile_to_data(data: Mapping[str, Any], profile: LoginProfile) -> Dict[str, Any]:
    """Return a copy of connection *data* with *profile*'s settings overlaid.

    The result is a full update payload for ``ConnectionRepository.update_connection``.
    """
    out = dict(data)
    fields = profile_owned_fields(profile)
    if "username" in fields:
        out["username"] = profile.username
        authored = {
            str(name).strip().lower()
            for name in (out.get("__authored_directives") or ())
            if str(name).strip()
        }
        authored.add("user")
        out["__authored_directives"] = tuple(sorted(authored))
    if "identity_files" in fields:
        out["identity_files"] = list(profile.identity_files)
        out["keyfile"] = profile.identity_files[0]
        out["identity_file_none"] = False
    # The stored key mode is derived as the connection editor derives it:
    # 1 = IdentitiesOnly yes, 2 = IdentityFile lines, 0 = neither. A profile
    # that sets only one of the two leaves the connection's other one alone.
    if "identities_only" in fields or "identity_files" in fields:
        current = int(out.get("key_select_mode", 0) or 0)
        if "identities_only" in fields:
            identities_only_yes = profile.identities_only is True
            out["identities_only_explicit_no"] = profile.identities_only is False
        else:
            identities_only_yes = current == 1
        has_files = bool(_norm_list(out.get("identity_files"))) and (
            "identity_files" in fields or current in (1, 2)
        )
        out["key_select_mode"] = 1 if identities_only_yes else (2 if has_files else 0)
    if "certificate_files" in fields:
        out["certificate_files"] = list(profile.certificate_files)
        out["certificate"] = profile.certificate_files[0]
    for name in ("identity_agent", "add_keys_to_agent", "pkcs11_provider",
                 "security_key_provider"):
        if name in fields:
            out[name] = getattr(profile, name)
    if "forward_agent" in fields:
        out["forward_agent"] = bool(profile.forward_agent)
        out["forward_agent_explicit_no"] = profile.forward_agent is False
        out["forward_agent_target"] = (
            profile.forward_agent_target if profile.forward_agent else ""
        )
    # Authentication lines are options like any other: those the profile
    # sets replace the connection's own; what it leaves unset stays.
    out["extra_ssh_config"] = _merge_extra(
        str(out.get("extra_ssh_config") or ""), profile.extra_ssh_config
    )
    # The editor's older Key/Password view of the result.
    order = preferred_order(out["extra_ssh_config"])
    pubkey_no = pubkey_disabled(out["extra_ssh_config"])
    out["auth_method"] = AUTH_PASSWORD if (pubkey_no or password_comes_first(order)) else AUTH_KEY
    out["pubkey_auth_no"] = pubkey_no
    out["preferred_authentications"] = order
    # An empty profile command leaves the connection's ProxyCommand alone.
    # A set command owns the field and drops any extra copy: OpenSSH uses the
    # first occurrence, and the formatter emits the field itself.
    if profile.proxy_command:
        out["proxy_command"] = profile.proxy_command
        out["extra_ssh_config"] = _drop_keyword(out["extra_ssh_config"], "proxycommand")
    return out


def _norm_str(value: Any) -> str:
    return str(value or "").strip()


def _norm_list(values: Any) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return [str(v).strip() for v in values if str(v).strip()]


#: Profile fields a link can own, in display order. Each is owned only when
#: the profile sets it; the connection keeps its own value otherwise.
#: (Links recorded before IdentitiesOnly was its own setting may list
#: ``key_select_mode``; their fingerprint is recomputed from what they list.)
PROFILE_FIELDS: Tuple[str, ...] = (
    "username",
    "identities_only",
    "identity_files",
    "certificate_files",
    "identity_agent",
    "add_keys_to_agent",
    "pkcs11_provider",
    "security_key_provider",
    "forward_agent",
    "proxy_command",
)

#: Fields a stored link may list that no profile owns any more. They are kept
#: when a link is read so its fingerprint still validates; reconciliation then
#: records the profile's current fields without touching the Host block.
LEGACY_LINK_FIELDS: Tuple[str, ...] = ("key_select_mode",)


def profile_owned_fields(profile: "LoginProfile") -> Tuple[str, ...]:
    """The fields *profile* sets (empty text/lists and ``None`` are unset)."""
    owned = []
    for name in PROFILE_FIELDS:
        value = getattr(profile, name)
        if value is None or value == "" or value == ():
            continue
        owned.append(name)
    return tuple(owned)


def profile_owned_keywords(profile: "LoginProfile") -> Tuple[str, ...]:
    """Directive keywords this profile replaces on a linked connection.

    Extra lines contribute their keywords. ``proxycommand`` is included when
    the profile's own Proxy command row is set, so drift detection watches
    the connection field the formatter writes.
    """
    keys = list(extra_keywords(profile.extra_ssh_config))
    if profile.proxy_command and "proxycommand" not in keys:
        keys.append("proxycommand")
    return tuple(keys)


def extra_keywords(extra: str) -> Tuple[str, ...]:
    """Lowercased directive keywords of an extra-config block, in order."""
    keys: List[str] = []
    for line in split_config_lines(extra or ""):
        key = _extra_keyword(line)
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def auth_projection(
    data: Mapping[str, Any],
    owned_keywords: Iterable[str] = (),
    *,
    expand_paths: bool = False,
    legacy_auth: bool = False,
    owned_fields: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """The profile-owned slice of connection data.

    The authentication order and switches are plain lines the profile owns
    like any other option, so they enter through ``extra``. Links recorded
    before that (``legacy_auth``) also hashed the editor's Key/Password view
    of the block; it stays in their projection so they keep matching.

    Drift detection computes it from what the loader parsed back out of the
    Host block, so loader normalization applies equally to the recorded and the
    current value. ``expand_paths`` mimics the loader's ``~``/``$VAR``
    expansion for not-yet-written data (assignment previews).
    """
    auth_method = int(data.get("auth_method", 0) or 0)
    key_mode = int(data.get("key_select_mode", 0) or 0)
    owned = {str(k).lower() for k in owned_keywords if str(k).strip()}
    extra_lines = sorted(
        " ".join(line.split())
        for line in split_config_lines(str(data.get("extra_ssh_config") or ""))
        if _extra_keyword(line) in owned and _extra_keyword(line) != "proxycommand"
    )

    def paths(key: str) -> List[str]:
        values = _norm_list(data.get(key))
        return [_expand(v) for v in values] if expand_paths else values

    projection = {
        "username": _norm_str(data.get("username")),
        "key_select_mode": key_mode,
        "identity_files": paths("identity_files") if key_mode in (1, 2) else [],
        "certificate_files": paths("certificate_files"),
        "identity_agent": _norm_str(data.get("identity_agent")),
        "add_keys_to_agent": _norm_str(data.get("add_keys_to_agent")).lower(),
        "pkcs11_provider": _norm_str(data.get("pkcs11_provider")),
        "security_key_provider": _norm_str(data.get("security_key_provider")),
        "forward_agent": bool(data.get("forward_agent")),
        "forward_agent_target": _norm_str(data.get("forward_agent_target")),
        "extra": extra_lines,
    }
    # Included only for a profile that sets a Proxy command, so links that
    # leave the connection's own command alone keep their recorded fingerprint.
    if "proxycommand" in owned:
        projection["proxy_command"] = _norm_str(data.get("proxy_command"))
    if legacy_auth:
        projection["auth_method"] = auth_method
        projection["pubkey_auth_no"] = bool(data.get("pubkey_auth_no"))
    if owned_fields is not None:
        # Only what the profile sets is its business (v4 links).
        keep = set(owned_fields) | {"extra"}
        if "forward_agent" in keep:
            keep.add("forward_agent_target")
            projection["forward_agent_explicit_no"] = bool(
                data.get("forward_agent_explicit_no")
            )
            keep.add("forward_agent_explicit_no")
        if "identity_files" in keep and key_mode not in (1, 2):
            projection["identity_files"] = paths("identity_files")
        if "identities_only" in keep:
            projection["identities_only"] = (
                "yes" if key_mode == 1
                else "no" if data.get("identities_only_explicit_no") else ""
            )
        projection = {k: v for k, v in projection.items() if k in keep}
    return projection


def _projection_fingerprint(projection: Mapping[str, Any]) -> str:
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


AUTH_LINES_FINGERPRINT = "v3:"
OWNED_FIELDS_FINGERPRINT = "v4:"


def auth_fingerprint(
    data: Mapping[str, Any],
    owned_keywords: Iterable[str] = (),
    *,
    legacy_auth: bool = False,
    owned_fields: Optional[Iterable[str]] = None,
) -> str:
    """The link's drift fingerprint.

    New links hash only what the profile owns (``v3:``). ``legacy_auth``
    recomputes the fingerprint of a link recorded before the authentication
    lines were plain options, which also hashed the Key/Password view.
    """
    if owned_fields is not None:
        projection = auth_projection(data, owned_keywords, owned_fields=owned_fields)
        return OWNED_FIELDS_FINGERPRINT + _projection_fingerprint(projection)
    projection = auth_projection(data, owned_keywords, legacy_auth=legacy_auth)
    if not legacy_auth:
        return AUTH_LINES_FINGERPRINT + _projection_fingerprint(projection)
    # Version command-owning links so legacy whitespace folding is only used
    # during migration, never to mask subsequent edits inside shell quotes.
    prefix = "v2:" if "proxy_command" in projection else ""
    return prefix + _projection_fingerprint(projection)


def is_legacy_auth_link(applied: str) -> bool:
    return bool(applied) and not applied.startswith(
        (AUTH_LINES_FINGERPRINT, OWNED_FIELDS_FINGERPRINT)
    )


def has_legacy_link_fields(link: "ProfileLink") -> bool:
    """Whether *link* lists a field that profiles no longer own."""
    return any(name in LEGACY_LINK_FIELDS for name in link.fields)


def link_fingerprint(data: Mapping[str, Any], link: "ProfileLink") -> str:
    """Recompute *link*'s fingerprint the way it was recorded."""
    if link.applied.startswith(OWNED_FIELDS_FINGERPRINT):
        return auth_fingerprint(data, link.extra_keys, owned_fields=link.fields)
    return auth_fingerprint(
        data, link.extra_keys, legacy_auth=is_legacy_auth_link(link.applied)
    )


def matches_legacy_auth_fingerprint(
    data: Mapping[str, Any], owned_keywords: Iterable[str], applied: str,
) -> bool:
    """Recognize both historical ProxyCommand projections before upgrading a link."""
    if applied.startswith("v2:") or not is_legacy_auth_link(applied):
        return False
    projection = auth_projection(data, owned_keywords, legacy_auth=True)
    if "proxy_command" not in projection:
        return False
    command = " ".join(projection["proxy_command"].split())
    projection["proxy_command"] = command
    if _projection_fingerprint(projection) == applied:
        return True
    del projection["proxy_command"]
    if command:
        projection["extra"] = sorted([*projection["extra"], "ProxyCommand " + command])
    return _projection_fingerprint(projection) == applied


# Profile-owned fields compared by the assignment preview diff, in display
# order. Frontends own the labels.
DIFF_FIELDS: Tuple[str, ...] = (
    "username",
    "key_select_mode",
    "identities_only",
    "identity_files",
    "certificate_files",
    "identity_agent",
    "add_keys_to_agent",
    "pkcs11_provider",
    "security_key_provider",
    "forward_agent",
    "forward_agent_target",
    "proxy_command",
    "extra",
)


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: Any
    after: Any


def diff_projections(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> Tuple[FieldChange, ...]:
    return tuple(
        FieldChange(key, before.get(key), after.get(key))
        for key in DIFF_FIELDS
        if before.get(key) != after.get(key)
    )


# ---------------------------------------------------------------------------
# Link metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileLink:
    mode: str
    profile_id: Optional[str] = None
    applied: str = ""
    rev: str = ""
    extra_keys: Tuple[str, ...] = ()
    # The profile fields this link applied (v4 links): drift and locking
    # concern these only.
    fields: Tuple[str, ...] = ()

    def to_metadata(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "mode": self.mode,
            "applied": self.applied,
            "rev": self.rev,
            "extra_keys": list(self.extra_keys),
        }
        if self.fields:
            payload["fields"] = list(self.fields)
        if self.mode == LINK_EXPLICIT:
            payload["id"] = self.profile_id
        return payload


def parse_link(metadata: Optional[Mapping[str, Any]]) -> Optional[ProfileLink]:
    """Read the profile link from a connection's metadata; tolerate junk."""
    if not metadata:
        return None
    raw = metadata.get(LINK_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    mode = raw.get("mode")
    applied = raw.get("applied") if isinstance(raw.get("applied"), str) else ""
    rev = raw.get("rev") if isinstance(raw.get("rev"), str) else ""
    keys_raw = raw.get("extra_keys")
    extra_keys = tuple(
        str(k).lower() for k in (keys_raw if isinstance(keys_raw, (list, tuple)) else ())
        if isinstance(k, str) and k.strip()
    )
    fields_raw = raw.get("fields")
    fields = tuple(
        f for f in (fields_raw if isinstance(fields_raw, (list, tuple)) else ())
        if isinstance(f, str) and (f in PROFILE_FIELDS or f in LEGACY_LINK_FIELDS)
    )
    if mode == LINK_EXPLICIT and is_profile_id(raw.get("id")):
        return ProfileLink(LINK_EXPLICIT, str(raw["id"]), applied, rev, extra_keys, fields)
    if mode == LINK_INHERIT:
        return ProfileLink(LINK_INHERIT, None, applied, rev, extra_keys, fields)
    return None


@dataclass(frozen=True)
class LoginProfileState:
    """Everything persisted in ``login_profiles.json``."""

    profiles: Tuple[LoginProfile, ...] = ()
    # scope ("default" | "isolated") -> group id -> profile id
    group_links: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = [p.id for p in self.profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate login profile ids")
        names = [p.name.casefold() for p in self.profiles]
        if len(set(names)) != len(names):
            raise ValueError("duplicate login profile names")

    def get(self, profile_id: Optional[str]) -> Optional[LoginProfile]:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        return None

    def links_for(self, scope: str) -> Dict[str, str]:
        return dict(self.group_links.get(scope, {}))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "profiles": {p.id: p.to_dict() for p in self.profiles},
            "group_links": {
                scope: dict(sorted(links.items()))
                for scope, links in sorted(self.group_links.items())
                if links
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LoginProfileState":
        if data.get("version") != 1:
            raise ValueError("unsupported login profile file version")
        raw_profiles = data.get("profiles", {})
        if not isinstance(raw_profiles, Mapping):
            raise ValueError("profiles must be an object")
        profiles = tuple(
            sorted(
                (LoginProfile.from_dict(item) for item in raw_profiles.values()),
                key=lambda p: p.name.casefold(),
            )
        )
        raw_links = data.get("group_links", {})
        if not isinstance(raw_links, Mapping):
            raise ValueError("group_links must be an object")
        links: Dict[str, Dict[str, str]] = {}
        for scope, mapping in raw_links.items():
            if not isinstance(mapping, Mapping):
                raise ValueError("group_links entries must be objects")
            links[str(scope)] = {
                str(gid): str(pid)
                for gid, pid in mapping.items()
                if is_profile_id(pid)
            }
        return cls(profiles=profiles, group_links=links)


def sorted_profiles(profiles: Iterable[LoginProfile]) -> Tuple[LoginProfile, ...]:
    return tuple(sorted(profiles, key=lambda p: p.name.casefold()))
