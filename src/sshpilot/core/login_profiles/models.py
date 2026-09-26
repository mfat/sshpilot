"""Login profile value objects and pure connection-data helpers (GTK-free).

A :class:`LoginProfile` is a named, reusable bundle of authentication settings
(username, key selection, agent/hardware options, extra directives) that can
be linked to any number of SSH connections, directly or through a group.

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


def _extra_keyword(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    return re.split(r"[\s=]+", stripped, maxsplit=1)[0].lower()


def _clean_extra(value: Any) -> str:
    text = _clean_text(value, "extra_ssh_config", single_line=False)
    lines = [line.strip() for line in text.splitlines()]
    from ...ssh_config_formatter import MANAGED_HOST_OPTIONS

    for line in lines:
        keyword = _extra_keyword(line)
        if keyword in ("host", "match", "include"):
            raise ValueError("extra_ssh_config must not contain Host, Match or Include lines")
        if keyword in MANAGED_HOST_OPTIONS:
            raise ValueError(
                f"{keyword} has a dedicated field and cannot be set in extra_ssh_config"
            )
    return "\n".join(line for line in lines if line and not line.startswith("#"))


@dataclass(frozen=True)
class LoginProfile:
    """One reusable login profile. Immutable; use :meth:`with_changes`."""

    id: str
    name: str
    username: str = ""
    auth_method: int = AUTH_KEY
    key_select_mode: int = 0
    identity_files: Tuple[str, ...] = ()
    certificate_files: Tuple[str, ...] = ()
    identity_agent: str = ""
    add_keys_to_agent: str = ""
    pkcs11_provider: str = ""
    security_key_provider: str = ""
    pubkey_auth_no: bool = False
    forward_agent: bool = False
    forward_agent_target: str = ""
    extra_ssh_config: str = ""
    has_password: bool = False
    has_sudo_password: bool = False
    revision: int = 1

    def __post_init__(self) -> None:
        if not is_profile_id(self.id):
            raise ValueError("login profile id is invalid")
        name = _clean_text(self.name, "name")
        if not name:
            raise ValueError("login profile name must not be empty")
        if len(name) > _MAX_NAME:
            raise ValueError("login profile name is too long")
        object.__setattr__(self, "name", name)
        username = _clean_text(self.username, "username")
        if any(ch.isspace() for ch in username):
            raise ValueError("username must not contain whitespace")
        object.__setattr__(self, "username", username)
        if self.auth_method not in (AUTH_KEY, AUTH_PASSWORD):
            raise ValueError("auth_method must be 0 (key) or 1 (password)")
        if self.key_select_mode not in (0, 1, 2):
            raise ValueError("key_select_mode must be 0, 1 or 2")
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
        add_keys = _clean_text(self.add_keys_to_agent, "add_keys_to_agent").lower()
        if add_keys not in _VALID_ADD_KEYS and not re.fullmatch(
            r"(yes|no|ask|confirm)?\s*\d+[smhdw]?", add_keys
        ):
            raise ValueError("add_keys_to_agent is invalid")
        object.__setattr__(self, "add_keys_to_agent", add_keys)
        object.__setattr__(self, "extra_ssh_config", _clean_extra(self.extra_ssh_config))
        for flag in ("pubkey_auth_no", "forward_agent", "has_password", "has_sudo_password"):
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
            "key_select_mode": self.key_select_mode,
            "identity_files": list(self.identity_files),
            "certificate_files": list(self.certificate_files),
            "identity_agent": self.identity_agent,
            "add_keys_to_agent": self.add_keys_to_agent,
            "pkcs11_provider": self.pkcs11_provider,
            "security_key_provider": self.security_key_provider,
            "pubkey_auth_no": self.pubkey_auth_no,
            "forward_agent": self.forward_agent,
            "forward_agent_target": self.forward_agent_target,
            "extra_ssh_config": self.extra_ssh_config,
            "has_password": self.has_password,
            "has_sudo_password": self.has_sudo_password,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LoginProfile":
        if not isinstance(data, Mapping):
            raise TypeError("login profile must be an object")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        values = {k: v for k, v in data.items() if k in known}
        for key in ("identity_files", "certificate_files"):
            if key in values and isinstance(values[key], list):
                values[key] = tuple(values[key])
        return cls(**values)

    def with_changes(self, **changes: Any) -> "LoginProfile":
        return replace(self, **changes)

    # -- editable fields ------------------------------------------------------

    def settings(self) -> Dict[str, Any]:
        """Editable, non-identity fields (what an update request may change)."""
        data = self.to_dict()
        for key in ("id", "revision", "has_password", "has_sudo_password"):
            data.pop(key, None)
        return data


EDITABLE_FIELDS = frozenset(
    LoginProfile(id="lp-0000000000000000", name="x").settings().keys()
)


# ---------------------------------------------------------------------------
# Applying a profile to connection data
# ---------------------------------------------------------------------------

def _merge_extra(connection_extra: str, profile_extra: str) -> str:
    """Profile extra directives replace same-keyword lines of the connection."""
    profile_lines = [line for line in (profile_extra or "").splitlines() if line.strip()]
    owned = set(extra_keywords(profile_extra))
    kept = [
        line
        for line in (connection_extra or "").splitlines()
        if line.strip() and _extra_keyword(line) not in owned
    ]
    return "\n".join(kept + profile_lines)


def apply_profile_to_data(data: Mapping[str, Any], profile: LoginProfile) -> Dict[str, Any]:
    """Return a copy of connection *data* with *profile*'s settings overlaid.

    The result is a full update payload for ``ConnectionRepository.update_connection``.
    """
    out = dict(data)
    out["username"] = profile.username
    authored = {
        str(name).strip().lower()
        for name in (out.get("__authored_directives") or ())
        if str(name).strip()
    }
    if profile.username:
        authored.add("user")
    else:
        authored.discard("user")
    out["__authored_directives"] = tuple(sorted(authored))

    out["auth_method"] = profile.auth_method
    out["key_select_mode"] = profile.key_select_mode
    out["identity_files"] = list(profile.identity_files)
    out["keyfile"] = profile.identity_files[0] if profile.identity_files else ""
    out["certificate_files"] = list(profile.certificate_files)
    out["certificate"] = profile.certificate_files[0] if profile.certificate_files else ""
    out["identity_file_none"] = False
    out["identities_only_explicit_no"] = False
    out["identity_agent"] = profile.identity_agent
    out["add_keys_to_agent"] = profile.add_keys_to_agent
    out["pkcs11_provider"] = profile.pkcs11_provider
    out["security_key_provider"] = profile.security_key_provider
    out["pubkey_auth_no"] = profile.pubkey_auth_no
    out["forward_agent"] = profile.forward_agent
    out["forward_agent_target"] = profile.forward_agent_target if profile.forward_agent else ""
    out["forward_agent_explicit_no"] = False
    # Let the formatter pick the method order for the profile's auth method; a
    # stale per-host PreferredAuthentications must not outlive the profile.
    out["preferred_authentications"] = []
    out["extra_ssh_config"] = _merge_extra(
        str(out.get("extra_ssh_config") or ""), profile.extra_ssh_config
    )
    return out


def _norm_str(value: Any) -> str:
    return str(value or "").strip()


def _norm_list(values: Any) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def extra_keywords(extra: str) -> Tuple[str, ...]:
    """Lowercased directive keywords of an extra-config block, in order."""
    keys: List[str] = []
    for line in (extra or "").splitlines():
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
) -> Dict[str, Any]:
    """The profile-owned slice of connection data.

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
        for line in str(data.get("extra_ssh_config") or "").splitlines()
        if _extra_keyword(line) in owned
    )

    def paths(key: str) -> List[str]:
        values = _norm_list(data.get(key))
        return [_expand(v) for v in values] if expand_paths else values

    return {
        "username": _norm_str(data.get("username")),
        "auth_method": auth_method,
        "key_select_mode": key_mode,
        "identity_files": paths("identity_files") if key_mode in (1, 2) else [],
        "certificate_files": paths("certificate_files"),
        "identity_agent": _norm_str(data.get("identity_agent")),
        "add_keys_to_agent": _norm_str(data.get("add_keys_to_agent")).lower(),
        "pkcs11_provider": _norm_str(data.get("pkcs11_provider")),
        "security_key_provider": _norm_str(data.get("security_key_provider")),
        "pubkey_auth_no": bool(data.get("pubkey_auth_no")),
        "forward_agent": bool(data.get("forward_agent")),
        "forward_agent_target": _norm_str(data.get("forward_agent_target")),
        "extra": extra_lines,
    }


