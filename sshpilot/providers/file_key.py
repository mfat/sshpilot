"""File-based SSH key identity provider.

Represents a single private key on disk (e.g. ``~/.ssh/id_ed25519``). Passphrase
handling is delegated to the credential backend through :mod:`askpass_utils`
(``lookup_passphrase`` / ``ensure_key_in_agent``) — this provider never talks to
libsecret/keyring directly, so it honours whatever secret backend the user has
selected (see :mod:`secret_storage`).
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

from ..identity import Identity, IdentityProvider

logger = logging.getLogger(__name__)


class FileKeyProvider(IdentityProvider):
    name = "file-key"

    def __init__(self, key_path: str) -> None:
        self._key_path = key_path or ""
        self._expanded = os.path.expanduser(self._key_path)

    @property
    def key_path(self) -> str:
        """The expanded (``~`` resolved) path to the private key."""
        return self._expanded

    def is_available(self) -> bool:
        return bool(self._expanded) and os.path.isfile(self._expanded)

    def apply_to_env(self, env: Dict[str, str]) -> Dict[str, str]:
        new_env = dict(env)
        if self.is_available():
            # Convention variable for callers that opt into a specific key. The
            # canonical ssh mechanism remains ``IdentityFile`` in ``~/.ssh/config``
            # (see CLAUDE.md), so we deliberately do not append a CLI flag here.
            new_env["SSH_IDENTITY_FILE"] = self._expanded
        return new_env

    def list_identities(self) -> List[Identity]:
        if not self.is_available():
            return []
        return [
            Identity(
                id=os.path.realpath(self._expanded),
                display_name=os.path.basename(self._expanded),
                fingerprint=self._fingerprint(),
                provider_name="file-key",
            )
        ]

    # -- passphrase / unlock (delegated to the credential backend) ----------
    def has_stored_passphrase(self) -> bool:
        """Whether the credential backend has a stored passphrase for this key."""
        return bool(self._lookup_passphrase())

    def unlock(self, *, lifetime: int = 0) -> bool:
        """Load (and, if encrypted, unlock) the key into ssh-agent.

        The passphrase is supplied by the credential backend through the shared
        askpass path — there are no libsecret/keyring calls here. ``lifetime`` is
        forwarded to ``ssh-add -t`` (0 = no expiry).
        """
        if not self.is_available():
            return False
        try:
            from ..askpass_utils import ensure_key_in_agent

            return bool(ensure_key_in_agent(self._expanded, force=True, lifetime=lifetime))
        except Exception as exc:
            logger.debug("file-key unlock failed for %s: %s", self._expanded, exc)
            return False

    # -- internals ----------------------------------------------------------
    def _lookup_passphrase(self) -> str:
        try:
            from ..askpass_utils import lookup_passphrase

            return lookup_passphrase(self._key_path) or ""
        except Exception as exc:
            logger.debug("passphrase lookup failed for %s: %s", self._key_path, exc)
            return ""

    def _fingerprint(self) -> Optional[str]:
        """SHA256 fingerprint from the sibling ``.pub`` file, if present."""
        pub_path = self._expanded + ".pub"
        try:
            if os.path.isfile(pub_path):
                from ..authorized_keys_parser import compute_fingerprint

                with open(pub_path, "r", encoding="utf-8") as handle:
                    parts = handle.read().split()
                if len(parts) >= 2:
                    return compute_fingerprint(parts[0], parts[1]) or None
        except Exception as exc:
            logger.debug("fingerprint for %s failed: %s", pub_path, exc)
        return None
