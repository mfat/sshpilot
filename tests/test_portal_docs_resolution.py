"""Unit tests for Document-portal path resolution.

These guard the Flatpak SCP-destination bug: a fresh grant must resolve to the
sandbox-writable portal path (the picker path / portal mount), never the real
host path returned by GetHostPaths, which is unreachable inside the sandbox.
"""

import os

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
    # GetHostPaths always returns the real host path (the wrong target for scp).
    monkeypatch.setattr(portal_docs, "_host_path_for_doc", lambda doc_id: "/home/user/Downloads")
    monkeypatch.setattr(portal_docs, "_pretty_path_for_display", lambda p: p)
    saved = {}
    monkeypatch.setattr(portal_docs, "_save_doc", lambda path, doc_id: saved.setdefault(doc_id, path))
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
