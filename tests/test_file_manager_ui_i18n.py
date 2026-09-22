"""File manager presentation keeps translation at the last UI boundary."""

from __future__ import annotations

import ast
import pathlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot.file_manager.common import FileEntry


def _modules():
    sys.modules.setdefault("cairo", types.SimpleNamespace())
    from sshpilot import file_manager_window as window_module
    from sshpilot.file_manager import pane as pane_module
    from sshpilot.file_manager import progress_dialog as progress_module

    return window_module, pane_module, progress_module


def _entry(name: str) -> FileEntry:
    return FileEntry(name=name, is_dir=False, size=1, modified=0)


def _window(window_module, left=None, right=None):
    window = window_module.FileManagerWindow.__new__(window_module.FileManagerWindow)
    window._left_pane = left or MagicMock(name="left")
    window._right_pane = right or MagicMock(name="right")
    return window


def test_copy_cut_toasts_translate_before_format_and_keep_clipboard(monkeypatch, tmp_path):
    window_module, _, _ = _modules()
    pane = MagicMock()
    pane._current_path = str(tmp_path)
    window = _window(window_module, left=pane)
    window._update_paste_targets = MagicMock()
    monkeypatch.setattr(window_module, "_", lambda msg: {
        "Copied {name}": "{name} copied-localized",
    }.get(msg, msg))
    monkeypatch.setattr(
        window_module, "ngettext",
        lambda singular, plural, count: "{count} cut-localized" if count > 1 else singular,
    )

    window._op_copy_cut(pane, "copy", {"entries": [_entry("alpha.txt")], "directory": str(tmp_path)})
    pane.show_toast.assert_called_with("alpha.txt copied-localized")
    assert window._clipboard_entries[0].name == "alpha.txt"
    assert window._clipboard_operation == "copy"

    window._op_copy_cut(pane, "cut", {"entries": [_entry("a"), _entry("b")], "directory": str(tmp_path)})
    pane.show_toast.assert_called_with("2 cut-localized")
    assert [item.name for item in window._clipboard_entries] == ["a", "b"]
    assert window._clipboard_operation == "cut"


class _EntryWidget:
    def __init__(self):
        self.value = ""

    def set_text(self, value):
        self.value = value

    def get_text(self):
        return self.value

    def connect(self, *_args):
        pass


def _dialog_response(dialog):
    return next(call.args[1] for call in dialog.connect.call_args_list if call.args[0] == "response")


def test_create_dialog_localizes_heading_body_and_creates_folder(monkeypatch, tmp_path):
    window_module, _, _ = _modules()
    pane = MagicMock()
    pane.toolbar.path_entry.get_text.return_value = str(tmp_path)
    window = _window(window_module, left=pane)
    window._pending_highlights = {pane: None}
    window._load_local = MagicMock()
    window._embedded_parent = None
    dialog = MagicMock()
    alert_dialog = SimpleNamespace(new=MagicMock(return_value=dialog))
    monkeypatch.setattr(window_module.Adw, "AlertDialog", alert_dialog, raising=False)
    monkeypatch.setattr(window_module.Gtk, "Entry", _EntryWidget)
    monkeypatch.setattr(window_module, "_", lambda msg: f"translated:{msg}")

    window._op_mkdir(pane)

    alert_dialog.new.assert_called_once_with(
        "translated:New Folder", "translated:Enter a name for the new folder"
    )
    entry = dialog.set_extra_child.call_args.args[0]
    entry.set_text("created")
    _dialog_response(dialog)(dialog, "ok")
    assert (tmp_path / "created").is_dir()
    window._load_local.assert_called_once_with(str(tmp_path))
    assert window._pending_highlights[pane] == "created"


