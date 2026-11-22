"""Authentication settings: plain lines of the Host block.

The login order (``PreferredAuthentications``) and the per-method switches
(``PubkeyAuthentication``, ``PasswordAuthentication``,
``KbdInteractiveAuthentication``, ``GSSAPIAuthentication``,
``HostbasedAuthentication``) are kept exactly as the Host block writes them,
like any other option, so OpenSSH reads what the user set. The writer never
generates them.

The editor's older Key/Password choice and "Disable public key
authentication" switch are translated into those lines here, and only when
the user changes them.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

# ssh_config(5): the order OpenSSH uses when no list is configured.
OPENSSH_DEFAULT_PREFERRED_AUTH = (
    "gssapi-with-mic,hostbased,publickey,keyboard-interactive,password"
)

# "Password first": OpenSSH's order with the password methods moved to the
# front as a pair, in OpenSSH's own relative order, because most servers ask
# for a password through keyboard-interactive (PAM, one-time codes).
PASSWORD_MODE_PREFERRED_AUTH = (
    "keyboard-interactive,password,gssapi-with-mic,hostbased,publickey"
)

# The keyword of each login-policy line, lower case.
PREFERRED_AUTH_KEYWORD = "preferredauthentications"
PUBKEY_KEYWORD = "pubkeyauthentication"
AUTH_POLICY_KEYWORDS = frozenset({
    PREFERRED_AUTH_KEYWORD,
    PUBKEY_KEYWORD,
    "passwordauthentication",
    "kbdinteractiveauthentication",
    "challengeresponseauthentication",  # older name of the line above
    "gssapiauthentication",
    "hostbasedauthentication",
})


def password_comes_first(methods: Iterable[str]) -> bool:
    """True when ``password`` precedes ``publickey`` in a login order.

    The editor's Key/Password choice reads such a block as password login; a
    list without ``password``, or with ``publickey`` ahead of it, reads as key
    login.
    """
    lowered = [str(m).strip().lower() for m in methods]
    if "password" not in lowered:
        return False
    if "publickey" not in lowered:
        return True
    return lowered.index("password") < lowered.index("publickey")


def _keyword(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    return re.split(r"[\s=]+", stripped, maxsplit=1)[0].lower()


def _value(line: str) -> str:
    parts = re.split(r"[\s=]+", line.strip(), maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def preferred_order(extra: str) -> List[str]:
    """The login order an extra-options text sets (first line wins)."""
    for line in (extra or "").splitlines():
        if _keyword(line) == PREFERRED_AUTH_KEYWORD:
            return [m.strip().lower() for m in _value(line).split(",") if m.strip()]
    return []


def _without(lines: List[str], keyword: str, value: Optional[str] = None) -> List[str]:
    return [
        line for line in lines
        if not (
            _keyword(line) == keyword
            and (value is None or _value(line).lower() == value)
        )
    ]


def apply_legacy_auth_choice(
    extra: str,
    *,
    auth_method: Optional[int] = None,
    pubkey_auth_no: Optional[bool] = None,
) -> str:
    """Turn the older Key/Password choice into Host-block lines.

    ``None`` means the choice was not changed. Choosing password login puts
    the password methods first unless the order already does; choosing key
    login removes an order that puts them first and a ``PubkeyAuthentication
    no`` (the two things that made the block read as password login). The
    switch sets or removes ``PubkeyAuthentication no``; an authored ``yes``
    is the user's own and stays.
    """
    lines: List[str] = [line for line in (extra or "").splitlines() if line.strip()]
    if auth_method is not None:
        order = preferred_order("\n".join(lines))
        if int(auth_method) == 1:
            if not password_comes_first(order):
                lines = _without(lines, PREFERRED_AUTH_KEYWORD)
                lines.append(f"PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}")
        else:
            if password_comes_first(order):
                lines = _without(lines, PREFERRED_AUTH_KEYWORD)
            lines = _without(lines, PUBKEY_KEYWORD, "no")
    if pubkey_auth_no is not None:
        lines = _without(lines, PUBKEY_KEYWORD, "no")
        if pubkey_auth_no:
            lines = _without(lines, PUBKEY_KEYWORD)
            lines.append("PubkeyAuthentication no")
    return "\n".join(lines)


def split_auth_policy(extra: str) -> Tuple[str, str]:
    """``(policy_lines, other_lines)`` of an extra-options text."""
    policy, other = [], []
    for line in (extra or "").splitlines():
        if not line.strip():
            continue
        (policy if _keyword(line) in AUTH_POLICY_KEYWORDS else other).append(line)
    return "\n".join(policy), "\n".join(other)
