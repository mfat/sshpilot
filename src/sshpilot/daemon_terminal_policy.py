"""Close policies, settings keys, and rollout helpers for daemon terminals.

Emulator decision: VTE is the production daemon SSH emulator (``feed`` +
``commit``). PyXtermJS remains available via ``terminal.backend`` for local
terminals; daemon SSH uses one VTE-feed production path.

Local terminals remain GTK-owned. External terminals remain
external-process-owned. Rollout Stage C defaults daemon-backed SSH on, with
legacy local SSH only behind an explicit setting.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from .api.capabilities import Capability
from .file_manager_integration import should_hide_external_terminal_options
from .terminal_session_controller import daemon_terminal_capabilities_missing


class TerminalClosePolicy(str, Enum):
    """Policy for handling daemon SSH terminal close operations."""

    DETACH = "detach"
    TERMINATE = "terminate"
    ASK = "ask"


DAEMON_BACKED_SSH_SETTING = "terminal.daemon_backed_ssh"
TAB_CLOSE_POLICY_SETTING = "terminal.daemon_tab_close_policy"
APP_CLOSE_POLICY_SETTING = "terminal.daemon_app_close_policy"
RESTORE_SESSIONS_SETTING = "terminal.daemon_restore_sessions"
AUTO_ATTACH_SETTING = "terminal.daemon_auto_attach"
LEGACY_LOCAL_SSH_FALLBACK_SETTING = "terminal.legacy_local_ssh_fallback"
PREFERRED_EMULATOR_SETTING = "terminal.daemon_emulator"
SESSION_RESTORE_STATE_SETTING = "terminal.daemon_session_restore_state"

# Stage C: default on for supported installs; legacy fallback stays explicit.
DEFAULT_DAEMON_BACKED_SSH = True


def _config_of(window_or_config: Any):
    if hasattr(window_or_config, "config"):
        return window_or_config.config
    return window_or_config


def _get_setting(config, key: str, default=None):
    getter = getattr(config, "get_setting", None)
    if callable(getter):
        return getter(key, default)
    return default


def should_use_daemon_ssh_terminal(
    window_or_config,
    connection,
    *,
    client=None,
    is_local: bool = False,
) -> bool:
    """Return whether ordinary internal SSH activation should use the daemon."""

    config = _config_of(window_or_config)
    if is_local:
        return False
    if connection is None or getattr(connection, "protocol", "ssh") != "ssh":
        return False
    if _get_setting(config, LEGACY_LOCAL_SSH_FALLBACK_SETTING, False):
        return False
    if not bool(_get_setting(config, DAEMON_BACKED_SSH_SETTING, DEFAULT_DAEMON_BACKED_SSH)):
        return False

    use_external = bool(_get_setting(config, "use-external-terminal", False))
    if use_external and not should_hide_external_terminal_options():
        return False

    resolved_client = client
    if resolved_client is None and hasattr(window_or_config, "client"):
        resolved_client = window_or_config.client
    if resolved_client is None:
        return False
    if not hasattr(resolved_client, "open_session"):
        return False
    if not hasattr(resolved_client, "server_instance_id"):
        return False
    if daemon_terminal_capabilities_missing(resolved_client):
        return False
    return True


def resolve_close_policy(config, setting_key: str) -> TerminalClosePolicy:
    raw = str(_get_setting(config, setting_key, TerminalClosePolicy.DETACH.value) or "")
    try:
        return TerminalClosePolicy(raw.strip().lower())
    except ValueError:
        return TerminalClosePolicy.DETACH


def resolve_tab_close_policy(config) -> TerminalClosePolicy:
    return resolve_close_policy(config, TAB_CLOSE_POLICY_SETTING)


def resolve_app_close_policy(config) -> TerminalClosePolicy:
    return resolve_close_policy(config, APP_CLOSE_POLICY_SETTING)


def preferred_daemon_emulator(config) -> str:
    value = str(_get_setting(config, PREFERRED_EMULATOR_SETTING, "vte") or "vte")
    return value.strip().lower() or "vte"


REQUIRED_OPEN_CAPABILITIES = frozenset(
    {
        Capability.SESSIONS_READ,
        Capability.SESSIONS_WRITE,
        Capability.SESSIONS_EVENTS,
        Capability.TERMINAL_OUTPUT,
        Capability.TERMINAL_INPUT,
        Capability.TERMINAL_RESIZE,
        Capability.TERMINAL_REPLAY,
    }
)
