"""Headless, GTK-free SSH identity-provider state service.

The daemon owns the selected identity provider, the custom agent socket,
provider availability, and the effective agent environment applied to spawned
OpenSSH commands.  This service is the settings-backed authority for that
state, mirroring :class:`~sshpilot.core.ssh_overrides_service.SshOverridesService`:
reads and mutations go through the strict settings store under the shared
settings transaction lock, and every mutation is revision-checked.

Provider semantics are inherited from :mod:`sshpilot.identity` and
:mod:`sshpilot.providers`: ``'auto'`` (and any unknown selection) resolves to
the system ssh-agent inherited via ``SSH_AUTH_SOCK``; fixed-socket providers
(the 1Password preset, a user-supplied custom socket) express their socket
through the native agent environment so ssh, ssh-add, and ssh-copy-id all
reach the same agent.  Per-host ``IdentityAgent`` directives in the user's
SSH configuration keep their native precedence over ``SSH_AUTH_SOCK``, which
preserves the established fallback semantics of the retired managed
``Host *`` block without writing machine-wide configuration.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.identity import (
    REVISION_CONFLICT,
    SETTINGS_MALFORMED,
    SETTINGS_PERSISTENCE_FAILED,
    IdentityProviderDescriptor,
    IdentityProviderRegistry,
    IdentityState,
    UpdateIdentityConfigurationRequest,
    UpdateIdentitySelectionRequest,
)
from ..providers.socket_agent import ONEPASSWORD_SOCKET, SocketAgentProvider
from .settings import (
    SettingsFileError,
    compute_identity_revision,
    load_settings_strict,
    normalize_identity_settings,
    save_settings,
    set_nested,
    settings_transaction_lock,
)
from .settings.identity import (
    AUTO_PROVIDER,
    CUSTOM_PROVIDER,
    DEFAULT_IDENTITY_SETTINGS,
    FIELD_TO_CONFIG_KEY,
)

logger = logging.getLogger(__name__)

SYSTEM_AGENT_PROVIDER = "system-agent"
ONEPASSWORD_PROVIDER = "onepassword"

# Environment variables a spawned OpenSSH child needs to reach the agent.
_AGENT_ENV_VARS = ("SSH_AUTH_SOCK", "SSH_AGENT_PID")

# Provider ids the registry advertises, in display order.  ``'auto'`` already
# means the system ssh-agent, so it is not listed twice; ``'file-key'`` is a
# per-connection concern, not a global default.
_REGISTRY_ORDER = (AUTO_PROVIDER, ONEPASSWORD_PROVIDER, CUSTOM_PROVIDER)

_REGISTRY_LABELS = {
    AUTO_PROVIDER: "Automatic (system ssh-agent)",
    ONEPASSWORD_PROVIDER: "1Password",
    CUSTOM_PROVIDER: "Custom socket",
}


def _default_ssh_copy_id_available() -> bool:
    """Whether a native ``ssh-copy-id`` executable is available."""
    import shutil

    flatpak_path = "/app/bin/ssh-copy-id"
    if os.path.exists(flatpak_path) and os.access(flatpak_path, os.X_OK):
        return True
    return shutil.which("ssh-copy-id") is not None


class IdentityStateService:
    """Thread-safe, headless authority for identity-provider state.

    ``environ`` is the daemon's captured process environment: the system
    agent's ``SSH_AUTH_SOCK`` is a volatile per-session path, so it is read
    from the environment the daemon inherited at startup, never from the
    frontend at request time.
    """

    def __init__(
        self,
        settings_path: Path | str,
        *,
        environ: Optional[Mapping[str, str]] = None,
        ssh_copy_id_probe: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._path = Path(settings_path)
        self._lock = threading.Lock()
        self._environ = dict(os.environ if environ is None else environ)
        self._ssh_copy_id_probe = ssh_copy_id_probe or _default_ssh_copy_id_available

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_state(self) -> IdentityState:
        """Return the authoritative current identity state."""
        with self._lock:
            semantic = self._semantic(self._load_strict())
            return self._state_snapshot(semantic)

    def get_providers(self) -> IdentityProviderRegistry:
        """Return the provider registry with safe per-provider metadata."""
        with self._lock:
            semantic = self._semantic(self._load_strict())
            selected = semantic["provider"]
            descriptors = []
            for provider_id in _REGISTRY_ORDER:
                descriptors.append(
                    self._describe(provider_id, semantic, selected=provider_id == selected)
                )
            return IdentityProviderRegistry(
                providers=tuple(descriptors),
                revision=compute_identity_revision(semantic),
            )

    def update_selection(
        self,
        request: UpdateIdentitySelectionRequest,
    ) -> IdentityState:
        """Select the default identity provider (revision-checked)."""
        if type(request) is not UpdateIdentitySelectionRequest:
            raise TypeError("an UpdateIdentitySelectionRequest is required")
        with self._lock:
            with settings_transaction_lock(self._path):
                config = self._load_strict()
                current = self._semantic(config)
                self._check_revision(request.expected_revision, current)
                if request.provider == current["provider"]:
                    return self._state_snapshot(current)
                set_nested(
                    config, FIELD_TO_CONFIG_KEY["provider"], request.provider
                )
                self._persist(config)
                return self._state_snapshot(self._semantic(config))

    def update_configuration(
        self,
        request: UpdateIdentityConfigurationRequest,
    ) -> IdentityState:
        """Configure the custom agent socket (revision-checked)."""
        if type(request) is not UpdateIdentityConfigurationRequest:
            raise TypeError("an UpdateIdentityConfigurationRequest is required")
        with self._lock:
            with settings_transaction_lock(self._path):
                config = self._load_strict()
                current = self._semantic(config)
                self._check_revision(request.expected_revision, current)
                if request.custom_socket == current["custom_socket"]:
                    return self._state_snapshot(current)
                set_nested(
                    config,
                    FIELD_TO_CONFIG_KEY["custom_socket"],
                    request.custom_socket,
                )
                self._persist(config)
                return self._state_snapshot(self._semantic(config))

    # ------------------------------------------------------------------
    # Launch-path view (consumed by the daemon launch provider and the
    # daemon's native agent operations)
    # ------------------------------------------------------------------

    def agent_environment(self, base_env: Mapping[str, str]) -> Dict[str, str]:
        """Return a copy of *base_env* with the selected provider applied.

        This is the single seam through which daemon-spawned OpenSSH commands
        (ssh, sftp, scp, ssh-add, ssh-copy-id) reach the selected agent:

        * system agent (``'auto'``/unknown): the daemon's inherited
          ``SSH_AUTH_SOCK``/``SSH_AGENT_PID`` are forwarded;
        * fixed-socket provider: ``SSH_AUTH_SOCK`` points at the provider's
          socket (OpenSSH's native agent discovery; per-host
          ``IdentityAgent`` directives keep their native precedence over it).

        Never raises: an unreadable settings file degrades to the canonical
        defaults (system agent) so a damaged file cannot abort a launch.
        """
        try:
            with self._lock:
                semantic = self._semantic(self._load_view())
        except Exception:  # defensive: the launch path must not break
            logger.debug("identity view unavailable; using defaults", exc_info=True)
            semantic = dict(DEFAULT_IDENTITY_SETTINGS)
        env = dict(base_env)
        provider = self._resolve_socket_provider(semantic)
        if provider is not None:
            env["SSH_AUTH_SOCK"] = provider.socket_path_expanded
            env.pop("SSH_AGENT_PID", None)
            return env
        for var in _AGENT_ENV_VARS:
            value = self._environ.get(var)
            if value:
                env[var] = value
        return env

    def provider_agent_environment(
        self, provider_id: str, base_env: Mapping[str, str]
    ) -> Dict[str, str]:
        """Return a copy of *base_env* with one *named* provider's agent applied.

        Unlike :meth:`agent_environment`, which resolves the *selected*
        provider, this scopes to an explicit registry provider id so callers
        can observe a specific provider regardless of the current selection:

        * ``'auto'`` (the system ssh-agent): the daemon's inherited
          ``SSH_AUTH_SOCK``/``SSH_AGENT_PID`` are forwarded;
        * a fixed-socket provider (``'onepassword'``, ``'custom'``):
          ``SSH_AUTH_SOCK`` points at the provider's socket.

        Unknown provider ids resolve to the system agent.  Never raises: an
        unreadable settings file degrades to the canonical defaults.
        """
        try:
            with self._lock:
                semantic = self._semantic(self._load_view())
        except Exception:  # defensive: the launch path must not break
            logger.debug("identity view unavailable; using defaults", exc_info=True)
            semantic = dict(DEFAULT_IDENTITY_SETTINGS)
        env = dict(base_env)
        socket = self._provider_socket(provider_id, semantic)
        if socket:
            env["SSH_AUTH_SOCK"] = socket
            env.pop("SSH_AGENT_PID", None)
            return env
        for var in _AGENT_ENV_VARS:
            value = self._environ.get(var)
            if value:
                env[var] = value
        return env

    def effective_agent_socket(self) -> str:
        """The agent socket the selected provider effectively uses (or '')."""
        try:
            with self._lock:
                semantic = self._semantic(self._load_view())
        except Exception:
            semantic = dict(DEFAULT_IDENTITY_SETTINGS)
        return self._effective_socket(semantic)

    # ------------------------------------------------------------------
    # Provider resolution (mirrors IdentityManager.selected_provider)
    # ------------------------------------------------------------------

    def _resolve_socket_provider(
        self, semantic: Mapping[str, Any]
    ) -> Optional["_ResolvedSocketProvider"]:
        """The selected fixed-socket provider, or None for the system agent.

        ``'custom'`` without a configured socket resolves to the system
        agent, preserving the never-silently-disable semantics.
        """
        name = semantic["provider"]
        if name == ONEPASSWORD_PROVIDER:
            return _ResolvedSocketProvider(ONEPASSWORD_PROVIDER, ONEPASSWORD_SOCKET)
        if name == CUSTOM_PROVIDER:
            socket = semantic["custom_socket"]
            if socket:
                return _ResolvedSocketProvider(CUSTOM_PROVIDER, socket)
            return None
        # 'auto', 'system-agent', and unknown selections: system agent.
        return None

    def _provider_socket(
        self, provider_id: str, semantic: Mapping[str, Any]
    ) -> str:
        """The explicit provider's fixed agent socket, or '' for the system agent.

        Mirrors :meth:`_resolve_socket_provider` but for a *named* provider id
        rather than the current selection, so a provider can be observed even
        when it is not selected.
        """
        name = str(provider_id or "").strip().lower()
        if name == ONEPASSWORD_PROVIDER:
            return os.path.expanduser(ONEPASSWORD_SOCKET)
        if name == CUSTOM_PROVIDER:
            socket = semantic["custom_socket"]
            return os.path.expanduser(socket) if socket else ""
        # 'auto', 'system-agent', and unknown provider ids: system agent.
        return ""

    def _effective_socket(self, semantic: Mapping[str, Any]) -> str:
        provider = self._resolve_socket_provider(semantic)
        if provider is not None:
            return provider.socket_path
        return str(self._environ.get("SSH_AUTH_SOCK") or "")

    def _provider_available(
        self, provider_id: str, semantic: Mapping[str, Any]
    ) -> bool:
        if provider_id == AUTO_PROVIDER:
            return bool(self._environ.get("SSH_AUTH_SOCK"))
        if provider_id == ONEPASSWORD_PROVIDER:
            return _socket_exists(ONEPASSWORD_SOCKET)
        if provider_id == CUSTOM_PROVIDER:
            socket = semantic["custom_socket"]
            return bool(socket) and _socket_exists(socket)
        return False

    def _describe(
        self,
        provider_id: str,
        semantic: Mapping[str, Any],
        *,
        selected: bool,
    ) -> IdentityProviderDescriptor:
        available = self._provider_available(provider_id, semantic)
        if provider_id == AUTO_PROVIDER:
            socket = str(self._environ.get("SSH_AUTH_SOCK") or "")
            capabilities = ("ssh_auth_sock",)
            detail = "" if available else "agent-socket-env-missing"
            custom_required = False
        elif provider_id == ONEPASSWORD_PROVIDER:
            socket = ONEPASSWORD_SOCKET
            capabilities = ("identity_agent", "ssh_auth_sock")
            detail = "" if available else "agent-socket-not-found"
            custom_required = False
        else:
            socket = semantic["custom_socket"]
            capabilities = ("identity_agent", "ssh_auth_sock")
            if not socket:
                detail = "custom-socket-unset"
            elif not available:
                detail = "agent-socket-not-found"
            else:
                detail = ""
            custom_required = True
        return IdentityProviderDescriptor(
            provider_id=provider_id,
            label=_REGISTRY_LABELS[provider_id],
            available=available,
            selected=selected,
            effective_agent_socket=socket,
            custom_socket_required=custom_required,
            capabilities=capabilities,
            detail=detail,
        )

    def _state_snapshot(self, semantic: Mapping[str, Any]) -> IdentityState:
        selected = semantic["provider"]
        ssh_copy_id_available = False
        try:
            ssh_copy_id_available = bool(self._ssh_copy_id_probe())
        except Exception:
            logger.debug("ssh-copy-id probe failed", exc_info=True)
        # Availability follows the *effective* resolution: a custom selection
        # without a configured socket resolves to the system agent, matching
        # the launch environment and the never-silently-disable semantics.
        socket_provider = self._resolve_socket_provider(semantic)
        if socket_provider is not None:
            agent_available = _socket_exists(socket_provider.socket_path)
        else:
            agent_available = bool(self._environ.get("SSH_AUTH_SOCK"))
        return IdentityState(
            provider=selected,
            custom_socket=semantic["custom_socket"],
            effective_agent_socket=self._effective_socket(semantic),
            agent_available=agent_available,
            ssh_copy_id_available=ssh_copy_id_available,
            revision=compute_identity_revision(semantic),
        )

    # ------------------------------------------------------------------
    # Settings plumbing (mirrors SshOverridesService)
    # ------------------------------------------------------------------

    def _semantic(self, config: Mapping[str, Any]) -> Dict[str, Any]:
        identity = config.get("identity", {}) if isinstance(config, Mapping) else {}
        if not isinstance(identity, Mapping):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The identity settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        try:
            return normalize_identity_settings(identity)
        except (TypeError, ValueError) as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The identity settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            ) from exc

    def _load_strict(self) -> Dict[str, Any]:
        try:
            config, _migrated = load_settings_strict(self._path)
        except SettingsFileError as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The identity settings file could not be read",
                details={"code": SETTINGS_MALFORMED},
            ) from exc
        if not isinstance(config, dict):
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The identity settings file is malformed",
                details={"code": SETTINGS_MALFORMED},
            )
        return config

    def _load_view(self) -> Dict[str, Any]:
        """Best-effort load for the launch path (defaults on malformed input)."""
        try:
            return self._load_strict()
        except SshPilotError:
            logger.warning(
                "Identity settings file is malformed; using canonical defaults "
                "for the launch view"
            )
            from .settings import get_default_config

            return get_default_config()

    def _check_revision(
        self,
        expected_revision: Optional[str],
        current: Mapping[str, Any],
    ) -> None:
        if expected_revision is None:
            return
        if expected_revision != compute_identity_revision(current):
            raise SshPilotError(
                ErrorCode.VALIDATION_FAILED,
                "The identity settings have been modified since last read",
                details={"code": REVISION_CONFLICT},
            )

    def _persist(self, config: Dict[str, Any]) -> None:
        try:
            save_settings(self._path, config)
        except Exception as exc:
            raise SshPilotError(
                ErrorCode.PERSISTENCE_FAILED,
                "The identity settings could not be saved",
                details={"code": SETTINGS_PERSISTENCE_FAILED},
            ) from exc


class _ResolvedSocketProvider:
    """A selected fixed-socket agent (reuses the provider socket semantics)."""

    def __init__(self, name: str, socket_path: str) -> None:
        self._provider = SocketAgentProvider(name, name, socket_path)

    @property
    def socket_path(self) -> str:
        """The configured socket path (as written)."""
        return self._provider.socket_path

    @property
    def socket_path_expanded(self) -> str:
        return os.path.expanduser(self._provider.socket_path)


def _socket_exists(socket_path: str) -> bool:
    expanded = os.path.expanduser(socket_path) if socket_path else ""
    if not expanded:
        return False
    try:
        return os.path.exists(expanded)
    except Exception:
        return False
