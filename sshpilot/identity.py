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
import os
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
    are per-key — register them by path as needed. The manager also tracks a
    *selected* default provider (config ``identity.provider``): the one whose
    environment injection is applied to spawned connections via
    :meth:`apply_selected_to_env`. ``'auto'`` (the default, and any unknown
    selection) resolves to the system ssh-agent, so the agent is never silently
    disabled.
    """

    SYSTEM_AGENT = "system-agent"

    def __init__(self) -> None:
        self._providers: Dict[str, IdentityProvider] = {}
        self._selected: Optional[str] = None  # resolved lazily (config/env)
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

    # -- selection (default provider for env injection) ------------------
    def set_selected(self, name: Optional[str]) -> None:
        """Choose the default identity provider (``'auto'`` = system ssh-agent)."""
        self._selected = (name or "auto").strip().lower()

    def _selected_name(self) -> str:
        """Resolve the selected provider name, defaulting from the
        ``SSHPILOT_IDENTITY_PROVIDER`` env var (set by the app) then ``'auto'``."""
        if self._selected is None:
            env = os.environ.get("SSHPILOT_IDENTITY_PROVIDER")
            self._selected = (env or "auto").strip().lower()
        return self._selected

    def registered_providers(self) -> List[str]:
        """Names of every registered provider (for the configuration UI)."""
        return list(self._providers.keys())

    def selected_provider(self) -> Optional[IdentityProvider]:
        """The provider that injects the default identity env. ``'auto'`` and any
        unknown selection resolve to the system ssh-agent, preserving the historical
        behavior and never silently disabling the agent."""
        name = self._selected_name()
        if name in ("", "auto"):
            return self._providers.get(self.SYSTEM_AGENT)
        return self._providers.get(name) or self._providers.get(self.SYSTEM_AGENT)

    def apply_selected_to_env(self, env: Dict[str, str]) -> Dict[str, str]:
        """Apply the selected default provider's :meth:`IdentityProvider.apply_to_env`.
        The single seam for identity/agent env injection in the connection flow."""
        provider = self.selected_provider()
        if provider is None:
            return dict(env)
        try:
            return provider.apply_to_env(env)
        except Exception as exc:
            logger.debug("identity apply_to_env failed (%s): %s",
                         self._selected_name(), exc)
            return dict(env)

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
