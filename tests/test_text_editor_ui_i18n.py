"""The integrated text editor localizes presentation at the GTK boundary."""

from __future__ import annotations

import ast
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import sshpilot.text_editor as text_editor


Editor = text_editor.RemoteFileEditorWindow


def _record_dialog(monkeypatch):
    dialog = MagicMock()
    constructor = MagicMock(return_value=dialog)
    monkeypatch.setattr(
        text_editor.Adw.AlertDialog,
        "new",
        constructor,
        raising=False,
    )
    return constructor, dialog


def _response_handler(dialog):
    return next(
        call.args[1]
        for call in dialog.connect.call_args_list
        if call.args[0] == "response"
    )


@pytest.mark.parametrize(
    ("is_local", "question", "save_msgid"),
    [
        (
            True,
            "You have unsaved changes to {file_name}. "
            "Save changes before closing?",
            "Save",
        ),
        (
            False,
            "You have unsaved changes to {file_name}. "
            "Upload changes before closing?",
            "Save & Upload",
        ),
    ],
)
def test_unsaved_dialog_localizes_title_body_and_actions_before_formatting(
    monkeypatch, is_local, question, save_msgid
):
    calls = []

    def translate(msgid):
        calls.append(msgid)
        if msgid == question:
            return "localized question for {file_name}"
        return f"localized:{msgid}"

    monkeypatch.setattr(text_editor, "_", translate)
    constructor, dialog = _record_dialog(monkeypatch)
    editor = Editor.__new__(Editor)
    editor._buffer = SimpleNamespace(get_modified=lambda: True)
    editor._file_name = "notes.txt"
    editor._is_local = is_local
    editor._on_save_clicked = MagicMock()
    editor._do_close = MagicMock()

    editor._check_and_close()

    constructor.assert_called_once_with(
        "localized:Unsaved Changes",
        "localized question for notes.txt",
    )
    assert question in calls
    assert "notes.txt" not in calls
    assert dialog.add_response.call_args_list == [
        (("cancel", "localized:Cancel"),),
        (("discard", "localized:Discard Changes"),),
        (("save", f"localized:{save_msgid}"),),
    ]
    dialog.set_default_response.assert_called_once_with("save")
    dialog.set_close_response.assert_called_once_with("cancel")

    respond = _response_handler(dialog)
    respond(dialog, "cancel")
    editor._on_save_clicked.assert_not_called()
    editor._do_close.assert_not_called()
    respond(dialog, "discard")
    editor._do_close.assert_called_once_with()


def test_external_change_dialog_localizes_copy_and_keeps_overwrite_flow(monkeypatch):
    translated = []
    monkeypatch.setattr(
        text_editor,
        "_",
        lambda msgid: translated.append(msgid) or f"localized:{msgid}",
    )
    constructor, dialog = _record_dialog(monkeypatch)
    editor = Editor.__new__(Editor)
    editor._temp_file = Path("/tmp/editor-file")
    editor._buffer = SimpleNamespace(
        get_bounds=lambda: (object(), object()),
        get_text=lambda *_args: "edited text",
    )
    editor._is_local = True
    editor._pre_save_validator = None
    editor._externally_modified = True
    editor._perform_save = MagicMock()

    editor._on_save_clicked(None)

    title = "File changed on disk"
    body = (
        "This file was modified outside the editor since you opened it. "
        "Saving now overwrites those changes."
    )
    constructor.assert_called_once_with(f"localized:{title}", f"localized:{body}")
    assert translated == [title, body, "Cancel", "Overwrite"]
    assert dialog.add_response.call_args_list == [
        (("cancel", "localized:Cancel"),),
        (("overwrite", "localized:Overwrite"),),
    ]

    respond = _response_handler(dialog)
    respond(dialog, "cancel")
    editor._perform_save.assert_not_called()
    respond(dialog, "overwrite")
    editor._perform_save.assert_called_once_with("edited text")


@pytest.mark.parametrize(
    ("is_local", "msgid"),
    [
        (True, "File not found"),
        (False, "Downloaded file not found"),
    ],
)
def test_missing_file_reason_is_localized_for_local_and_remote(
    monkeypatch, tmp_path, is_local, msgid
):
    calls = []
    monkeypatch.setattr(
        text_editor,
        "_",
        lambda value: calls.append(value) or f"localized:{value}",
    )
    editor = Editor.__new__(Editor)
    editor._temp_file = tmp_path / "missing.txt"
    editor._is_local = is_local
    editor._is_loading = True
    editor._show_error = MagicMock()

    editor._load_file_content()

    editor._show_error.assert_called_once_with(f"localized:{msgid}")
    assert calls == [msgid]


def test_save_without_a_file_localizes_file_not_found(monkeypatch):
    calls = []
    monkeypatch.setattr(
        text_editor,
        "_",
        lambda value: calls.append(value) or f"localized:{value}",
    )
    editor = Editor.__new__(Editor)
    editor._temp_file = None
    editor._show_error = MagicMock()

    editor._on_save_clicked(None)

    editor._show_error.assert_called_once_with("localized:File not found")
    assert calls == ["File not found"]


def test_load_failure_translates_stable_reason_and_preserves_diagnostic(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        text_editor,
        "_",
        lambda msgid: calls.append(msgid) or "localized load: {error}",
    )
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    editor = Editor.__new__(Editor)
    editor._temp_file = directory
    editor._is_local = True
    editor._is_loading = True
    editor._show_error = MagicMock()

    editor._load_file_content()

    message = editor._show_error.call_args.args[0]
    assert message.startswith("localized load: ")
    assert str(directory) in message
    assert calls == ["Failed to load file: {error}"]
    assert editor._is_loading is False