def auth_fingerprint(data: Mapping[str, Any], owned_keywords: Iterable[str] = ()) -> str:
    encoded = json.dumps(
        auth_projection(data, owned_keywords), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


# Human-readable labels for the assignment preview diff.
DIFF_LABELS: Tuple[Tuple[str, str], ...] = (
    ("username", "User"),
    ("auth_method", "Authentication"),
    ("key_select_mode", "Key selection"),
    ("identity_files", "IdentityFile"),
    ("certificate_files", "CertificateFile"),
    ("identity_agent", "IdentityAgent"),
    ("add_keys_to_agent", "AddKeysToAgent"),
    ("pkcs11_provider", "PKCS11Provider"),
    ("security_key_provider", "SecurityKeyProvider"),
    ("pubkey_auth_no", "PubkeyAuthentication no"),
    ("forward_agent", "ForwardAgent"),
    ("forward_agent_target", "ForwardAgent target"),
    ("extra", "Extra directives"),
)


@dataclass(frozen=True)
class FieldChange:
    field: str
    label: str
    before: Any
    after: Any


def diff_projections(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> Tuple[FieldChange, ...]:
    return tuple(
        FieldChange(key, label, before.get(key), after.get(key))
        for key, label in DIFF_LABELS
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

    def to_metadata(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "mode": self.mode,
            "applied": self.applied,
            "rev": self.rev,
            "extra_keys": list(self.extra_keys),
        }
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
    if mode == LINK_EXPLICIT and is_profile_id(raw.get("id")):
        return ProfileLink(LINK_EXPLICIT, str(raw["id"]), applied, rev, extra_keys)
    if mode == LINK_INHERIT:
        return ProfileLink(LINK_INHERIT, None, applied, rev, extra_keys)
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
