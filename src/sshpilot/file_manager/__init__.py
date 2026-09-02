"""File-manager subpackage.

Step 4 of the refactor plan extracts the monolithic ``file_manager_window``
module into focused submodules.  Phase 4a moved the low-coupling helpers
(portal/docs handling, format helpers, remote walk helpers, the
cancellation exception).  Phase 4b moved the standalone dialogs and
pane-level UI controls (``SFTPProgressDialog``, ``PathEntry``,
``PaneControls``, ``PaneToolbar``, ``PropertiesDialog``).  Phase 4c
extracted ``FilePane`` (4c-ii).  The in-app backend is now the daemon-owned
``DaemonSftpManager``; ``FileManagerWindow`` remains in
``sshpilot.file_manager_window``.
"""

import logging

from .exceptions import TransferCancelledException

logger = logging.getLogger(__name__)
from .format_utils import (
    _human_size,
    _human_time,
    _mode_to_octal,
    _mode_to_str,
    safe_display_text,
)
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
    config=None,
    prefer_daemon=None,
    **kwargs,
):
    """Construct the daemon-backed file-manager presentation backend."""

    from ..api.daemon_client import DaemonClient
    from ..extended_service_policy import (
        daemon_sftp_unavailable_message,
        resolve_file_manager_route,
        ExtendedServiceRoute,
    )

    route_config = config
    if route_config is None and parent_widget is not None:
        route_config = getattr(parent_widget, "config", None)

    route = resolve_file_manager_route(
        route_config,
        prefer_daemon=prefer_daemon,
        client=daemon_client,
    )

    if (
        isinstance(daemon_client, DaemonClient)
        and bridge is not None
        and connection_id is not None
    ):
        from ..daemon_sftp_backend import (
            DaemonSftpManager,
            daemon_file_manager_capabilities_missing,
        )

        missing = daemon_file_manager_capabilities_missing(daemon_client)
        if missing:
            raise RuntimeError(daemon_sftp_unavailable_message(missing=missing))
        logger.info("File manager backend: daemon-sftp")
        return DaemonSftpManager(
            *args,
            connection_id=connection_id,
            daemon_client=daemon_client,
            bridge=bridge,
            parent_widget=parent_widget,
            **kwargs,
        )

    if route is ExtendedServiceRoute.DAEMON:
        raise RuntimeError(daemon_sftp_unavailable_message())

    raise RuntimeError(daemon_sftp_unavailable_message())


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
    "safe_display_text",
    "stat_isdir",
    "walk_remote",
]
