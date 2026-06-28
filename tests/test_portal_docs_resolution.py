"""Unit tests for Document-portal path resolution.

These guard the Flatpak SCP-destination bug: a fresh grant must resolve to the
sandbox-writable portal path (the picker path / portal mount), never the real
host path returned by GetHostPaths, which is unreachable inside the sandbox.
"""

import os
import types

import pytest

from sshpilot.file_manager import portal_docs


class _FakeGFile:
    def __init__(self, path):
        self._path = path

    def get_path(self):
        return self._path


@pytest.fixture
def patched_portal(monkeypatch):
    """Stub the D-Bus-backed helpers so resolution logic is testable offline."""
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    monkeypatch.setattr(portal_docs, "_grant_persistent_access", lambda gfile: "DOCID")
    # The host-path resolver (xattr → GetHostPaths) is tested separately; here it
    # always yields the real host path (the wrong target for scp — display only).
    monkeypatch.setattr(portal_docs, "_real_host_path", lambda portal_path, doc_id: "/home/user/Downloads")
    monkeypatch.setattr(portal_docs, "_pretty_path_for_display", lambda p: p)
    # Portal paths in tests don't exist on disk → make them count as writable.
    monkeypatch.setattr(os, "access", lambda p, mode: True)
    saved = {}

    def _save(path, doc_id):
        saved.pop(doc_id, None)
        saved[doc_id] = path

    monkeypatch.setattr(portal_docs, "_save_doc", _save)
    return saved


def test_resolve_prefers_picker_path_not_host_path(patched_portal, monkeypatch):
    """A fresh grant must use the picker's sandbox-writable path, not the host path."""
    picker_path = f"/run/user/1000/doc/DOCID/Downloads"
    monkeypatch.setattr(os.path, "isdir", lambda p: p == picker_path)

    result = portal_docs.resolve_granted_folder(_FakeGFile(picker_path))

    assert result is not None
    assert result["path"] == picker_path
    assert result["path"] != "/home/user/Downloads"
    # Display still reflects the human-friendly host path.
    assert result["display"] == "/home/user/Downloads"
    # The grant is persisted for cross-session parity with the file manager.
    assert patched_portal == {"DOCID": picker_path}


def test_resolve_falls_back_to_portal_mount(patched_portal, monkeypatch):
    """If the picker path is not a usable dir, fall back to the portal mount."""
    mount = f"/run/user/{os.getuid()}/doc/DOCID"
    monkeypatch.setattr(os.path, "isdir", lambda p: p == mount)

    result = portal_docs.resolve_granted_folder(_FakeGFile("/some/unmounted/path"))

    assert result is not None
    assert result["path"] == mount


def test_resolve_returns_none_when_grant_fails(monkeypatch):
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    monkeypatch.setattr(portal_docs, "_grant_persistent_access", lambda gfile: None)

    assert portal_docs.resolve_granted_folder(_FakeGFile("/x")) is None


def test_resolve_returns_none_without_path(monkeypatch):
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    assert portal_docs.resolve_granted_folder(_FakeGFile(None)) is None


def test_lookup_document_path_prefers_portal_mount(monkeypatch):
    """``_lookup_document_path`` must honor its docstring: return the writable
    portal mount when present, not the host path from GetHostPaths."""
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    monkeypatch.setattr(portal_docs, "_lookup_path_from_config", lambda doc_id: None)
    host_called = {"hit": False}

    def _host(doc_id):
        host_called["hit"] = True
        return "/home/user/Downloads"

    monkeypatch.setattr(portal_docs, "_host_path_for_doc", _host)
    mount = f"/run/user/{os.getuid()}/doc/DOCID"
    monkeypatch.setattr(os.path, "isdir", lambda p: p == mount)

    assert portal_docs._lookup_document_path("DOCID") == mount
    assert host_called["hit"] is False