def test_rename_and_delete_dialogs_keep_file_operations(monkeypatch, tmp_path):
    window_module, _, _ = _modules()
    pane = MagicMock()
    pane.toolbar.path_entry.get_text.return_value = str(tmp_path)
    window = _window(window_module, left=pane)
    window._pending_highlights = {pane: None}
    window._load_local = MagicMock()
    window._embedded_parent = None
    dialogs = []

    def new_dialog(*args):
        dialog = MagicMock()
        dialogs.append((args, dialog))
        return dialog

    monkeypatch.setattr(
        window_module.Adw, "AlertDialog", SimpleNamespace(new=new_dialog), raising=False
    )
    monkeypatch.setattr(window_module.Gtk, "Entry", _EntryWidget)
    monkeypatch.setattr(window_module.GLib, "idle_add", lambda fn, *args, **kwargs: 1)
    monkeypatch.setattr(window_module, "_", lambda msg: f"translated:{msg}")
    monkeypatch.setattr(
        window_module, "ngettext",
        lambda singular, plural, count: f"translated:{singular if count == 1 else plural}",
    )
    source = tmp_path / "old.txt"
    source.write_text("payload")

    window._op_rename(pane, {"entries": [_entry("old.txt")], "directory": str(tmp_path)})
    assert dialogs[0][0] == (
        "translated:Rename Item", "translated:Enter a new name for old.txt"
    )
    rename_entry = dialogs[0][1].set_extra_child.call_args.args[0]
    rename_entry.set_text("new.txt")
    _dialog_response(dialogs[0][1])(dialogs[0][1], "ok")
    assert not source.exists()
    assert (tmp_path / "new.txt").read_text() == "payload"
    pane.show_toast.assert_any_call("translated:Renamed to new.txt")

    window._op_delete(pane, {"entries": [_entry("new.txt")], "directory": str(tmp_path)})
    assert dialogs[1][0] == ("translated:Delete Item", "translated:Delete new.txt?")
    _dialog_response(dialogs[1][1])(dialogs[1][1], "ok")
    assert not (tmp_path / "new.txt").exists()
    pane.show_toast.assert_any_call("translated:Deleted 1 item")


def test_pane_transfer_toasts_translate_and_preserve_payloads(monkeypatch, tmp_path):
    window_module, pane_module, _ = _modules()
    local = pane_module.FilePane.__new__(pane_module.FilePane)
    remote = pane_module.FilePane.__new__(pane_module.FilePane)
    window = _window(window_module, left=local, right=remote)
    local._current_path = str(tmp_path)
    local.toolbar = MagicMock()
    local.get_selected_entries = lambda: [_entry("a.txt"), _entry("b.txt")]
    local._get_file_manager_window = lambda: window
    local.emit = MagicMock()
    local.show_toast = MagicMock()
    local._is_remote = False
    remote._current_path = "/remote"
    remote.toolbar = MagicMock()
    remote.toolbar.path_entry.get_text.return_value = "/remote"
    remote.get_selected_entries = lambda: [_entry("a.txt"), _entry("b.txt")]
    remote._get_file_manager_window = lambda: window
    remote.emit = MagicMock()
    remote.show_toast = MagicMock()
    remote._is_remote = True
    monkeypatch.setattr(pane_module, "_", lambda msg: f"translated:{msg}")
    monkeypatch.setattr(
        pane_module, "ngettext",
        lambda singular, plural, count: "{count} localized-items…",
    )

    local._on_upload_clicked(None)
    signal, action, payload = local.emit.call_args.args
    assert signal == "request-operation"
    assert action == "upload"
    assert payload["paths"] == [tmp_path / "a.txt", tmp_path / "b.txt"]
    assert payload["destination"] == "/remote"
    local.show_toast.assert_called_once_with("2 localized-items…")

    remote._on_download_clicked(None)
    signal, action, payload = remote.emit.call_args.args
    assert signal == "request-operation"
    assert action == "download"
    assert payload["directory"] == "/remote"
    assert payload["destination"] == tmp_path
    remote.show_toast.assert_called_once_with("2 localized-items…")


def test_progress_counts_use_ngettext_before_format(monkeypatch):
    _, _, progress_module = _modules()
    dialog = progress_module.SFTPProgressDialog.__new__(progress_module.SFTPProgressDialog)
    dialog.total_files = 0
    dialog.files_completed = 0
    dialog.counter_label = MagicMock()
    dialog.file_label = MagicMock()
    dialog.current_file = ""
    selected = []

    def translate_count(singular, plural, count):
        selected.append((singular, plural, count))
        return "{total} total; {done} done"

    monkeypatch.setattr(progress_module, "ngettext", translate_count)
    dialog.set_operation_details(2)
    dialog._increment_file_count_ui()
    assert selected[0][2] == 2
    assert selected[1][2] == 2
    dialog.counter_label.set_text.assert_called_with("2 total; 1 done")


