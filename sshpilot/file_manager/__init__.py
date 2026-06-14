"""File-manager subpackage.

Step 4 of the refactor plan extracts the monolithic ``file_manager_window``
module into focused submodules. This first phase moves the small,
low-coupling helpers (portal/docs handling, format helpers, the paramiko
walk helpers, and the cancellation exception). The heavy widget classes
(``AsyncSFTPManager``, ``FilePane``, ``FileManagerWindow``, dialogs) still
live in ``sshpilot.file_manager_window`` and will move in follow-up PRs.
"""

from .exceptions import TransferCancelledException
from .format_utils import _human_size, _human_time, _mode_to_octal, _mode_to_str
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
from .remote_walk import _sftp_path_exists, stat_isdir, walk_remote

__all__ = [
    "DOCS_JSON",
    "TransferCancelledException",
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
