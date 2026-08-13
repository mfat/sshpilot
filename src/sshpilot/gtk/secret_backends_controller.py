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
    SecretOperationResult,
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

    def update_selection(
        self, backend: str, expected_revision: Optional[str] = None
    ) -> SecretBackendState:
        """Change the selected backend, revision-checked against the controller's
        loaded configuration snapshot.

        ``expected_revision`` defaults to the revision of the configuration the
        controller last staged (``load_configuration``/``update_configuration``),
        so a stale UI can never silently overwrite a concurrent selection change.
        On a ``REVISION_CONFLICT`` the daemon rejects the mutation and the
        controller keeps both snapshots untouched.

        After a successful selection the authoritative ``SecretConfiguration`` is
        re-fetched and staged as ``_configuration`` (the selection bumped its
        revision), so a following ``update_configuration`` uses the new revision
        instead of conflicting.  The returned ``SecretBackendState`` is staged
        as ``_state``.  On any failure both old snapshots are preserved.
        """
        with self._lock:
            self._ensure_idle()
            if expected_revision is None and self._configuration is not None:
                expected_revision = self._configuration.revision
            self._busy = True
        try:
            snapshot = self._client.update_secret_selection(
                backend, expected_revision=expected_revision
            )
            # The selection created a new revision; fetch it so subsequent
            # mutations start from the authoritative state.
            config = self._client.get_secret_configuration()
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        with self._lock:
            self._configuration = config
            self._state = snapshot
            self._busy = False
        return snapshot

    # -- KeePassXC lifecycle ---------------------------------------------

    def keepassxc_create_database(
        self,
        path: str,
        *,
        keyfile: Optional[str] = None,
    ) -> SecretOperationResult:
        return self._client.keepassxc_create_database(path, keyfile=keyfile)

    def keepassxc_unlock(self) -> SecretOperationResult:
        return self._client.keepassxc_unlock()

    def keepassxc_lock(self) -> SecretOperationResult:
        return self._client.keepassxc_lock()

    # -- remembered master password ---------------------------------------

    def remember_master_password(self) -> SecretOperationResult:
        return self._client.remember_master_password()

    def forget_master_password(self) -> SecretOperationResult:
        return self._client.forget_master_password()

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

    def bitwarden_sso_login(self, identifier: Optional[str] = None) -> BitwardenStatus:
        return self._client.bitwarden_sso_login(identifier=identifier)

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

    def preview_backup(self, *, source: str):
        return self._client.preview_backup(source=source)

    def preview_bitwarden_backup(self, *, entry_id: str):
        return self._client.preview_bitwarden_backup(entry_id=entry_id)

    def preview_ssh_backup(
        self, *, connection_id: str, remote_dir: str, entry_id: str
    ):
        return self._client.preview_ssh_backup(
            connection_id=connection_id,
            remote_dir=remote_dir,
            entry_id=entry_id,
        )

    def list_bitwarden_backups(self):
        return self._client.list_bitwarden_backups()

    def import_bitwarden_backup(
        self,
        *,
        entry_id: str,
        options: Optional[dict] = None,
    ) -> SecretTransferResult:
        return self._client.import_bitwarden_backup(entry_id=entry_id, options=options)

    def list_ssh_backups(self, *, connection_id: str, remote_dir: str):
        return self._client.list_ssh_backups(
            connection_id=connection_id, remote_dir=remote_dir
        )

    def import_ssh_backup(
        self,
        *,
        connection_id: str,
        remote_dir: str,
        entry_id: str,
        options: Optional[dict] = None,
    ) -> SecretTransferResult:
        return self._client.import_ssh_backup(
            connection_id=connection_id,
            remote_dir=remote_dir,
            entry_id=entry_id,
            options=options,
        )