def test_lookup_document_path_falls_back_to_host(monkeypatch):
    """When no portal mount exists, GetHostPaths is the last resort (display)."""
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    monkeypatch.setattr(portal_docs, "_lookup_path_from_config", lambda doc_id: None)
    monkeypatch.setattr(portal_docs, "_host_path_for_doc", lambda doc_id: "/home/user/Downloads")
    monkeypatch.setattr(os.path, "isdir", lambda p: False)

    assert portal_docs._lookup_document_path("DOCID") == "/home/user/Downloads"


def test_restore_returns_most_recent_grant(monkeypatch):
    """``restore_granted_folder`` returns the last-saved valid grant, with the
    display derived from the real host path (not the raw portal mount)."""
    config = {
        "OLD": {"path": "/run/user/1000/doc/OLD/Old"},
        "NEW": {"path": "/run/user/1000/doc/NEW/New"},
    }
    monkeypatch.setattr(portal_docs, "_load_doc_config", lambda: config)
    monkeypatch.setattr(
        portal_docs, "_lookup_document_path", lambda doc_id: f"/run/user/1000/doc/{doc_id}/New"
    )
    monkeypatch.setattr(portal_docs, "_is_valid_destination", lambda p: True)
    # Host-path resolver (xattr → GetHostPaths) is tested separately.
    monkeypatch.setattr(portal_docs, "_real_host_path", lambda portal_path, doc_id: f"/home/user/{doc_id}")
    monkeypatch.setattr(portal_docs, "_pretty_path_for_display", lambda p: p)

    result = portal_docs.restore_granted_folder()

    assert result == {
        "path": "/run/user/1000/doc/NEW/New",
        "display": "/home/user/NEW",
        "doc_id": "NEW",
    }


def test_restore_skips_invalid_and_falls_through(monkeypatch):
    """An entry that isn't a valid destination is skipped for an older valid one."""
    config = {
        "OLD": {"path": "/run/user/1000/doc/OLD/Old"},
        "NEW": {"path": "/run/user/1000/doc/NEW/New"},
    }
    monkeypatch.setattr(portal_docs, "_load_doc_config", lambda: config)
    monkeypatch.setattr(
        portal_docs, "_lookup_document_path", lambda doc_id: f"/run/user/1000/doc/{doc_id}"
    )
    # Newest ("NEW") is no longer usable; older ("OLD") still is.
    monkeypatch.setattr(portal_docs, "_is_valid_destination", lambda p: p.endswith("/OLD"))
    monkeypatch.setattr(portal_docs, "_real_host_path", lambda portal_path, doc_id: f"/home/user/{doc_id}")
    monkeypatch.setattr(portal_docs, "_pretty_path_for_display", lambda p: p)

    result = portal_docs.restore_granted_folder()

    assert result is not None
    assert result["doc_id"] == "OLD"


def test_restore_skips_root_grant_for_real_folder(monkeypatch):
    """A stale ``/`` grant (last inserted) is skipped for a real portal mount."""
    config = {
        "HOME": {"path": "/run/user/1000/doc/c8bb62d1/home"},
        "ROOT": {"path": "/"},
    }
    monkeypatch.setattr(portal_docs, "_load_doc_config", lambda: config)
    monkeypatch.setattr(portal_docs, "_lookup_document_path", lambda doc_id: config[doc_id]["path"])
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)  # both "/" and the mount exist
    monkeypatch.setattr(os, "access", lambda p, mode: True)
    monkeypatch.setattr(portal_docs, "_real_host_path", lambda portal_path, doc_id: "/home")
    monkeypatch.setattr(portal_docs, "_pretty_path_for_display", lambda p: p)

    result = portal_docs.restore_granted_folder()

    # "/" is not a portal mount → rejected; the home grant wins.
    assert result is not None
    assert result["doc_id"] == "HOME"
    assert result["path"] == "/run/user/1000/doc/c8bb62d1/home"


