"""Close policies, SSH terminal routing, and daemon readiness helpers.

VTE and PyXtermJS both render sessions owned by the daemon.

Local terminals remain GTK-owned. External terminals remain
external-process-owned. Rollout Stage C defaults daemon-backed SSH on, with
no internal local-SSH alternative.

Route selection (``resolve_ssh_terminal_route``) is pure product/user policy.
It never inspects daemon readiness. Readiness
(``resolve_daemon_terminal_readiness``) never changes the selected route and
never initiates a frontend SSH launch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from gettext import gettext as _
from typing import Any, Optional

from .api.capabilities import Capability
from .api.version import PROTOCOL_VERSION
from .file_manager_integration import should_hide_external_terminal_options
from .terminal_session_controller import (
    daemon_terminal_capabilities_missing,
    required_daemon_terminal_capabilities,
)


class TerminalClosePolicy(str, Enum):
    """Policy for handling daemon SSH terminal close operations."""

    DETACH = "detach"
    TERMINATE = "terminate"
    ASK = "ask"


class SshTerminalRoute(str, Enum):
    """User/product routing for ordinary internal SSH terminal activation.

    Local (non-SSH) shell tabs are outside this enum — callers must not invoke
    the SSH route resolver for ``is_local`` terminals.
    """

    DAEMON = "daemon"
    EXTERNAL = "external"


class DaemonTerminalReadinessReason(str, Enum):
    """Stable reasons a daemon SSH route cannot launch."""

    CLIENT_UNAVAILABLE = "client_unavailable"
    BRIDGE_UNAVAILABLE = "bridge_unavailable"
    HANDSHAKE_INCOMPLETE = "handshake_incomplete"
    SERVER_INSTANCE_MISSING = "server_instance_missing"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    TERMINAL_TRANSPORT_UNAVAILABLE = "terminal_transport_unavailable"
    SECRET_TRANSPORT_UNAVAILABLE = "secret_transport_unavailable"
    MISSING_CAPABILITIES = "missing_capabilities"
    DAEMON_START_FAILED = "daemon_start_failed"
    DAEMON_START_TIMEOUT = "daemon_start_timeout"
    SELECTION_PENDING = "selection_pending"


@dataclass(frozen=True)
class DaemonTerminalReadiness:
    """Result of checking whether a daemon SSH launch may proceed."""

    ready: bool
    reason: Optional[DaemonTerminalReadinessReason] = None
    missing_capabilities: tuple[Capability, ...] = ()


TAB_CLOSE_POLICY_SETTING = "terminal.daemon_tab_close_policy"
APP_CLOSE_POLICY_SETTING = "terminal.daemon_app_close_policy"
RESTORE_SESSIONS_SETTING = "terminal.daemon_restore_sessions"
AUTO_ATTACH_SETTING = "terminal.daemon_auto_attach"
# Retained as a compatibility setting name; internal SSH routing is daemon-only.
PREFERRED_EMULATOR_SETTING = "terminal.daemon_emulator"
SESSION_RESTORE_STATE_SETTING = "terminal.daemon_session_restore_state"

# Stage C: default on for supported installs.

_TERMINAL_TRANSPORT_CAPS = frozenset(
    {
        Capability.TERMINAL_OUTPUT,
        Capability.TERMINAL_INPUT,
        Capability.TERMINAL_RESIZE,
        Capability.TERMINAL_REPLAY,
    }
)
_SECRET_TRANSPORT_CAPS = frozenset(
    {
        Capability.INTERACTIONS_READ,
        Capability.INTERACTIONS_RESPOND,
        Capability.INTERACTIONS_EVENTS,
        Capability.INTERACTIONS_HOST_KEY,
        Capability.INTERACTIONS_PASSWORD,
        Capability.INTERACTIONS_PASSPHRASE,
    }
)


def _config_of(window_or_config: Any):
    nested = getattr(window_or_config, "config", None)
    if nested is not None and callable(getattr(nested, "get_setting", None)):
        return nested
    return window_or_config


def _get_setting(config, key: str, default=None):
    getter = getattr(config, "get_setting", None)
    if callable(getter):
        return getter(key, default)
    return default


def resolve_ssh_terminal_route(
    window_or_config,
    connection,
    *,
    is_local: bool = False,
) -> Optional[SshTerminalRoute]:
    """Select external integration or the mandatory daemon session route."""
    if connection is None:
        return None
    config = _config_of(window_or_config)
    use_external = bool(_get_setting(config, "use-external-terminal", False))
    if use_external and not should_hide_external_terminal_options():
        return SshTerminalRoute.EXTERNAL
    return SshTerminalRoute.DAEMON

def should_use_daemon_ssh_terminal(
    window_or_config,
    connection,
    *,
    client=None,
    is_local: bool = False,
) -> bool:
    """Return whether ordinary internal SSH activation selected the daemon route.

    Readiness is *not* consulted. Pass ``client`` only for call-site
    compatibility; it is ignored.
    """

    del client  # readiness is separate from route selection
    return (
        resolve_ssh_terminal_route(
            window_or_config,
            connection,
            is_local=is_local,
        )
        is SshTerminalRoute.DAEMON
    )


def _read_server_instance_id(client) -> tuple[Optional[str], Optional[DaemonTerminalReadinessReason]]:
    if client is None:
        return None, DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE
    if not hasattr(client, "open_session"):
        return None, DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE
    # Use identity checks so MagicMock test doubles are not treated as closed.
    if getattr(client, "is_closed", None) is True or getattr(
        client, "transport_failed", None
    ) is True:
        return None, DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE
    try:
        instance_id = getattr(client, "server_instance_id")
    except AttributeError:
        return None, DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE
    except Exception:
        return None, DaemonTerminalReadinessReason.HANDSHAKE_INCOMPLETE
    if not instance_id:
        return None, DaemonTerminalReadinessReason.SERVER_INSTANCE_MISSING
    return str(instance_id), None


def _classify_missing_capabilities(
    missing: frozenset[Capability],
) -> DaemonTerminalReadiness:
    ordered = tuple(sorted(missing, key=lambda item: item.value))
    if not missing:
        return DaemonTerminalReadiness(ready=True)

    only_terminal = missing <= _TERMINAL_TRANSPORT_CAPS
    only_secret = missing <= _SECRET_TRANSPORT_CAPS
    if only_terminal and missing:
        return DaemonTerminalReadiness(
            ready=False,
            reason=DaemonTerminalReadinessReason.TERMINAL_TRANSPORT_UNAVAILABLE,
            missing_capabilities=ordered,
        )
    if only_secret and missing:
        return DaemonTerminalReadiness(
            ready=False,
            reason=DaemonTerminalReadinessReason.SECRET_TRANSPORT_UNAVAILABLE,
            missing_capabilities=ordered,
        )
    return DaemonTerminalReadiness(
        ready=False,
        reason=DaemonTerminalReadinessReason.MISSING_CAPABILITIES,
        missing_capabilities=ordered,
    )


def resolve_daemon_terminal_readiness(
    client,
    bridge=None,
    *,
    selection_pending: bool = False,
    startup_failure: Optional[DaemonTerminalReadinessReason] = None,
) -> DaemonTerminalReadiness:
    """Inspect whether a daemon-backed SSH launch can proceed.

    Never initiates a frontend SSH launch and never changes routing policy.
    """

    if startup_failure is not None:
        return DaemonTerminalReadiness(ready=False, reason=startup_failure)

    if selection_pending:
        return DaemonTerminalReadiness(
            ready=False,
            reason=DaemonTerminalReadinessReason.SELECTION_PENDING,
        )

    if bridge is None:
        return DaemonTerminalReadiness(
            ready=False,
            reason=DaemonTerminalReadinessReason.BRIDGE_UNAVAILABLE,
        )

    instance_id, instance_reason = _read_server_instance_id(client)
    if instance_reason is not None:
        return DaemonTerminalReadiness(ready=False, reason=instance_reason)
    del instance_id

    try:
        capabilities = client.get_capabilities()
    except Exception:
        return DaemonTerminalReadiness(
            ready=False,
            reason=DaemonTerminalReadinessReason.HANDSHAKE_INCOMPLETE,
        )

    protocol_version = getattr(capabilities, "protocol_version", None)
    compatibility = getattr(capabilities, "compatibility", None)
    compatible = bool(getattr(compatibility, "compatible", False))
    if protocol_version != PROTOCOL_VERSION or not compatible:
        return DaemonTerminalReadiness(
            ready=False,
            reason=DaemonTerminalReadinessReason.PROTOCOL_INCOMPATIBLE,
        )

    try:
        missing = daemon_terminal_capabilities_missing(client)
    except Exception:
        return DaemonTerminalReadiness(
            ready=False,
            reason=DaemonTerminalReadinessReason.HANDSHAKE_INCOMPLETE,
        )
    return _classify_missing_capabilities(frozenset(missing))


def daemon_readiness_user_message(readiness: DaemonTerminalReadiness) -> str:
    """Return a safe, actionable message for a readiness failure."""

    reason = readiness.reason
    if reason is DaemonTerminalReadinessReason.BRIDGE_UNAVAILABLE:
        return _(
            "The GTK service bridge could not be initialized.\n"
            "The connection was not opened in legacy mode."
        )
    if reason is DaemonTerminalReadinessReason.PROTOCOL_INCOMPATIBLE:
        return _(
            "The running SSH Pilot service is incompatible with this "
            "application version.\n"
            "Restart or upgrade the service.\n"
            "The connection was not opened in legacy mode."
        )
    if reason is DaemonTerminalReadinessReason.TERMINAL_TRANSPORT_UNAVAILABLE:
        return _(
            "The running service does not support production terminal sessions.\n"
            "The connection was not opened in legacy mode."
        )
    if reason is DaemonTerminalReadinessReason.SECRET_TRANSPORT_UNAVAILABLE:
        return _(
            "The running service cannot securely handle authentication prompts.\n"
            "The connection was not opened in legacy mode."
        )
    if reason is DaemonTerminalReadinessReason.MISSING_CAPABILITIES:
        return _(
            "The running service does not support production terminal sessions.\n"
            "The connection was not opened in legacy mode."
        )
    if reason is DaemonTerminalReadinessReason.DAEMON_START_TIMEOUT:
        return _(
            "The SSH Pilot background service did not become ready in time.\n"
            "The connection was not opened in legacy mode."
        )
    if reason is DaemonTerminalReadinessReason.DAEMON_START_FAILED:
        return _(
            "The SSH Pilot background service could not be started.\n"
            "The connection was not opened in legacy mode."
        )
    if reason is DaemonTerminalReadinessReason.SELECTION_PENDING:
        return _(
            "The SSH Pilot background service is still starting.\n"
            "Try again in a moment. The connection was not opened in legacy mode."
        )
    if reason in {
        DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE,
        DaemonTerminalReadinessReason.HANDSHAKE_INCOMPLETE,
        DaemonTerminalReadinessReason.SERVER_INSTANCE_MISSING,
    }:
        return _(
            "The SSH Pilot background service is unavailable.\n"
            "The connection was not opened in legacy mode."
        )
    return _(
        "The SSH Pilot background service is unavailable.\n"
        "The connection was not opened in legacy mode."
    )


def resolve_close_policy(
    config,
    setting_key: str,
    default: TerminalClosePolicy = TerminalClosePolicy.DETACH,
) -> TerminalClosePolicy:
    raw = str(_get_setting(config, setting_key, default.value) or "")
    try:
        return TerminalClosePolicy(raw.strip().lower())
    except ValueError:
        return default


def resolve_tab_close_policy(config) -> TerminalClosePolicy:
    """Resolve what closing a terminal tab does to its daemon session.

    Closing a tab ends the session by default. Detaching left the session
    RUNNING in the daemon with no tab owning it, which the sidebar (rightly)
    kept reporting as connected, so the indicator stayed green after the user
    had closed the connection — and the orphan then outvoted every later
    session for that host, so even a clean ``exit`` could not turn it red
    (GH #1176). Detach is still available, but only when asked for.
    """
    return resolve_close_policy(
        config, TAB_CLOSE_POLICY_SETTING, TerminalClosePolicy.TERMINATE
    )


def resolve_app_close_policy(config) -> TerminalClosePolicy:
    """Resolve how quit treats daemon-backed work: confirm first, or just do it.

    Quit always terminates, so DETACH is not a valid app-close policy. A
    configuration still carrying the retired ``detach`` value (or anything
    unrecognized) resolves to ASK, which confirms before tearing live work
    down — the safe reading of a stale preference.
    """
    raw = str(
        _get_setting(config, APP_CLOSE_POLICY_SETTING, TerminalClosePolicy.ASK.value)
        or ""
    )
    try:
        policy = TerminalClosePolicy(raw.strip().lower())
    except ValueError:
        return TerminalClosePolicy.ASK
    if policy is TerminalClosePolicy.DETACH:
        return TerminalClosePolicy.ASK
    return policy


def preferred_daemon_emulator(config) -> str:
    value = str(_get_setting(config, PREFERRED_EMULATOR_SETTING, "vte") or "vte")
    return value.strip().lower() or "vte"


REQUIRED_OPEN_CAPABILITIES = required_daemon_terminal_capabilities()
