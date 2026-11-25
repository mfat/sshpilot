"""Issue #1428: Manage Files on an unsaved (ad-hoc) target.

A target typed in the search box is registered in the daemon under a
transient id while its nickname stays what the user typed. The file manager
must open SFTP under the transient id, or the daemon reports the connection
as not found.
"""
import sys
import types

from sshpilot.api.connection_identity import file_manager_connection_id
from tests.test_manage_files_ui import prepare_file_manager_integration


def _adhoc_connection():
    return types.SimpleNamespace(
        nickname="root@203.0.113.7",
        hostname="203.0.113.7",
        username="root",
        port=22,
        transient_connection_id="adhoc-0123456789ab",
    )


def test_helper_prefers_transient_id():
    assert file_manager_connection_id(_adhoc_connection()) == "adhoc-0123456789ab"


def test_helper_keeps_saved_nickname_and_tolerates_no_identity():
    assert file_manager_connection_id(types.SimpleNamespace(nickname="web")) == "web"
    assert file_manager_connection_id(types.SimpleNamespace()) == ""


def _stub_file_manager_window(monkeypatch, created):
    class FakeFileManagerWindow:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def realize(self):
            pass

        def detach_for_embedding(self, _parent):
            return object()

    module = types.ModuleType("sshpilot.file_manager_window")
    module.FileManagerWindow = FakeFileManagerWindow
    monkeypatch.setitem(sys.modules, "sshpilot.file_manager_window", module)


def test_embedded_tab_opens_under_transient_id(monkeypatch):
    integration = prepare_file_manager_integration(monkeypatch)
    integration.Gtk.Application = types.SimpleNamespace(
        get_default=lambda: types.SimpleNamespace(remove_window=lambda _w: None)
    )
    monkeypatch.setattr(integration, "FileManagerTabEmbed", lambda c, w: (c, w))
    created = {}
    _stub_file_manager_window(monkeypatch, created)

    integration.create_internal_file_manager_tab(
        user="root", host="203.0.113.7", nickname="root@203.0.113.7",
        parent_window=None, connection=_adhoc_connection(),
    )

    assert created["connection_id"] == "adhoc-0123456789ab"
