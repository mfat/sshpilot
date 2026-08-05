"""GTK-free SSH overrides controller.

Stages the current daemon-owned global SSH overrides snapshot and performs
revision-checked mutations through a daemon-backed ``SshPilotClient``. No
filesystem or GTK imports; the controller is a plain value/operation holder
consumed by the Preferences SSH Options page.

Threading: one ``RLock`` guards the snapshot and the ``_busy`` flag so
overlapping operations are rejected deterministically. Client RPCs run
*outside* the lock so a blocked request never stalls other callers.
"""
from __future__ import annotations

import threading
from typing import Any, Mapping, Optional

from sshpilot.api.client import SshPilotClient
from sshpilot.api.models.settings import (
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
)


class SshOverridesController:
    """Stateful frontend controller for the global SSH overrides page."""

    def __init__(self, client: SshPilotClient) -> None:
        self._client = client
        self._lock = threading.RLock()
        self._snapshot: GlobalSshOverrides | None = None
        self._busy = False

    def _ensure_idle(self) -> None:
        if self._busy:
            raise RuntimeError("an SSH overrides operation is already in progress")

    def load(self) -> GlobalSshOverrides:
        with self._lock:
            self._ensure_idle()
            self._busy = True

        try:
            snapshot = self._client.get_global_ssh_overrides()
        except BaseException:
            with self._lock:
                self._busy = False
            raise

        with self._lock:
            self._snapshot = snapshot
            self._busy = False

        return snapshot

    def snapshot(self) -> GlobalSshOverrides | None:
        with self._lock:
            return self._snapshot

    def update(
        self,
        patch: Mapping[str, Any],
        expected_revision: Optional[str] = None,
    ) -> GlobalSshOverrides:
        with self._lock:
            self._ensure_idle()
            current = self._snapshot
            self._busy = True

        try:
            if expected_revision is None and current is not None:
                expected_revision = current.revision
            request = UpdateGlobalSshOverridesRequest(
                patch=dict(patch),
                expected_revision=expected_revision,
            )
            snapshot = self._client.update_global_ssh_overrides(request)
        except BaseException:
            with self._lock:
                self._busy = False
            raise

        with self._lock:
            self._snapshot = snapshot
            self._busy = False

        return snapshot

    def reset(
        self,
        expected_revision: Optional[str] = None,
    ) -> GlobalSshOverrides:
        with self._lock:
            self._ensure_idle()
            current = self._snapshot
            self._busy = True

        try:
            if expected_revision is None and current is not None:
                expected_revision = current.revision
            snapshot = self._client.reset_global_ssh_overrides(
                expected_revision=expected_revision
            )
        except BaseException:
            with self._lock:
                self._busy = False
            raise

        with self._lock:
            self._snapshot = snapshot
            self._busy = False

        return snapshot
