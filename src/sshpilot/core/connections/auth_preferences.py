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
KBDINTERACTIVE_KEYWORD = "kbdinteractiveauthentication"
# Other names OpenSSH accepts for the same setting (readconf.c). A block may
# use any of them, and the first line of the setting wins whatever its
# spelling, so they are one setting everywhere a keyword is compared.
AUTH_KEYWORD_ALIASES = {
    "challengeresponseauthentication": KBDINTERACTIVE_KEYWORD,
    "skeyauthentication": KBDINTERACTIVE_KEYWORD,
    "tisauthentication": KBDINTERACTIVE_KEYWORD,
    "dsaauthentication": PUBKEY_KEYWORD,
}
AUTH_POLICY_KEYWORDS = frozenset({
    PREFERRED_AUTH_KEYWORD,
    PUBKEY_KEYWORD,
    "passwordauthentication",
    KBDINTERACTIVE_KEYWORD,
    "gssapiauthentication",
    "hostbasedauthentication",
    *AUTH_KEYWORD_ALIASES,
})


def canonical_keyword(keyword: str) -> str:
    """The setting a keyword names: an alias gives its current spelling."""
    lowered = str(keyword).strip().lower()
    return AUTH_KEYWORD_ALIASES.get(lowered, lowered)


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


def split_option(line: str) -> Tuple[str, str]:
    """``(lower-case keyword, value)`` of one option line.

    Both forms ssh_config(5) allows are read (``Keyword value`` and
    ``Keyword=value``); a value's surrounding quotes are dropped. A blank
    line or a comment gives ``("", "")``.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return "", ""
    parts = re.split(r"\s*=\s*|\s+", stripped, maxsplit=1)
    value = parts[1].strip() if len(parts) == 2 else ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return parts[0].lower(), value


def _keyword(line: str) -> str:
    return split_option(line)[0]


def _value(line: str) -> str:
    return split_option(line)[1]


def pubkey_disabled(extra: str) -> bool:
    """True when an extra-options text sets ``PubkeyAuthentication no``.

    As OpenSSH reads it: the first line of the keyword decides.
    """
    for line in (extra or "").splitlines():
        keyword, value = split_option(line)
        if canonical_keyword(keyword) == PUBKEY_KEYWORD:
            return value.lower() in ("no", "false")
    return False


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
            canonical_keyword(_keyword(line)) == keyword
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