def test_completion_summary_uses_real_singular_and_plural(monkeypatch):
    _, _, progress_module = _modules()
    selected = []

    def translate_count(singular, plural, count):
        selected.append((singular, plural, count))
        return f"translated:{singular if count == 1 else plural}"

    monkeypatch.setattr(progress_module, "ngettext", translate_count)

    def dialog_for(count, size):
        dialog = progress_module.SFTPProgressDialog.__new__(progress_module.SFTPProgressDialog)
        dialog._completion_shown = False
        dialog._stop_render_timer = MagicMock()
        dialog._set_dialog_heading = MagicMock()
        dialog.files_completed = count
        dialog.total_files = count
        dialog._transferred_bytes = size
        dialog.status_label = MagicMock()
        dialog.file_label = MagicMock()
        dialog.counter_label = MagicMock()
        dialog.progress_bar = MagicMock()
        dialog.speed_label = MagicMock()
        dialog.time_label = MagicMock()
        dialog.action_button = MagicMock()
        dialog.locate_button = MagicMock()
        dialog.operation_type = "upload"
        dialog.current_file = ""
        return dialog

    singular = dialog_for(1, 0)
    singular._show_completion_ui(True, None)
    singular.file_label.set_text.assert_called_with("translated:Successfully transferred 1 file")

    plural = dialog_for(2, 2048)
    plural._show_completion_ui(True, None)
    plural.status_label.set_text.assert_called_with(
        "translated:Successfully transferred 2 files (2.0 KB)"
    )
    assert ("Successfully transferred {count} file", "Successfully transferred {count} files", 1) in selected
    assert ("Successfully transferred {count} file ({size})", "Successfully transferred {count} files ({size})", 2) in selected


def test_backend_diagnostic_stays_opaque_on_directory_error(monkeypatch):
    window_module, _, _ = _modules()
    window = _window(window_module)
    manager = MagicMock()
    window._manager = manager
    window._is_disposed = False
    window._pending_paths = {window._left_pane: None, window._right_pane: "/private"}
    window._loading_toast_timeouts = {window._left_pane: None, window._right_pane: None}
    translated = []

    def translate(msg):
        translated.append(msg)
        return "reason: {error}"

    monkeypatch.setattr(window_module, "_", translate)

    window._on_operation_error(manager, "opaque server diagnostic")

    window._right_pane.show_load_error.assert_called_once_with(
        "/private", "reason: opaque server diagnostic"
    )
    assert translated == ["File operation failed: {error}"]


def test_daemon_transfer_progress_translates_before_format_and_keeps_bytes(monkeypatch):
    sys.modules.setdefault("cairo", types.SimpleNamespace())
    from sshpilot import daemon_sftp_backend as backend_module

    manager = SimpleNamespace(
        emit=MagicMock(),
        _format_size=backend_module.DaemonSftpManager._format_size,
    )
    translated = []

    def translate(message):
        translated.append(message)
        return {
            "Transferred {done} of {total}": "total={total}; done={done}",
            "Transferred {size}": "size={size}",
        }.get(message, message)

    monkeypatch.setattr(backend_module, "_", translate)

    backend_module.DaemonSftpManager._emit_transfer_progress(
        manager, 0, SimpleNamespace(bytes_completed=2048), 4096
    )
    assert manager.emit.call_args_list[0].args == ("progress-bytes", 2048, 4096)
    assert manager.emit.call_args_list[1].args == (
        "progress",
        0.5,
        "total=4.0 KB; done=2.0 KB",
    )

    manager.emit.reset_mock()
    backend_module.DaemonSftpManager._emit_transfer_progress(
        manager, 512, SimpleNamespace(bytes_completed=512), 0
    )
    assert manager.emit.call_args_list[0].args == ("progress-bytes", 1024, 0)
    assert manager.emit.call_args_list[1].args == ("progress", 0.0, "size=1.0 KB")
    assert translated == ["Transferred {done} of {total}", "Transferred {size}"]


