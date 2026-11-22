"""The PreferredAuthentications list SSH Pilot writes for password login.

One definition for the config writer, the editor and the daemon launches.
"""
from __future__ import annotations

from typing import Iterable

# Password login: password methods first, then the other methods OpenSSH
# tries by default (Kerberos/GSSAPI, host-based), so choosing a password does
# not silently turn those off. Public keys stay out of the list for now.
PASSWORD_MODE_PREFERRED_AUTH = (
    "keyboard-interactive,password,gssapi-with-mic,hostbased"
)

# What password mode wrote before; upgraded when the block is next written.
LEGACY_PASSWORD_MODE_PREFERRED_AUTH = "keyboard-interactive,password"


def is_password_mode_default(methods: Iterable[str]) -> bool:
    """True for a list SSH Pilot wrote for password mode (current or old)."""
    joined = ",".join(str(m).strip().lower() for m in methods)
    return joined in (
        PASSWORD_MODE_PREFERRED_AUTH,
        LEGACY_PASSWORD_MODE_PREFERRED_AUTH,
    )
