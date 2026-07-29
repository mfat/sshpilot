"""Application composition policy for frontend-neutral SSH Pilot clients."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional

from sshpilot.daemon.launcher import (
    DaemonLaunchError,
    DaemonLauncher,
    DaemonProcessHandle,
)

from .client import SshPilotClient
from .in_process_client import InProcessClient

logger = logging.getLogger(__name__)

CLIENT_MODE_ENVIRONMENT = "SSHPILOT_CLIENT_MODE"
DAEMON_FALLBACK_MESSAGE = (
    "The local SSH Pilot service could not be used. "
    "SSH Pilot is running in compatibility mode."
)


class ClientMode(str, Enum):
    IN_PROCESS = "in_process"
    DAEMON = "daemon"


class ClientFallbackReason(str, Enum):
    INVALID_MODE = "invalid_mode"
    DAEMON_STARTUP_FAILED = "daemon_startup_failed"
    UNEXPECTED_ERROR = "unexpected_error"


@dataclass(frozen=True)
class ClientSelection:
    client: SshPilotClient
    mode: ClientMode
    daemon_process: Optional[DaemonProcessHandle] = None
    fallback_reason: Optional[ClientFallbackReason] = None
    warning: Optional[str] = None


def parse_client_mode(value: Optional[str]) -> ClientMode:
    """Parse the experimental mode setting.

    Case and surrounding ASCII/Unicode whitespace are ignored. Missing or blank
    values retain the production ``in_process`` default.
    """

    normalized = "" if value is None else value.strip().lower()
    if not normalized:
        return ClientMode.IN_PROCESS
    try:
        return ClientMode(normalized)
    except ValueError:
        raise ValueError("invalid SSH Pilot client mode") from None


def requested_client_mode(
    environment: Optional[Mapping[str, str]] = None,
) -> ClientMode:
    source = os.environ if environment is None else environment
    return parse_client_mode(source.get(CLIENT_MODE_ENVIRONMENT))


def in_process_selection(
    connection_manager,
    *,
    group_manager=None,
    existing_client: Optional[InProcessClient] = None,
    fallback_reason: Optional[ClientFallbackReason] = None,
) -> ClientSelection:
    client = existing_client or InProcessClient(
        connection_manager,
        group_manager=group_manager,
    )
    return ClientSelection(
        client=client,
        mode=ClientMode.IN_PROCESS,
        fallback_reason=fallback_reason,
        warning=DAEMON_FALLBACK_MESSAGE if fallback_reason is not None else None,
    )


def select_client(
    connection_manager,
    *,
    group_manager=None,
    mode: ClientMode = ClientMode.IN_PROCESS,
    in_process_client: Optional[InProcessClient] = None,
    launcher: Optional[DaemonLauncher] = None,
) -> ClientSelection:
    """Select one implementation; daemon failures fall back once and visibly."""

    if mode is ClientMode.IN_PROCESS:
        return in_process_selection(
            connection_manager,
            group_manager=group_manager,
            existing_client=in_process_client,
        )

    fallback = in_process_client or InProcessClient(
        connection_manager,
        group_manager=group_manager,
    )
    daemon_launcher = launcher or DaemonLauncher()
    try:
        launched = daemon_launcher.connect_or_start()
    except DaemonLaunchError as error:
        logger.warning(
            "Experimental daemon mode unavailable; using in-process client "
            "reason=%s",
            error.reason.value,
        )
        return in_process_selection(
            connection_manager,
            group_manager=group_manager,
            existing_client=fallback,
            fallback_reason=ClientFallbackReason.DAEMON_STARTUP_FAILED,
        )
    except Exception as error:
        # Log only the exception type. Raw messages may contain process,
        # environment, or socket data and are not needed for this safe fallback.
        logger.error(
            "Experimental daemon mode failed unexpectedly; using in-process "
            "client type=%s",
            type(error).__name__,
        )
        return in_process_selection(
            connection_manager,
            group_manager=group_manager,
            existing_client=fallback,
            fallback_reason=ClientFallbackReason.UNEXPECTED_ERROR,
        )

    logger.info("Experimental daemon client mode is active")
    mismatch = None
    if hasattr(launched.client, "build_mismatch"):
        try:
            mismatch = launched.client.build_mismatch()
        except Exception:
            mismatch = None
    if mismatch:
        logger.warning(
            "Connected daemon build metadata differs from this application "
            "(%s). Restart the SSH Pilot service before relying on new "
            "daemon behavior.",
            mismatch,
        )
    return ClientSelection(
        client=launched.client,
        mode=ClientMode.DAEMON,
        daemon_process=launched.process,
        warning=(
            "The running SSH Pilot service does not match this application "
            "build. Restart the service after updating daemon code."
            if mismatch
            else None
        ),
    )