def test_daemon_upload_download_starts_are_localized_and_requests_unchanged(
    monkeypatch, tmp_path
):
    sys.modules.setdefault("cairo", types.SimpleNamespace())
    from sshpilot import daemon_sftp_backend as backend_module
    from sshpilot.api.models.common import ConnectionId, SftpServiceId
    from sshpilot.api.models.transfers import TransferDirection

    transfers = MagicMock()
    manager = SimpleNamespace(
        _connection_id=ConnectionId("connection-1"),
        _transfers=transfers,
        _expand=lambda path: path,
        _require_ready_service_id=lambda: SftpServiceId("sftp-1"),
        _next_operation_id=lambda kind: kind,
        _cancellable=lambda future, _operation_id, _get_transfer_id: future,
        emit=MagicMock(),
    )
    monkeypatch.setattr(
        backend_module,
        "_",
        lambda message: {
            "Starting upload…": "upload-localized",
            "Starting download…": "download-localized",
        }.get(message, message),
    )

    source = tmp_path / "source.txt"
    source.write_bytes(b"payload")
    upload_future = backend_module.DaemonSftpManager.upload(
        manager, source, "/remote/source.txt"
    )
    assert not upload_future.done()
    manager.emit.assert_called_once_with("progress", 0.0, "upload-localized")
    upload_request = transfers.start_transfer.call_args.args[0]
    assert upload_request.direction is TransferDirection.UPLOAD
    assert upload_request.local_path == str(source)
    assert upload_request.remote_path == "/remote/source.txt"

    manager.emit.reset_mock()
    transfers.reset_mock()
    destination = tmp_path / "downloads" / "destination.txt"
    download_future = backend_module.DaemonSftpManager.download(
        manager, "/remote/destination.txt", destination
    )
    assert not download_future.done()
    manager.emit.assert_called_once_with("progress", 0.0, "download-localized")
    download_request = transfers.start_transfer.call_args.args[0]
    assert download_request.direction is TransferDirection.DOWNLOAD
    assert download_request.remote_path == "/remote/destination.txt"
    assert download_request.local_path == str(destination)


def test_daemon_transfer_failure_diagnostic_is_not_translated(monkeypatch):
    sys.modules.setdefault("cairo", types.SimpleNamespace())
    from sshpilot import daemon_sftp_backend as backend_module
    from sshpilot.api.models.transfers import TransferState

    translated = []
    monkeypatch.setattr(backend_module, "_", lambda message: translated.append(message))
    monkeypatch.setattr(
        backend_module,
        "format_sftp_failure",
        lambda _failure: "opaque SFTP diagnostic",
    )
    manager = SimpleNamespace(_safe_set=backend_module.DaemonSftpManager._safe_set)
    future = backend_module.Future()

    backend_module.DaemonSftpManager._finish_transfer(
        manager,
        future,
        SimpleNamespace(
            state=TransferState.FAILED,
            failure=object(),
            bytes_completed=0,
        ),
    )

    assert str(future.exception()) == "opaque SFTP diagnostic"
    assert translated == []


def test_reconnect_error_toast_translates_reason(monkeypatch):
    window_module, _, _ = _modules()
    window = _window(window_module)
    pane = window._right_pane
    pane._is_remote = True
    pane._current_path = "/private"
    window._pending_paths = {pane: None}
    window._pending_highlights = {pane: None}
    window._refreshing_panes = set()
    window._manager = MagicMock()
    window._manager.listdir.side_effect = RuntimeError("socket is closed")
    monkeypatch.setattr(window_module, "_", lambda msg: f"translated:{msg}")

    window._force_refresh_pane(pane)

    pane.show_toast.assert_called_once_with(
        "translated:Connection closed. Please refresh manually.", timeout=3
    )
    assert pane not in window._refreshing_panes


def test_file_manager_gettext_sources_are_in_potfiles():
    root = pathlib.Path(__file__).resolve().parents[1]
    paths = [
        root / "src/sshpilot/file_manager_window.py",
        root / "src/sshpilot/file_manager_integration.py",
        root / "src/sshpilot/daemon_sftp_backend.py",
        root / "src/sshpilot/window_file_manager.py",
        root / "src/sshpilot/window_tabs.py",
        *(root / "src/sshpilot/file_manager").glob("*.py"),
    ]
    potfiles = set((root / "po/POTFILES").read_text(encoding="utf-8").splitlines())
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses_gettext = any(
            isinstance(node, ast.ImportFrom) and node.module == "gettext"
            for node in ast.walk(tree)
        )
        if uses_gettext:
            assert path.relative_to(root).as_posix() in potfiles
