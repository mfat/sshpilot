"""A daemon client for editor GUI tests that answers with real OpenSSH.

The connection editor asks ``evaluate_connection_edit`` after each edit.
Instead of a canned answer, this client puts the question to the daemon's
own service, on a configuration written to a temporary folder, so the
marks a test checks are the marks OpenSSH's real answer gives.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from sshpilot.api.models.common import ConnectionId


class RealConfigClient:
    """``evaluate_connection_edit`` (and ``get_effective_config``) of the
    daemon's service, on *text* as the SSH configuration.

    *forced* are Settings ▸ SSH Options, as ``Keyword=value`` strings.
    """

    def __init__(self, text: str, *, forced=(), folder=None):
        from sshpilot.core.connection_application_service import (
            ConnectionApplicationService,
        )
        from sshpilot.core.connections.repository import ConnectionRepository
        from sshpilot.core.connections.ssh_config_store import SshConfigStore

        self.folder = Path(folder or tempfile.mkdtemp(prefix="sshpilot-editor-rig-"))
        self.root = self.folder / "ssh_config"
        self.root.write_text(text, encoding="utf-8")
        repository = ConnectionRepository(
            ssh_store=SshConfigStore(self.root, isolated=True),
            state_path=self.folder / "connections.json",
            legacy_config_path=self.folder / "config.json",
            isolated=True,
        )
        self.service = ConnectionApplicationService(
            repository, client_name="test", allow_cross_thread_commands=True,
        )
        args = []
        for option in forced:
            args += ["-o", option]
        self.service._forced_ssh_option_args = lambda: tuple(args)
        self._lock = threading.Lock()
        self.questions = []

    def evaluate_connection_edit(self, request):
        with self._lock:
            self.questions.append(request)
            return self.service.evaluate_connection_edit(request)

    def get_effective_config(self, connection_id):
        with self._lock:
            return self.service.get_effective_config(ConnectionId(str(connection_id)))