def test_local_save_failure_translates_reason_without_translating_diagnostic(
    monkeypatch, tmp_path
):
    calls = []
    diagnostic = "opaque filesystem diagnostic"
    monkeypatch.setattr(
        text_editor,
        "_",
        lambda msgid: calls.append(msgid) or "localized save: {error}",
    )
    from sshpilot import ssh_config_utils

    monkeypatch.setattr(
        ssh_config_utils,
        "atomic_write_text",
        MagicMock(side_effect=OSError(diagnostic)),
    )
    editor = Editor.__new__(Editor)
    editor._temp_file = tmp_path / "file.txt"
    editor._daemon_file_service = None
    editor._is_local = True
    editor._show_error = MagicMock()

    editor._perform_save("edited text")

    editor._show_error.assert_called_once_with(f"localized save: {diagnostic}")
    assert calls == ["Failed to save file: {error}"]


def test_validation_failure_translates_reason_and_keeps_validator_diagnostic(
    monkeypatch
):
    calls = []
    diagnostic = "line 7: opaque parser diagnostic"
    template = "Not saved — invalid SSH config:\n\n{error}"
    monkeypatch.setattr(
        text_editor,
        "_",
        lambda msgid: calls.append(msgid) or "localized validation\n\n{error}",
    )
    editor = Editor.__new__(Editor)
    editor._temp_file = Path("/tmp/config")
    editor._buffer = SimpleNamespace(
        get_bounds=lambda: (object(), object()),
        get_text=lambda *_args: "Host web",
    )
    editor._is_local = True
    editor._pre_save_validator = lambda _text: diagnostic
    editor._show_error = MagicMock()
    editor._perform_save = MagicMock()

    editor._on_save_clicked(None)

    editor._show_error.assert_called_once_with(
        f"localized validation\n\n{diagnostic}"
    )
    assert calls == [template]
    editor._perform_save.assert_not_called()


def test_daemon_read_only_toast_is_localized_without_changing_remote_load(
    monkeypatch, tmp_path
):
    calls = []
    future = Future()
    future.set_result(
        SimpleNamespace(
            revision="revision-1",
            content="remote content",
            display_name="/remote/notes.txt",
            writable=False,
        )
    )
    service = SimpleNamespace(load=lambda: future)
    monkeypatch.setattr(
        text_editor,
        "_",
        lambda msgid: calls.append(msgid) or f"localized:{msgid}",
    )
    monkeypatch.setattr(text_editor.GLib, "idle_add", lambda *_args: 1)
    editor = Editor.__new__(Editor)
    editor._daemon_file_service = service
    editor._temp_file = tmp_path / "downloaded.txt"
    editor._remote_display_path = None
    editor._source_view = MagicMock()
    editor._save_button = MagicMock()
    editor._show_toast = MagicMock()

    editor._download_and_load()

    assert editor._temp_file.read_text(encoding="utf-8") == "remote content"
    assert editor._daemon_file_revision == "revision-1"
    assert editor._remote_display_path == "/remote/notes.txt"
    editor._source_view.set_editable.assert_called_once_with(False)
    editor._save_button.set_sensitive.assert_called_once_with(False)
    editor._show_toast.assert_called_once_with(
        "localized:Read-only file", timeout=3
    )
    assert calls == ["Read-only file"]


def test_outline_match_label_is_localized_without_translating_config_data(
    monkeypatch
):
    calls = []
    labels = []

    def make_label(*_args, **kwargs):
        labels.append(kwargs["label"])
        return MagicMock()

    monkeypatch.setattr(
        text_editor,
        "_",
        lambda msgid: calls.append(msgid) or f"localized:{msgid}",
    )
    monkeypatch.setattr(text_editor.Gtk, "Label", make_label)
    monkeypatch.setattr(text_editor.Gtk, "ListBoxRow", MagicMock)
    monkeypatch.setattr(text_editor.Gtk, "Box", MagicMock)
    editor = Editor.__new__(Editor)
    editor._outline_refresh_id = 1
    editor._outline_listbox = MagicMock()
    editor._outline_listbox.get_first_child.return_value = None
    editor._buffer = SimpleNamespace(
        get_bounds=lambda: (object(), object()),
        get_text=lambda *_args: "Match host *.internal\n",
    )

    assert editor._refresh_outline() is False

    assert labels == ["localized:Match", "host *.internal"]
    assert calls == ["Match"]


def test_text_editor_gettext_sources_are_in_potfiles():
    root = Path(__file__).resolve().parents[1]
    sources = [
        root / "src/sshpilot/text_editor.py",
        root / "src/sshpilot/remote_file_editor_service.py",
        root / "src/sshpilot/ssh_config_editor_service.py",
    ]
    potfiles = set(
        (root / "po/POTFILES").read_text(encoding="utf-8").splitlines()
    )

    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        uses_gettext = any(
            isinstance(node, ast.ImportFrom) and node.module == "gettext"
            for node in ast.walk(tree)
        )
        if uses_gettext:
            assert source.relative_to(root).as_posix() in potfiles

    assert "src/sshpilot/resources/ui/text_editor_window.ui" in potfiles
