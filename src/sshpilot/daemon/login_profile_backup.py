"""Login profiles inside the portable ``connection_store`` backup section.

Profiles belong with groups and connection metadata (their links live there),
so the daemon embeds them as a ``login_profiles`` key of the existing
section instead of adding a new top-level archive section. They are restored
before the connection store, so the links it restores find their profiles. Profile secrets are exported with the other backup credentials,
under the same ``secrets`` option, and restored by the generic credential
path because they are ordinary login-password specs.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping

from sshpilot.api.models.secrets import SecretTransferMessage, SecretTransferMessageCode
from sshpilot.core.errors import CoreError
from sshpilot.core.login_profiles.models import profile_secret_host

logger = logging.getLogger(__name__)

SECTION_KEY = "login_profiles"


class ConnectionStoreBackupSnapshot:
    """Callable ``connection_store_snapshot`` that embeds login profiles.

    Also exposes :meth:`backup_credentials`, which the backup manager uses to
    include profile secrets with the other credentials.
    """

    def __init__(self, repository_snapshot: Callable[[], Dict[str, Any]], profiles: Any) -> None:
        self._repository_snapshot = repository_snapshot
        self._profiles = profiles

    def __call__(self) -> Dict[str, Any]:
        section = dict(self._repository_snapshot())
        section[SECTION_KEY] = self._profiles.snapshot_for_backup()
        return section

    def backup_credentials(self) -> List[Dict[str, Any]]:
        out = []
        for profile_id, account, value in self._profiles.backup_secrets():
            host = profile_secret_host(profile_id)
            out.append(
                {
                    "id": f"{account}@{host}",
                    "type": "password",
                    "host": host,
                    "username": account,
                    "secret": value,
                    "metadata": {"login_profile": profile_id},
                }
            )
        return out


class ConnectionStoreBackupRestore:
    """Callable ``connection_store_restore`` that restores embedded profiles."""

    def __init__(self, repository_restore: Callable[..., Any], profiles: Any) -> None:
        self._repository_restore = repository_restore
        self._profiles = profiles

    def __call__(self, section: Mapping[str, Any], *, mode: str = "merge"):
        section = dict(section)
        profiles_section = section.pop(SECTION_KEY, None)
        # Profiles first: restoring the connection store publishes a change
        # that reconciles links, and a link whose profile does not exist yet
        # would be detached as "profile missing".
        warning = None
        if isinstance(profiles_section, Mapping):
            try:
                self._profiles.restore_from_backup(profiles_section, mode=mode)
            except CoreError as error:
                logger.warning("Login profiles were not restored: %s", error.message)
                warning = SecretTransferMessage(
                    code=SecretTransferMessageCode.CONNECTION_STORE_RESTORE_FAILED,
                    diagnostic="login profiles",
                )
        result = self._repository_restore(section, mode=mode)
        if warning is None:
            return result
        from dataclasses import replace

        return replace(result, warnings=tuple(result.warnings) + (warning,))
