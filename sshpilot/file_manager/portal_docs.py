"""Helpers for the XDG Document portal and the granted-folders config.

These let the file manager remember which host folders the user has granted
the app access to (Flatpak), and translate between portal-document IDs,
config entries, and human-readable display paths.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

from gi.repository import Gio, GLib

from ..platform_utils import is_flatpak


logger = logging.getLogger(__name__)


def _get_docs_json_path():
    """Get the path to the granted folders config file."""
    from ..platform_utils import get_config_dir
    try:
        base_dir = get_config_dir()
    except TypeError:
        # Some tests monkeypatch GLib with lightweight stubs that do not
        # implement get_user_config_dir(). Fall back to a sensible default.
        base_dir = os.path.join(os.path.expanduser("~"), ".config", "sshpilot")
    return os.path.join(base_dir, "granted-folders.json")


DOCS_JSON = _get_docs_json_path()


def _ensure_cfg_dir():
    """Ensure the config directory exists."""
    cfg_dir = os.path.dirname(DOCS_JSON)
    os.makedirs(cfg_dir, exist_ok=True)


def _save_doc(folder_path: str, doc_id: str):
    """Save document ID, display name, and actual path to JSON config."""
    _ensure_cfg_dir()
    data = {}
    if os.path.exists(DOCS_JSON):
        try:
            with open(DOCS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[doc_id] = {
        "display": Gio.File.new_for_path(folder_path).get_parse_name(),
        "path": folder_path  # Store the actual path for non-Flatpak lookup
    }
    with open(DOCS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _grant_persistent_access(gfile):
    """Grant persistent access to a file via the Document portal (Flatpak only)."""
    if not is_flatpak():
        # In non-Flatpak environments, generate a simple ID from the path
        path = gfile.get_path()
        import hashlib
        doc_id = hashlib.md5(path.encode()).hexdigest()[:16]
        logger.debug(f"Generated simple doc ID for non-Flatpak: {doc_id}")
        return doc_id

    path = gfile.get_path()
    if not path:
        logger.warning("Cannot grant persistent access without a path")
        return None

    try:
        # Get the Document portal (only in Flatpak)
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/documents",
            "org.freedesktop.portal.Documents",
            None
        )

        fd_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY") and os.path.isdir(path):
            fd_flags |= os.O_DIRECTORY

        fd = os.open(path, fd_flags)
        try:
            fd_list = Gio.UnixFDList.new()
            fd_index = fd_list.append(fd)

            is_directory = os.path.isdir(path)
            flags = 1 | 2  # reuse_existing | persistent
            if is_directory:
                flags |= 8  # export-directory

            app_id = os.environ.get("FLATPAK_ID", "")
            basename = gfile.get_basename() or os.path.basename(path)

            permissions: List[str] = ["read"]
            if os.access(path, os.W_OK):
                permissions.append("write")

            # AddFull method signature according to the docs: (ah, u, s, as)
            parameters = GLib.Variant(
                "(ahusas)",
                ([fd_index], flags, app_id, permissions)
            )

            result = proxy.call_with_unix_fd_list_sync(
                "AddFull",
                parameters,
                Gio.DBusCallFlags.NONE,
                -1,
                fd_list,
                None
            )
        finally:
            os.close(fd)

        if result:
            doc_ids = result.get_child_value(0).unpack()
            doc_id = doc_ids[0] if doc_ids else None
            if doc_id:
                logger.info(
                    "Granted persistent access via Document portal, doc_id: %s", doc_id
                )
                return doc_id

    except Exception as e:
        logger.warning(f"Failed to grant persistent access via Document portal: {e}")

    # Fallback to simple ID generation
    path = gfile.get_path()
    if path:
        import hashlib
        doc_id = hashlib.md5(path.encode()).hexdigest()[:16]
        logger.debug(f"Using fallback doc ID: {doc_id}")
        return doc_id
    return None


def _lookup_document_path(doc_id: str):
    """Look up the current path for a document ID."""
    # Always try config lookup first since Document portal seems unreliable
    config_path = _lookup_path_from_config(doc_id)
    if config_path and os.path.exists(config_path):
        logger.debug(f"Found valid path from config for {doc_id}: {config_path}")
        return config_path

    # Only try Document portal in Flatpak and if config lookup failed
    if not is_flatpak():
        return None

    try:
        # Get the Document portal (only in Flatpak)
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/documents",
            "org.freedesktop.portal.Documents",
            None
        )

        # Use GetHostPaths to get the path from doc_id
        # GetHostPaths(IN as doc_ids, OUT a{say} paths)
        # This method is available inside the sandbox (version 5+)
        result = proxy.call_sync(
            "GetHostPaths",
            GLib.Variant("(as)", ([doc_id],)),
            Gio.DBusCallFlags.NONE,
            -1,
            None
        )

        if result:
            # Result is a dictionary: {doc_id: path_bytes}
            paths_dict = result.get_child_value(0).unpack()
            if doc_id in paths_dict:
                path_bytes = paths_dict[doc_id]
                path = path_bytes.decode('utf-8')
                logger.debug(f"Document portal GetHostPaths for {doc_id}: {path}")
                return path

    except Exception as e:
        logger.debug(f"Document portal GetHostPaths failed for {doc_id}: {e}")

    return None


def _lookup_path_from_config(doc_id: str):
    """Look up the original path from our config."""
    try:
        entry = _lookup_doc_entry(doc_id)
        if entry:

            # First try the 'path' field (new format)
            if 'path' in entry:
                path = entry['path']
                if os.path.exists(path):
                    return path

            # Fallback to 'display' field (old format)
            display = entry.get('display', '')
            if display:
                # If it's a portal path, try it directly
                if '/doc/' in display:
                    if os.path.exists(display):
                        return display
                # If it starts with ~, expand it
                elif display.startswith('~'):
                    expanded = os.path.expanduser(display)
                    if os.path.exists(expanded):
                        return expanded
                # Try as-is
                elif os.path.exists(display):
                    return display

            # Last resort: try to construct portal path from doc_id
            if is_flatpak():
                portal_path = f"/run/user/{os.getuid()}/doc/{doc_id}"
                if os.path.isdir(portal_path):
                    return portal_path

    except Exception as e:
        logger.debug(f"Failed to lookup path from config: {e}")
    return None


def _portal_doc_path(doc_id: str) -> str:
    """Get the portal mount path for a document ID."""
    return f"/run/user/{os.getuid()}/doc/{doc_id}"


def _load_doc_config() -> Dict[str, Dict[str, str]]:
    """Load the granted folders configuration file."""

    if not os.path.exists(DOCS_JSON):
        return {}

    try:
        with open(DOCS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                # Ensure we only keep dictionary entries
                return {
                    key: value
                    for key, value in data.items()
                    if isinstance(value, dict)
                }
    except Exception as exc:  # pragma: no cover - config parsing errors are non-fatal
        logger.debug(f"Failed to load granted folders config: {exc}")

    return {}


def _lookup_doc_entry(doc_id: str) -> Optional[Dict[str, str]]:
    """Return the stored configuration entry for the given document ID."""

    config = _load_doc_config()
    entry = config.get(doc_id)
    if isinstance(entry, dict):
        return entry
    return None


def _load_first_doc_path():
    """Load the first valid document portal path from saved config."""
    logger.debug(f"Looking for config file: {DOCS_JSON}")

    config = _load_doc_config()
    if not config:
        logger.debug("Config file does not exist or is empty")
        return None

    for doc_id, entry in config.items():
        logger.debug(f"Looking up document ID: {doc_id}")
        portal_path = _lookup_document_path(doc_id)
        if portal_path and os.path.isdir(portal_path):
            logger.debug(f"Found valid portal path: {portal_path}")
            return portal_path, doc_id, entry

        logger.debug(f"Document ID {doc_id} is no longer valid")

    logger.debug("No valid portal paths found")
    return None


def _pretty_path_for_display(path: str) -> str:
    """Convert a filesystem path to a human-friendly display string.

    Uses GFile's parse_name for human-readable presentation (often shows "~" etc.).
    For document portal paths, shows just the folder name instead of the full mount path.
    """
    try:
        gfile = Gio.File.new_for_path(path)
        parse_name = gfile.get_parse_name()

        # If it's a doc mount, show a human-friendly version
        if "/doc/" in path and parse_name.startswith("/run/"):
            # Extract the final directory name from the portal path
            basename = gfile.get_basename()
            if basename:
                # For home directory, show it as ~/username
                if basename == os.path.basename(os.path.expanduser("~")):
                    return f"~/{basename}"
                # For other directories, just show the basename
                return basename
            # Fall back to the full path if basename fails
            return parse_name
        return parse_name
    except Exception:
        # Fallback to original path if GFile operations fail
        return path