def test_restore_display_falls_back_to_portal_path_without_host(monkeypatch):
    """If the host path can't be resolved, display falls back to the portal path."""
    config = {"DOCID": {"path": "/run/user/1000/doc/DOCID/Downloads"}}
    monkeypatch.setattr(portal_docs, "_load_doc_config", lambda: config)
    monkeypatch.setattr(portal_docs, "_lookup_document_path", lambda doc_id: "/run/user/1000/doc/DOCID")
    monkeypatch.setattr(portal_docs, "_is_valid_destination", lambda p: True)
    monkeypatch.setattr(portal_docs, "_real_host_path", lambda portal_path, doc_id: None)
    monkeypatch.setattr(portal_docs, "_pretty_path_for_display", lambda p: f"PRETTY:{p}")

    result = portal_docs.restore_granted_folder()

    assert result["display"] == "PRETTY:/run/user/1000/doc/DOCID"


def test_is_valid_destination(monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: p != "/nonexistent")
    monkeypatch.setattr(os, "access", lambda p, mode: True)

    # Under Flatpak: only portal mounts are valid; "/" and host paths are not.
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    assert portal_docs._is_valid_destination("/run/user/1000/doc/ID/Downloads") is True
    assert portal_docs._is_valid_destination("/") is False
    assert portal_docs._is_valid_destination("/home/mahdi/Downloads") is False
    assert portal_docs._is_valid_destination("/nonexistent") is False
    assert portal_docs._is_valid_destination("") is False

    # Non-Flatpak: any existing writable dir is fine.
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: False)
    assert portal_docs._is_valid_destination("/home/mahdi/Downloads") is True

    # Read-only dir is rejected even when it is a portal mount.
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    monkeypatch.setattr(os, "access", lambda p, mode: False)
    assert portal_docs._is_valid_destination("/run/user/1000/doc/ID/Downloads") is False


def test_grant_reuses_filechooser_portal_doc_id(monkeypatch):
    """When the picker returns a portal path, reuse its real doc id (no D-Bus,
    no md5 fallback)."""
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)

    def _boom(*a, **k):
        raise AssertionError("D-Bus AddFull must not run for an exported portal path")

    monkeypatch.setattr(portal_docs.Gio, "bus_get_sync", _boom)

    doc_id = portal_docs._grant_persistent_access(_FakeGFile("/run/user/1000/doc/c0b2576c/mahdi"))
    assert doc_id == "c0b2576c"


def test_grant_non_flatpak_uses_stable_md5(monkeypatch):
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: False)
    import hashlib

    expected = hashlib.md5(b"/home/mahdi/Downloads").hexdigest()[:16]
    assert portal_docs._grant_persistent_access(_FakeGFile("/home/mahdi/Downloads")) == expected


def test_is_usable_grant_allows_readonly(monkeypatch):
    """Browsing grants don't require write access; only portal mounts qualify."""
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    # No W_OK required (read-only is fine for browsing/restore).
    monkeypatch.setattr(os, "access", lambda p, mode: False)
    assert portal_docs._is_usable_grant("/run/user/1000/doc/ID/segs") is True
    assert portal_docs._is_usable_grant("/") is False
    assert portal_docs._is_usable_grant("/home/mahdi") is False


def test_load_first_doc_path_returns_most_recent_usable(monkeypatch):
    """The file manager restores the last granted folder, skipping a stale ``/``."""
    config = {
        "OLD": {"path": "/run/user/1000/doc/OLD/old"},
        "HOME": {"path": "/run/user/1000/doc/HOME/home"},
        "ROOT": {"path": "/"},
    }
    monkeypatch.setattr(portal_docs, "_load_doc_config", lambda: config)
    monkeypatch.setattr(portal_docs, "_lookup_document_path", lambda doc_id: config[doc_id]["path"])
    monkeypatch.setattr(portal_docs, "is_flatpak", lambda: True)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)

    result = portal_docs._load_first_doc_path()

    assert result is not None
    portal_path, doc_id, entry = result
    # "/" (last inserted) is skipped; HOME is the most-recent usable grant.
    assert doc_id == "HOME"
    assert portal_path == "/run/user/1000/doc/HOME/home"


