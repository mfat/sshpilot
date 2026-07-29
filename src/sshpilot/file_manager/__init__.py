"""File-manager subpackage.

Step 4 of the refactor plan extracts the monolithic ``file_manager_window``
module into focused submodules.  Phase 4a moved the low-coupling helpers
(portal/docs handling, format helpers, remote walk helpers, the
cancellation exception).  Phase 4b moved the standalone dialogs and
pane-level UI controls (``SFTPProgressDialog``, ``PathEntry``,
``PaneControls``, ``PaneToolbar``, ``PropertiesDialog``).  Phase 4c
extracted ``FilePane`` (4c-ii) and the OpenSSH SFTP backend (4c-i).
``FileManagerWindow`` remains in ``sshpilot.file_manager_window`` and will
move in 4c-iii.
"""

import logging

from .exceptions import TransferCancelledException

logger = logging.getLogger(__name__)
from .format_utils import _human_size, _human_time, _mode_to_octal, _mode_to_str
from .pane import (
    FilePane,
    _DEFAULT_ICON_LEVEL,
    _GRID_ICON_SIZES,
    _LIST_ICON_SIZES,
    _MAX_ICON_LEVEL,
    _MIN_ICON_LEVEL,
)
from .pane_controls import PaneControls, PaneToolbar, PathEntry
from .common import FileEntry, _MainThreadDispatcher
from .portal_docs import (
    DOCS_JSON,
    _ensure_cfg_dir,
    _get_docs_json_path,
    _grant_persistent_access,
    _load_doc_config,
    _load_first_doc_path,
    _load_grant_for_host,
    _lookup_doc_entry,
    _lookup_document_path,
    _lookup_path_from_config,
    _portal_doc_path,
    _pretty_path_for_display,
    _save_doc,
)
from .progress_dialog import (
    _HAS_ALERT_DIALOG,
    _PROGRESS_DIALOG_BASE,
    SFTPProgressDialog,
)
from .properties_dialog import PropertiesDialog
from .remote_walk import _sftp_path_exists, stat_isdir, walk_remote


def create_file_manager_backend(
    *args,
    daemon_client=None,
    bridge=None,
    connection_id=None,
    parent_widget=None,
    **kwargs,
):
    """Construct the file-manager backend.

    Prefers the daemon-backed SFTP manager (no local ``ssh -s sftp``
    subprocess) when ``daemon_client``/``bridge``/``connection_id`` are all
    supplied and ``daemon_client`` is a real out-of-process ``DaemonClient``
    with the required SFTP/transfer capabilities. Raises rather than
    silently spawning a local subprocess if that client is missing
    capabilities the running service should have (mirrors the daemon
    terminal policy — no silent fallback once daemon mode is selected).

    Falls back to the legacy ``OpenSSHSFTPManager`` (driving a native
    ``ssh -s sftp`` subprocess) when no daemon client is supplied, or when
    the supplied client is the in-process adapter rather than a real daemon
    connection (legacy path, kept until in-process mode is removed).
    """

    if daemon_client is not None and bridge is not None and connection_id is not None:
        from ..api.daemon_client import DaemonClient

        if isinstance(daemon_client, DaemonClient):
            from ..daemon_sftp_backend import (
                DaemonSftpManager,
                daemon_file_manager_capabilities_missing,
            )

            missing = daemon_file_manager_capabilities_missing(daemon_client)
            if missing:
                raise RuntimeError(
                    "The running SSH Pilot service does not support the file "
                    f"manager's SFTP API (missing: {sorted(c.value for c in missing)}); "
                    "refusing to spawn a local SFTP subprocess."
                )
            logger.info("File manager backend: daemon-sftp")
            return DaemonSftpManager(
                *args,
                connection_id=connection_id,
                daemon_client=daemon_client,
                bridge=bridge,
                parent_widget=parent_widget,
                **kwargs,
            )

        logger.info(
            "Daemon SFTP unavailable (client is %s, not a DaemonClient); "
            "using legacy OpenSSH SFTP backend",
            type(daemon_client).__name__,
        )

    from .openssh_backend import OpenSSHSFTPManager

    logger.info("File manager backend: openssh")
    return OpenSSHSFTPManager(*args, **kwargs)


__all__ = [
    "DOCS_JSON",
    "_DEFAULT_ICON_LEVEL",
    "_GRID_ICON_SIZES",
    "_HAS_ALERT_DIALOG",
    "_LIST_ICON_SIZES",
    "_MAX_ICON_LEVEL",
    "_MIN_ICON_LEVEL",
    "_PROGRESS_DIALOG_BASE",
    "FileEntry",
    "FilePane",
    "PaneControls",
    "PaneToolbar",
    "PathEntry",
    "PropertiesDialog",
    "SFTPProgressDialog",
    "TransferCancelledException",
    "_MainThreadDispatcher",
    "_ensure_cfg_dir",
    "_get_docs_json_path",
    "_grant_persistent_access",
    "_human_size",
    "_human_time",
    "_load_doc_config",
    "_load_first_doc_path",
    "_load_grant_for_host",
    "_lookup_doc_entry",
    "_lookup_document_path",
    "_lookup_path_from_config",
    "_mode_to_octal",
    "_mode_to_str",
    "_portal_doc_path",
    "_pretty_path_for_display",
    "_save_doc",
    "_sftp_path_exists",
    "create_file_manager_backend",
    "stat_isdir",
    "walk_remote",
]
