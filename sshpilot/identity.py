"""Pluggable SSH identity providers.

Where :mod:`secret_storage` answers *"what password/passphrase do we use?"*,
this module answers *"which SSH key or agent authenticates the connection, and
what does the spawned process need in its environment for that to work?"*. It is
the identity-side parallel of the credential backends, and follows the same
shape: a small :class:`IdentityProvider` interface plus an
:class:`IdentityManager` registry exposed through :func:`get_identity_manager`.

Keeping the two abstractions separate means users can mix and match as plain
configuration — e.g. passwords in libsecret while keys come from the system
ssh-agent, or passwords in Bitwarden while a key is read from
``~/.ssh/id_ed25519``.

Two concrete providers ship today (:mod:`sshpilot.providers.system_agent` and
:mod:`sshpilot.providers.file_key`); ``IDENTITY_PROVIDERS.md`` documents the
contract that future providers (e.g. a Bitwarden agent, PKCS#11) must honour.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Identity:
    """A single SSH identity surfaced by a provider.

    ``id`` is stable within a provider (an agent key fingerprint, a key file's
    canonical path, …) so callers can refer to it across listings.
    """

    id: str
    display_name: str
    fingerprint: str | None
    provider_name: str


class IdentityProvider(ABC):
    """Supplies SSH identities and the environment a spawned process needs to use
    them.

    Implementations must be safe to instantiate even when their dependency is
    missing — :meth:`is_available` reports readiness rather than the constructor
    raising.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable, lowercase provider identifier (e.g. ``"system-agent"``)."""

    @abstractmethod
    def list_identities(self) -> List[Identity]:
        """Return the identities this provider currently exposes (may be empty)."""

    @abstractmethod
    def apply_to_env(self, env: Dict[str, str]) -> Dict[str, str]:
        """Return a **copy** of ``env`` with whatever this provider needs
        injected (e.g. ``SSH_AUTH_SOCK``, ``SSH_IDENTITY_FILE``).

        Must not mutate the argument in place.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Whether this provider can be used right now (agent running, key file
        present, …)."""


class IdentityManager:
    """Registry of identity providers, parallel to
    :class:`secret_storage.SecretManager`.

    The default registry holds the system ssh-agent provider. File-key providers
    are per-key — register them by path as needed. Provider *selection* (which
    provider authenticates a given connection) is intentionally out of scope here;
    that lands with the configuration UI. This manager just holds providers and
    aggregates their identities.
    """

    SYSTEM_AGENT = "system-agent"

    def __init__(self) -> None:
        self._providers: Dict[str, IdentityProvider] = {}
        # Lazy import avoids a module-load cycle (providers import this module).
        from .providers.system_agent import SystemAgentProvider

        self.register(SystemAgentProvider())

    def register(self, provider: IdentityProvider) -> None:
        """Register (or replace) a provider, keyed by its ``name``."""
        self._providers[provider.name] = provider

    def get(self, name: str) -> Optional[IdentityProvider]:
        return self._providers.get(name)

    def system_agent(self) -> IdentityProvider:
        """The system ssh-agent provider (always registered)."""
        return self._providers[self.SYSTEM_AGENT]

    def providers(self) -> List[IdentityProvider]:
        return list(self._providers.values())

    def available_providers(self) -> List[IdentityProvider]:
        return [p for p in self._providers.values() if p.is_available()]

    def list_identities(self) -> List[Identity]:
        """Aggregate identities across every currently-available provider."""
        identities: List[Identity] = []
        for provider in self.available_providers():
            try:
                identities.extend(provider.list_identities())
            except Exception as exc:  # one provider must never break aggregation
                logger.debug("identity listing failed for %s: %s", provider.name, exc)
        return identities


_MANAGER: Optional[IdentityManager] = None


def get_identity_manager() -> IdentityManager:
    """Return the process-wide :class:`IdentityManager` singleton."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = IdentityManager()
    return _MANAGER