def test_load_grant_for_host_matches_home(monkeypatch):
    """Startup can prefer the grant whose real host path is the user's home."""
    config = {
        "DESK": {"path": "/run/user/1000/doc/DESK/Desktop"},
        "HOME": {"path": "/run/user/1000/doc/HOME/mahdi"},
    }
    host_by_path = {
        "/run/user/1000/doc/DESK/Desktop": "/home/mahdi/Desktop",
        "/run/user/1000/doc/HOME/mahdi": "/home/mahdi",
    }
    monkeypatch.setattr(portal_docs, "_load_doc_config", lambda: config)
    monkeypatch.setattr(portal_docs, "_lookup_document_path", lambda doc_id: config[doc_id]["path"])
    monkeypatch.setattr(portal_docs, "_is_usable_grant", lambda p: True)
    monkeypatch.setattr(portal_docs, "_portal_path_to_host", lambda p: host_by_path.get(p))

    result = portal_docs._load_grant_for_host("/home/mahdi")
    assert result is not None
    assert result[1] == "HOME"

    # No grant maps to the requested host → None.
    assert portal_docs._load_grant_for_host("/home/other") is None
    assert portal_docs._load_grant_for_host("") is None


def test_resolve_rejects_invalid_destination(patched_portal, monkeypatch):
    """Picking ``/`` (or any non-portal folder) yields no grant."""
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    # "/" is a dir but not a portal mount → invalid; nothing persisted.
    result = portal_docs.resolve_granted_folder(_FakeGFile("/"))
    assert result is None
    assert patched_portal == {}


def test_save_doc_moves_regrant_to_most_recent(monkeypatch, tmp_path):
    docs_json = tmp_path / "granted-folders.json"
    monkeypatch.setattr(portal_docs, "DOCS_JSON", str(docs_json))
    monkeypatch.setattr(portal_docs, "_ensure_cfg_dir", lambda: None)

    class _Gio:
        @staticmethod
        def new_for_path(p):
            return types.SimpleNamespace(get_parse_name=lambda: p)

    monkeypatch.setattr(portal_docs.Gio, "File", _Gio)

    portal_docs._save_doc("/a", "A")
    portal_docs._save_doc("/b", "B")
    portal_docs._save_doc("/a", "A")  # re-grant A → should move to the end

    import json

    data = json.loads(docs_json.read_text())
    assert list(data) == ["B", "A"]


def test_host_path_from_xattr_reads_and_normalises(monkeypatch):
    """``_host_path_from_xattr`` decodes the attr and strips the trailing NUL."""
    calls = {}

    def _getxattr(path, name):
        calls["path"] = path
        calls["name"] = name
        return b"/home/mahdi/Desktop/segs\x00"

    monkeypatch.setattr(os, "getxattr", _getxattr, raising=False)

    assert portal_docs._host_path_from_xattr("/run/user/1000/doc/DOCID/segs") == "/home/mahdi/Desktop/segs"
    assert calls["name"] == "user.document-portal.host-path"


def test_host_path_from_xattr_absent_returns_none(monkeypatch):
    def _getxattr(path, name):
        raise OSError("no such attribute")

    monkeypatch.setattr(os, "getxattr", _getxattr, raising=False)

    assert portal_docs._host_path_from_xattr("/run/user/1000/doc/DOCID/segs") is None
    assert portal_docs._host_path_from_xattr("") is None


def test_portal_path_to_host_exact_entry(monkeypatch):
    """When the path itself carries the xattr, return it directly."""
    monkeypatch.setattr(
        portal_docs,
        "_host_path_from_xattr",
        lambda p: "/home/mahdi/Desktop/segs" if p == "/run/user/1000/doc/DOCID/segs" else None,
    )
    assert portal_docs._portal_path_to_host("/run/user/1000/doc/DOCID/segs") == "/home/mahdi/Desktop/segs"


