"""File-manager subpackage.

Step 4 of the refactor plan extracts the monolithic ``file_manager_window``
module into focused submodules.  Phase 4a moved the low-coupling helpers
(portal/docs handling, format helpers, paramiko walk helpers, the
cancellation exception).  Phase 4b moved the standalone dialogs and
pane-level UI controls (``SFTPProgressDialog``, ``PathEntry``,
``PaneControls``, ``PaneToolbar``, ``PropertiesDialog``).  Phase 4c
extracted the heavy widget classes: ``AsyncSFTPManager`` (4c-i) and
``FilePane`` (4c-ii).  ``FileManagerWindow`` remains in
``sshpilot.file_manager_window`` and will move in 4c-iii.
"""

from .exceptions import TransferCancelledException
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
from .sftp_manager import AsyncSFTPManager, FileEntry, _MainThreadDispatcher
from .portal_docs import (
    DOCS_JSON,
    _ensure_cfg_dir,
    _get_docs_json_path,
    _grant_persistent_access,
    _load_doc_config,
    _load_first_doc_path,
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

__all__ = [
    "AsyncSFTPManager",
    "DOCS_JSON",
    "FileEntry",
    "PaneControls",
    "PaneToolbar",
    "PathEntry",
    "PropertiesDialog",
    "SFTPProgressDialog",
    "TransferCancelledException",
    "_HAS_ALERT_DIALOG",
    "_MainThreadDispatcher",
    "_PROGRESS_DIALOG_BASE",
    "_ensure_cfg_dir",
    "_get_docs_json_path",
    "_grant_persistent_access",
    "_human_size",
    "_human_time",
    "_load_doc_config",
    "_load_first_doc_path",
    "_lookup_doc_entry",
    "_lookup_document_path",
    "_lookup_path_from_config",
    "_mode_to_octal",
    "_mode_to_str",
    "_portal_doc_path",
    "_pretty_path_for_display",
    "_save_doc",
    "_sftp_path_exists",
    "stat_isdir",
    "walk_remote",
]
