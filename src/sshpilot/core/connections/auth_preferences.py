"""The PreferredAuthentications lists SSH Pilot writes.

One definition for the config writer, the editor and the daemon launches.
Key login writes no list: OpenSSH's default order and the user's own
configuration decide. Password login writes OpenSSH's default order with the
password methods moved to the front.
"""
from __future__ import annotations

from typing import Iterable

# ssh_config(5): the order OpenSSH uses when no list is configured.
OPENSSH_DEFAULT_PREFERRED_AUTH = (
    "gssapi-with-mic,hostbased,publickey,keyboard-interactive,password"
)

# Password login: OpenSSH's order with the password methods first. They move
# as a pair in OpenSSH's own relative order, because most servers ask for a
# password through keyboard-interactive (PAM, one-time codes). Everything
# else follows as OpenSSH would try it; "Disable public key authentication"
# adds PubkeyAuthentication no, which takes publickey out.
PASSWORD_MODE_PREFERRED_AUTH = (
    "keyboard-interactive,password,gssapi-with-mic,hostbased,publickey"
)

# Lists earlier versions wrote. A block still carrying one gets the current
# behaviour the next time it is written; any other list is the user's own.
_EARLIER_PASSWORD_MODE_LISTS = (
    "keyboard-interactive,password",
    "keyboard-interactive,password,gssapi-with-mic,hostbased",
)
# Key login with a saved password used to write OpenSSH's default order
# explicitly. That overrode a list set by an earlier block, so it is dropped.
_EARLIER_KEY_MODE_LISTS = (OPENSSH_DEFAULT_PREFERRED_AUTH,)


def _joined(methods: Iterable[str]) -> str:
    return ",".join(str(m).strip().lower() for m in methods)


def is_password_mode_default(methods: Iterable[str]) -> bool:
    """True for a list SSH Pilot wrote for password mode (current or earlier)."""
    joined = _joined(methods)
    return joined == PASSWORD_MODE_PREFERRED_AUTH or (
        joined in _EARLIER_PASSWORD_MODE_LISTS
    )


def is_app_written_list(methods: Iterable[str]) -> bool:
    """True for any list SSH Pilot itself wrote, in either login mode."""
    return is_password_mode_default(methods) or (
        _joined(methods) in _EARLIER_KEY_MODE_LISTS
    )