def test_portal_path_to_host_walks_up_subpath(monkeypatch):
    """A subpath whose ancestor (the grant root) carries the xattr resolves to
    the full reconstructed host path."""
    grant_root = "/run/user/1000/doc/DOCID/segs"
    monkeypatch.setattr(
        portal_docs,
        "_host_path_from_xattr",
        lambda p: "/home/mahdi/Desktop/segs" if p == grant_root else None,
    )
    result = portal_docs._portal_path_to_host("/run/user/1000/doc/DOCID/segs/sub/deep")
    assert result == "/home/mahdi/Desktop/segs/sub/deep"


def test_portal_path_to_host_non_portal_returns_none(monkeypatch):
    monkeypatch.setattr(portal_docs, "_host_path_from_xattr", lambda p: None)
    monkeypatch.setattr(portal_docs, "_host_path_for_doc", lambda doc_id: None)
    assert portal_docs._portal_path_to_host("/home/mahdi/Desktop/segs") is None
    assert portal_docs._portal_path_to_host("") is None
    # A /doc/ path with neither xattr nor GetHostPaths resolves to None.
    assert portal_docs._portal_path_to_host("/run/user/1000/doc/DOCID/segs") is None


def test_portal_grant_root(monkeypatch):
    assert portal_docs._portal_grant_root("/run/user/1000/doc/DOCID/home/mahdi") == "/run/user/1000/doc/DOCID"
    assert portal_docs._portal_grant_root("/run/user/1000/doc/DOCID") == "/run/user/1000/doc/DOCID"
    assert portal_docs._portal_grant_root("/home/mahdi/Desktop") is None
    assert portal_docs._portal_grant_root("/run/user/1000/doc") is None
    assert portal_docs._portal_grant_root("") is None


def test_portal_path_to_host_mount_root_via_gethostpaths(monkeypatch):
    """The bare mount root has no xattr; resolve it via GetHostPaths(doc_id).

    Models a ``/`` grant: doc id maps to ``/``.
    """
    monkeypatch.setattr(portal_docs, "_host_path_from_xattr", lambda p: None)
    monkeypatch.setattr(portal_docs, "_host_path_for_doc", lambda doc_id: "/")

    assert portal_docs._portal_path_to_host("/run/user/1000/doc/ROOTID") == "/"


def test_portal_path_to_host_subpath_under_rootless_grant(monkeypatch):
    """Sub-paths of a ``/`` grant reconstruct against the GetHostPaths root."""
    monkeypatch.setattr(portal_docs, "_host_path_from_xattr", lambda p: None)
    monkeypatch.setattr(portal_docs, "_host_path_for_doc", lambda doc_id: "/")

    result = portal_docs._portal_path_to_host("/run/user/1000/doc/ROOTID/home/mahdi")
    assert result == "/home/mahdi"


def test_portal_path_to_host_entry_xattr_wins_over_gethostpaths(monkeypatch):
    """A normal folder grant resolves via the entry xattr before reaching the
    root, so GetHostPaths (which would double the basename) is not consulted."""
    monkeypatch.setattr(
        portal_docs,
        "_host_path_from_xattr",
        lambda p: "/home/mahdi/Desktop/segs" if p == "/run/user/1000/doc/ID/segs" else None,
    )

    def _should_not_run(doc_id):
        raise AssertionError("GetHostPaths must not run when the entry xattr resolves")

    monkeypatch.setattr(portal_docs, "_host_path_for_doc", _should_not_run)

    assert portal_docs._portal_path_to_host("/run/user/1000/doc/ID/segs/sub") == "/home/mahdi/Desktop/segs/sub"


def test_restore_returns_none_when_empty_or_unresolvable(monkeypatch):
    monkeypatch.setattr(portal_docs, "_load_doc_config", dict)
    assert portal_docs.restore_granted_folder() is None

    monkeypatch.setattr(portal_docs, "_load_doc_config", lambda: {"D": {"display": "x"}})
    monkeypatch.setattr(portal_docs, "_lookup_document_path", lambda doc_id: None)
    assert portal_docs.restore_granted_folder() is None
