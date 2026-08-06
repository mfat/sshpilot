"""GTK-free controller for daemon-owned secret backend management.

Stages daemon-owned secret configuration, backend registry, and selection/lock
state through a daemon-backed ``SshPilotClient``. The controller never touches
``SecretManager``, never spawns ``bw``/``rbw``/``pass``, and never carries secret
values: every mutation and lifecycle operation runs inside the daemon, and
sensitive prompts are answered through protected interactions presented by the
GTK layer.

Threading: one ``RLock`` guards the staged snapshots and the ``_busy`` flag so
overlapping operations are rejected deterministically. Client RPCs run outside
the lock so a blocked request never stalls other callers.
"""
from __future__ import annotations

import threading
from typing import Any, Mapping, Optional

from sshpilot.api.client import SshPilotClient
from sshpilot.api.models.secrets import (
    SecretBackendRegistry,
    SecretBackendState,
    SecretConfiguration,
    SecretTransferResult,
    SecretUnlockResult,
    UpdateSecretConfigurationRequest,
    BitwardenStatus,
    RbwStatus,
)


class SecretBackendsController:
    """Stateful frontend controller for the daemon-owned secrets service."""

    def __init__(self, client: SshPilotClient) -> None:
        self._client = client
        self._lock = threading.RLock()
        self._configuration: SecretConfiguration | None = None
        self._registry: SecretBackendRegistry | None = None
        self._state: SecretBackendState | None = None
        self._busy = False

    def _ensure_idle(self) -> None:
        if self._busy:
            raise RuntimeError("a secret backend operation is already in progress")

    # -- reads ----------------------------------------------------------

    def load_configuration(self) -> SecretConfiguration:
        with self._lock:
            self._ensure_idle()
            self._busy = True
        try:
            snapshot = self._client.get_secret_configuration()
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._configuration = snapshot
            self._busy = False
        return snapshot

    def load_registry(self) -> SecretBackendRegistry:
        with self._lock:
            self._ensure_idle()
            self._busy = True
        try:
            snapshot = self._client.get_secret_backends()
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._registry = snapshot
            self._busy = False
        return snapshot

    def load_state(self) -> SecretBackendState:
        with self._lock:
            self._ensure_idle()
            self._busy = True
        try:
            snapshot = self._client.get_secret_state()
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._state = snapshot
            self._busy = False
        return snapshot

    def configuration(self) -> SecretConfiguration | None:
        with self._lock:
            return self._configuration

    def registry(self) -> SecretBackendRegistry | None:
        with self._lock:
            return self._registry

    def state(self) -> SecretBackendState | None:
        with self._lock:
            return self._state

    # -- mutations ------------------------------------------------------

    def update_configuration(
        self,
        patch: Mapping[str, Any],
        expected_revision: Optional[str] = None,
    ) -> SecretConfiguration:
        with self._lock:
            self._ensure_idle()
            current = self._configuration
            self._busy = True
        try:
            if expected_revision is None and current is not None:
                expected_revision = current.revision
            request = UpdateSecretConfigurationRequest(
                patch=dict(patch),
                expected_revision=expected_revision,
            )
            snapshot = self._client.update_secret_configuration(request)
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._configuration = snapshot
            self._busy = False
        return snapshot

    def update_selection(self, backend: str) -> SecretBackendState:
        with self._lock:
            self._ensure_idle()
            self._busy = True
        try:
            snapshot = self._client.update_secret_selection(backend)
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._state = snapshot
            self._busy = False
        return snapshot

    def unlock(self) -> SecretUnlockResult:
        with self._lock:
            self._ensure_idle()
            self._busy = True
        try:
            result = self._client.unlock_secrets()
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._busy = False
        return result

    def lock(self) -> SecretBackendState:
        with self._lock:
            self._ensure_idle()
            self._busy = True
        try:
            snapshot = self._client.lock_secrets()
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._state = snapshot
            self._busy = False
        return snapshot

    # -- Bitwarden / rbw lifecycle (daemon-owned) -----------------------

    def bitwarden_status(self, *, force_refresh: bool = False) -> BitwardenStatus:
        return self._client.bitwarden_status(force_refresh=force_refresh)

    def bitwarden_configure_server(self, url: str) -> BitwardenStatus:
        return self._client.bitwarden_configure_server(url)

    def bitwarden_login(
        self,
        email: str,
        *,
        twofa_method: Optional[str] = None,
    ) -> BitwardenStatus:
        return self._client.bitwarden_login(email, twofa_method=twofa_method)

    def bitwarden_api_key_login(self, client_id: str) -> BitwardenStatus:
        return self._client.bitwarden_api_key_login(client_id)

    def bitwarden_sso_login(self) -> BitwardenStatus:
        return self._client.bitwarden_sso_login()

    def bitwarden_unlock(self) -> BitwardenStatus:
        return self._client.bitwarden_unlock()

    def bitwarden_sync(self) -> BitwardenStatus:
        return self._client.bitwarden_sync()

    def bitwarden_lock(self) -> BitwardenStatus:
        return self._client.bitwarden_lock()

    def bitwarden_logout(self) -> BitwardenStatus:
        return self._client.bitwarden_logout()

    def rbw_status(self) -> RbwStatus:
        return self._client.rbw_status()

    def rbw_configure(self, email: str, base_url: str) -> RbwStatus:
        return self._client.rbw_configure(email, base_url)

    def rbw_unlock(self) -> RbwStatus:
        return self._client.rbw_unlock()

    def rbw_sync(self) -> RbwStatus:
        return self._client.rbw_sync()

    def rbw_lock(self) -> RbwStatus:
        return self._client.rbw_lock()

    # -- transfer (daemon-owned) ----------------------------------------

    def export_backup(
        self,
        *,
        destination: str,
        connection_ids: Optional[list] = None,
        options: Optional[dict] = None,
        mirror_logins: bool = False,
    ) -> SecretTransferResult:
        return self._client.export_secret_backup(
            destination=destination,
            connection_ids=connection_ids,
            options=options,
            mirror_logins=mirror_logins,
        )

    def import_backup(
        self,
        *,
        source: str,
        options: Optional[dict] = None,
    ) -> SecretTransferResult:
        return self._client.import_secret_backup(source=source, options=options)
