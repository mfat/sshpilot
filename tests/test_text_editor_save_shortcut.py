"""Primary+S and the Save button write the same file through the same path."""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import sshpilot.text_editor as text_editor


Editor = text_editor.RemoteFileEditorWindow

CONTROL, SHIFT, META = 1 << 2, 1 << 0, 1 << 28
KEY_S = 0x073


@pytest.fixture
def gdk(monkeypatch):
    fake = SimpleNamespace(
        ModifierType=SimpleNamespace(CONTROL_MASK=CONTROL, SHIFT_MASK=SHIFT, META_MASK=META),
        KEY_s=KEY_S,
        KEY_plus=0x02B,
        KEY_equal=0x03D,
        KEY_minus=0x02D,
        KEY_0=0x030,
        KEY_f=0x066,
        KEY_z=0x07A,
        KEY_y=0x079,
    )
    monkeypatch.setattr(text_editor, "Gdk", fake)
    return fake


def _local_editor(path, text):
    editor = Editor.__new__(Editor)
    editor._temp_file = path
    editor._buffer = SimpleNamespace(
        get_bounds=lambda: (object(), object()),
        get_text=lambda *_args: text,
        set_modified=lambda _value: None,
    )
    editor._is_local = True
    editor._pre_save_validator = None
    editor._externally_modified = False
    editor._daemon_file_service = None
    editor._root_mode = False
    editor._gtksource_enabled = False
    editor._on_saved = None
    editor._file_manager_window = None
    editor._save_button = MagicMock()
    editor._save_button.get_sensitive.return_value = True
    editor._update_title = MagicMock()
    editor._show_toast = MagicMock()
    editor._show_error = MagicMock()
    return editor


def _record_writes(monkeypatch):
    from sshpilot import ssh_config_utils

    writes = []
    real = ssh_config_utils.atomic_write_text
    monkeypatch.setattr(
        ssh_config_utils,
        "atomic_write_text",
        lambda path, text: writes.append((path, text)) or real(path, text),
    )
    return writes


def test_save_button_is_wired_to_the_shortcut_handler():
    # The button's "clicked" handler must be the exact method Primary+S calls,
    # so there is only one save path to keep correct.
    tree = ast.parse(textwrap.dedent(inspect.getsource(Editor)))
    connects = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_save_button"
    ]
    assert [ast.unparse(call) for call in connects] == [
        "self._save_button.connect('clicked', self._on_save_clicked)"
    ]


@pytest.mark.parametrize(("macos", "modifier"), [(False, CONTROL), (True, META)])
def test_shortcut_and_button_save_to_identical_path(monkeypatch, tmp_path, gdk, macos, modifier):
    monkeypatch.setattr(text_editor, "is_macos", lambda: macos)
    writes = _record_writes(monkeypatch)
    path = tmp_path / "notes.txt"
    path.write_text("original")

    by_button = _local_editor(path, "via button")
    by_button._on_save_clicked(by_button._save_button)

    by_shortcut = _local_editor(path, "via shortcut")
    handled = by_shortcut._on_key_pressed(None, gdk.KEY_s, 0, modifier)

    assert handled is True
    assert writes == [(str(path), "via button"), (str(path), "via shortcut")]
    assert path.read_text() == "via shortcut"
    by_button._show_error.assert_not_called()
    by_shortcut._show_error.assert_not_called()


@pytest.mark.parametrize("root_mode", [False, True])
def test_shortcut_and_button_use_same_daemon_save(monkeypatch, tmp_path, gdk, root_mode):
    monkeypatch.setattr(text_editor, "is_macos", lambda: False)
    calls = []
    for trigger in ("button", "shortcut"):
        editor = _local_editor(tmp_path / "config", "Host web")
        editor._daemon_file_service = object()
        editor._root_mode = root_mode
        editor._save_daemon_file = lambda text, *, privileged: calls.append((text, privileged))
        if trigger == "button":
            editor._on_save_clicked(editor._save_button)
        else:
            assert editor._on_key_pressed(None, gdk.KEY_s, 0, CONTROL) is True

    assert calls == [("Host web", root_mode)] * 2


def test_shortcut_and_button_upload_same_remote_file(monkeypatch, tmp_path, gdk):
    monkeypatch.setattr(text_editor, "is_macos", lambda: False)
    writes = _record_writes(monkeypatch)
    path = tmp_path / "remote-copy.txt"
    uploads = []
    for trigger in ("button", "shortcut"):
        editor = _local_editor(path, "remote text")
        editor._is_local = False
        editor._upload_file = lambda: uploads.append(path)
        if trigger == "button":
            editor._on_save_clicked(editor._save_button)
        else:
            assert editor._on_key_pressed(None, gdk.KEY_s, 0, CONTROL) is True

    assert writes == [(str(path), "remote text")] * 2
    assert uploads == [path, path]


def test_shortcut_is_a_no_op_when_the_button_is_disabled(monkeypatch, tmp_path, gdk):
    monkeypatch.setattr(text_editor, "is_macos", lambda: False)
    writes = _record_writes(monkeypatch)
    editor = _local_editor(tmp_path / "notes.txt", "unchanged")
    editor._save_button.get_sensitive.return_value = False

    assert editor._on_key_pressed(None, gdk.KEY_s, 0, CONTROL) is False
    assert writes == []
