"""Daemon-only GTK client composition.

User interfaces never construct core services.  Failure to connect or launch is
reported to the caller so the UI can offer explicit recovery actions.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from sshpilot.daemon.launcher import DaemonLauncher, DaemonProcessHandle
from .client import SshPilotClient


@dataclass(frozen=True)
class ClientSelection:
    """A validated daemon connection and optional process launched for it."""
    client: SshPilotClient
    daemon_process: Optional[DaemonProcessHandle] = None


def select_client(*, launcher: Optional[DaemonLauncher] = None) -> ClientSelection:
    """Locate or launch the daemon; propagate every failure without fallback."""
    launched = (launcher or DaemonLauncher()).connect_or_start()
    return ClientSelection(client=launched.client, daemon_process=launched.process)
