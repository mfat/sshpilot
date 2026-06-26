"""Helpers for the XDG Document portal and the granted-folders config.

These let the file manager remember which host folders the user has granted
the app access to (Flatpak), and translate between portal-document IDs,
config entries, and human-readable display paths.
"""

from __future__ import annotations

import json
import logging
import os
import re
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


def _host_path_for_doc(doc_id: str) -> Optional[str]:
    """Return the real host path for ``doc_id`` via the Document portal's
    ``GetHostPaths`` (Flatpak only).

    For DISPLAY purposes — the returned path (e.g. ``/home/user/Downloads``) is
    the user's real folder and is NOT necessarily accessible inside the sandbox.
    Use ``_lookup_document_path`` for the portal-mounted, writable path.
    """
    if not doc_id or not is_flatpak():
        return None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            # GetHostPaths lives on the Documents portal, which is a *separate*
            # bus name from the Desktop portal (org.freedesktop.portal.Desktop).
            "org.freedesktop.portal.Documents",
            "/org/freedesktop/portal/documents",
            "org.freedesktop.portal.Documents",
            None,
        )
        result = proxy.call_sync(
            "GetHostPaths",
            GLib.Variant("(as)", ([doc_id],)),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        if result:
            # Returns a{say}: doc_id -> host path. The ``ay`` value may unpack as
            # bytes or a list of ints depending on PyGObject, and the portal
            # NUL-terminates the path — normalise both.
            paths_dict = result.get_child_value(0).unpack()
            raw = paths_dict.get(doc_id)
            if raw is not None:
                host = bytes(raw).split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")
                return host or None
    except Exception as e:
        logger.debug(f"GetHostPaths failed for {doc_id}: {e}")
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

    # Prefer the portal mount when it exists — that is the sandbox-writable path
    # this function promises to return. ``_host_path_for_doc`` (GetHostPaths) is
    # the real host path and is only a display-oriented last resort.
    portal_path = _portal_doc_path(doc_id)
    if os.path.isdir(portal_path):
        return portal_path

    return _host_path_for_doc(doc_id)


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


def _host_path_from_xattr(path: str) -> Optional[str]:
    """Return the real host path recorded on a portal mount via the
    ``user.document-portal.host-path`` extended attribute (Flatpak only).

    This is the document portal's filesystem-level equivalent of
    ``GetHostPaths`` — more robust than the D-Bus call (no bus plumbing, no
    interface-version dependency). ``path`` must be the actual exported entry
    (the portal mount **with** basename). Returns ``None`` if the attribute is
    absent or unreadable (e.g. non-Linux, not a portal path).
    """
    if not path:
        return None
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return None
    try:
        raw = getxattr(path, "user.document-portal.host-path")
    except OSError:
        return None
    try:
        host = bytes(raw).split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")
    except Exception:  # pragma: no cover - defensive decode guard
        return None
    return host or None


_PORTAL_GRANT_ROOT_RE = re.compile(r"^/run/user/\d+/doc/[^/]+")


def _portal_grant_root(path: str) -> Optional[str]:
    """Return the portal grant's accessible root (``/run/user/<uid>/doc/<id>``)
    for any path inside it, or ``None`` if ``path`` is not a portal path.

    This is the sandbox-accessible boundary of a document-portal grant: paths at
    or below it are reachable, anything above it (``/run/user/<uid>/doc`` and up)
    is not.
    """
    if not path:
        return None
    match = _PORTAL_GRANT_ROOT_RE.match(path)
    return match.group(0) if match else None


def _portal_path_to_host(path: str) -> Optional[str]:
    """Resolve a ``/doc/`` portal path (possibly a subpath of a grant) to its
    full real host path.

    Walks up from ``path`` to the grant root reading the
    ``user.document-portal.host-path`` xattr (only guaranteed on the granted
    entry, not its descendants), collecting tail segments to re-append. The xattr
    is usually absent on the bare mount root, so when the walk reaches the grant
    root without a hit, fall back to ``GetHostPaths`` for the doc id. Returns
    ``None`` for non-portal paths or when nothing resolves.
    """
    root = _portal_grant_root(path)
    if not root:
        return None
    cur = path
    tail: List[str] = []
    while True:
        host = _host_path_from_xattr(cur)
        if host:
            return os.path.join(host, *reversed(tail)) if tail else host
        if cur == root:
            break
        tail.append(os.path.basename(cur))
        cur = os.path.dirname(cur)
    # Reached the grant root with no xattr — resolve the doc id via GetHostPaths.
    host = _host_path_for_doc(os.path.basename(root))
    if host:
        return os.path.join(host, *reversed(tail)) if tail else host
    return None


def _real_host_path(portal_path: str, doc_id: str) -> Optional[str]:
    """Resolve the real host path for a granted folder, for DISPLAY only.

    Prefers the ``user.document-portal.host-path`` xattr off the portal mount
    (walking up to the granted entry when ``portal_path`` is a subpath), then
    falls back to the Documents portal ``GetHostPaths`` D-Bus call.
    """
    return _portal_path_to_host(portal_path) or _host_path_for_doc(doc_id)


def resolve_granted_folder(gfile) -> Optional[Dict[str, str]]:
    """Grant persistent access to ``gfile`` and resolve a usable destination.

    Returns a dict with ``path`` (the sandbox-writable folder to hand to scp),
    ``display`` (a human-friendly string for status/toast text) and ``doc_id``
    (the portal document id), or ``None`` if access could not be granted.

    The picker's own path is preferred — under Flatpak it is already
    sandbox-writable (the file manager uses it directly). The portal mount
    (``/run/user/<uid>/doc/<doc_id>``) is only a fallback for the case where the
    picker path is not a usable directory. The grant is persisted via
    ``_save_doc`` so it survives across sessions and so later lookups resolve to
    the writable path instead of the real (unreachable) host path.
    """
    path = gfile.get_path()
    if not path:
        return None

    doc_id = _grant_persistent_access(gfile)
    if not doc_id:
        return None

    portal_path = path
    if not os.path.isdir(portal_path):
        constructed = _portal_doc_path(doc_id)
        if os.path.isdir(constructed):
            portal_path = constructed

    try:
        _save_doc(path, doc_id)
    except Exception as exc:  # pragma: no cover - persistence is best-effort
        logger.debug(f"Could not persist granted folder: {exc}")

    # Resolve the real host path (xattr → GetHostPaths) for a friendly display;
    # fall back to the picker path (itself a portal path) when unavailable.
    host_path = _real_host_path(path, doc_id)
    display = _pretty_path_for_display(host_path or path)

    return {"path": portal_path, "display": display, "doc_id": doc_id}


def restore_granted_folder() -> Optional[Dict[str, str]]:
    """Resolve the most recently granted folder from saved config, if usable.

    Read-side counterpart to :func:`resolve_granted_folder`, returning the same
    ``{path, display, doc_id}`` dict shape (or ``None``). Used to auto-restore a
    previously granted destination when a window reopens, without re-prompting
    the user via the portal.

    Entries are tried in reverse insertion order (most-recently-saved first), so
    the "last granted" folder wins. Each candidate is resolved to a
    sandbox-writable path via ``_lookup_document_path`` and validated with
    ``os.path.isdir``; the first one that resolves is returned.
    """
    config = _load_doc_config()
    if not config:
        return None

    for doc_id in reversed(list(config)):
        portal_path = _lookup_document_path(doc_id)
        if not portal_path or not os.path.isdir(portal_path):
            continue
        # Mirror the fresh-grant flow: derive the display from the real host
        # path (xattr → GetHostPaths), not the stored entry — ``_save_doc``
        # persisted the display from the portal path, which would show the raw
        # ``/run/user/<uid>/doc/<id>/<name>`` mount instead of the friendly path.
        host_path = _real_host_path(portal_path, doc_id)
        display = _pretty_path_for_display(host_path or portal_path)
        return {"path": portal_path, "display": display, "doc_id": doc_id}

    return None


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
    For document portal paths, resolves the real host path (e.g.
    ``/home/user/Desktop/segs``) via the ``user.document-portal.host-path`` xattr;
    only if that is unavailable does it fall back to the folder name.
    """
    try:
        gfile = Gio.File.new_for_path(path)
        parse_name = gfile.get_parse_name()

        # If it's a doc mount, show the real host path when resolvable.
        if "/doc/" in path and parse_name.startswith("/run/"):
            host = _portal_path_to_host(path)
            if host:
                return host
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
