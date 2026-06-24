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


def _resolve_backend_name(explicit=None) -> str:
    """Return the configured file-manager backend ('paramiko' | 'openssh')."""

    if explicit:
        name = str(explicit).strip().lower()
        return name if name in {"paramiko", "openssh"} else "paramiko"
    try:
        from ..config import Config

        backend = Config().get_file_manager_config().get("backend", "paramiko")
    except Exception:
        backend = "paramiko"
    name = str(backend or "paramiko").strip().lower()
    return name if name in {"paramiko", "openssh"} else "paramiko"


def create_file_manager_backend(*args, backend=None, **kwargs):
    """Construct the configured file-manager backend.

    Both backends share the same constructor signature and public contract, so
    the window can use either interchangeably. The backend is chosen by the
    ``file_manager.backend`` setting unless overridden via ``backend=``.
    """

    name = _resolve_backend_name(backend)
    if name == "openssh":
        from .openssh_backend import OpenSSHSFTPManager

        return OpenSSHSFTPManager(*args, **kwargs)
    return AsyncSFTPManager(*args, **kwargs)


__all__ = [
    "AsyncSFTPManager",
    "create_file_manager_backend",
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
