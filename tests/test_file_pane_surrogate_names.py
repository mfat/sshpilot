"""Local filenames whose bytes are not valid UTF-8 must not break the pane.

``os.scandir`` decodes undecodable filesystem bytes with ``surrogateescape``,
so such a name reaches the UI as a string holding lone surrogates. PyGObject
encodes every Python string to UTF-8 for the C call and raises
``UnicodeEncodeError: ... surrogates not allowed`` on those, which used to blow
up the whole directory listing (issue #1235).
"""

import pytest

# _restore_module_registry is an autouse fixture: importing it here undoes this
# module's rebuilding of the gi stubs and its purge of the sshpilot.file_manager
# chain, both of which live in the process-global sys.modules.
from tests.test_file_pane_typeahead import (  # noqa: F401
    _load_file_manager_window,
    _restore_module_registry,
)


BAD_NAME = "\udce5\udcb1 broken.txt"


class FakeStringObject:
    """Stands in for ``Gtk.StringObject``, including its UTF-8 strictness."""

    def __init__(self, value):
        # PyGObject encodes the string for the C call right here.
        value.encode("utf-8")
        self._value = value

    @classmethod
    def new(cls, value):
        return cls(value)

    def get_string(self):
        return self._value


class FakeListStore:
    def __init__(self):
        self.items = []

    def remove_all(self):
        self.items = []

    def append(self, item):
        self.items.append(item)


class FakeSelectionModel:
    def __init__(self):
        self.selected = []

    def unselect_all(self):
        self.selected = []

    def select_item(self, index, _unselect_rest):
        self.selected.append(index)


def _make_pane(module, entries):
    FilePane = module.FilePane
    pane = FilePane.__new__(FilePane)
    pane._cached_entries = list(entries)
    pane._raw_entries = []
    pane._entries = []
    pane._show_hidden = False
    pane._sort_key = "name"
    pane._sort_descending = False
    pane._list_store = FakeListStore()
    pane._selection_model = FakeSelectionModel()
    pane._selection_anchor = None
    pane._update_menu_state = lambda: None
    return pane


def _entry(module, name, is_dir=False):
    return module.FileEntry(name, is_dir, 0, 0.0, None)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("plain.txt", "plain.txt"),
        ("ünïcode ✓.txt", "ünïcode ✓.txt"),
        ("", ""),
        ("\udce5\udcb1x", "�x"),
        ("\ud800x", "�x"),
    ],
)
def test_safe_display_text(value, expected):
    safe_display_text = _load_file_manager_window().safe_display_text
    assert safe_display_text(value) == expected


def test_safe_display_text_output_is_always_encodable():
    safe_display_text = _load_file_manager_window().safe_display_text
    for value in (BAD_NAME, "\udcff", "a\udce5b", "ok"):
        safe_display_text(value).encode("utf-8")


def test_listing_survives_a_name_that_is_not_valid_utf8(monkeypatch):
    module = _load_file_manager_window()
    monkeypatch.setattr(module.Gtk, "StringObject", FakeStringObject, raising=False)

    entries = [
        _entry(module, "docs", is_dir=True),
        _entry(module, BAD_NAME),
        _entry(module, "readme.md"),
    ]
    pane = _make_pane(module, entries)

    pane._apply_entry_filter(preserve_selection=False)

    # Every entry is listed, and the raw name is preserved for file operations.
    # (Folders first, then files by casefolded name -- surrogates sort last.)
    assert [entry.name for entry in pane._entries] == [
        "docs",
        "readme.md",
        BAD_NAME,
    ]
    assert [item.get_string() for item in pane._list_store.items] == [
        "docs/",
        "readme.md",
        "� broken.txt",
    ]


def test_a_row_gtk_still_rejects_does_not_blank_the_pane(monkeypatch):
    """Store and ``_entries`` stay index-aligned when a row is dropped."""
    module = _load_file_manager_window()

    class RejectsOneName(FakeStringObject):
        @classmethod
        def new(cls, value):
            if value.startswith("reject"):
                raise UnicodeEncodeError("utf-8", value, 0, 1, "nope")
            return cls(value)

    monkeypatch.setattr(module.Gtk, "StringObject", RejectsOneName, raising=False)

    entries = [
        _entry(module, "alpha.txt"),
        _entry(module, "reject-me.txt"),
        _entry(module, "zeta.txt"),
    ]
    pane = _make_pane(module, entries)

    pane._apply_entry_filter(preserve_selection=False)

    assert [entry.name for entry in pane._entries] == ["alpha.txt", "zeta.txt"]
    assert [item.get_string() for item in pane._list_store.items] == [
        "alpha.txt",
        "zeta.txt",
    ]


# --- the sinks that take a name straight from the entry ---------------------
#
# The stubbed gi in these tests does not enforce UTF-8 the way PyGObject does,
# so each of these installs a recorder that raises the same UnicodeEncodeError
# real GTK raises. That is the whole invariant: no string reaching a GTK/Adw
# constructor may carry a lone surrogate.


def _strict(name, returns=None):
    """A GTK/Adw stand-in that rejects arguments PyGObject could not encode."""
    import unittest.mock

    calls = []

    def check(value):
        if isinstance(value, str):
            value.encode("utf-8")

    class Recorder:
        def __init__(self, *args, **kwargs):
            for value in args:
                check(value)
            for value in kwargs.values():
                check(value)
            calls.append((args, kwargs))
            self._mock = unittest.mock.MagicMock()

        def __getattr__(self, attr):
            return getattr(self._mock, attr)

        @classmethod
        def new(cls, *args, **kwargs):
            return cls(*args, **kwargs)

    Recorder.__name__ = name
    Recorder.calls = calls
    return Recorder


def test_properties_dialog_header_shows_a_name_gtk_can_encode(monkeypatch):
    import sys
    import unittest.mock

    module = _load_file_manager_window()
    dialogs = sys.modules["sshpilot.file_manager.properties_dialog"]
    from sshpilot import icon_utils

    label = _strict("Label")
    monkeypatch.setattr(dialogs.Gtk, "Label", label, raising=False)
    monkeypatch.setattr(dialogs.Gtk, "Box", unittest.mock.MagicMock(), raising=False)
    monkeypatch.setattr(dialogs.Gtk, "Orientation", unittest.mock.MagicMock(), raising=False)
    monkeypatch.setattr(dialogs.Gtk, "Align", unittest.mock.MagicMock(), raising=False)
    monkeypatch.setattr(
        icon_utils,
        "new_image_from_icon_name",
        lambda *a, **k: unittest.mock.MagicMock(),
    )

    dialog = dialogs.PropertiesDialog.__new__(dialogs.PropertiesDialog)
    dialog._entry = module.FileEntry(BAD_NAME, False, 12, 0.0, None)
    dialog._current_path = "/tmp"
    dialog._is_remote_file = lambda: True

    dialogs.PropertiesDialog._create_header_block(dialog)

    assert label.calls[0][1]["label"] == "� broken.txt"


def test_properties_dialog_parent_row_shows_a_path_gtk_can_encode(monkeypatch):
    import sys

    module = _load_file_manager_window()
    dialogs = sys.modules["sshpilot.file_manager.properties_dialog"]
    row = _strict("ActionRow")
    monkeypatch.setattr(dialogs.Adw, "ActionRow", row, raising=False)

    dialog = dialogs.PropertiesDialog.__new__(dialogs.PropertiesDialog)
    dialog._entry = module.FileEntry("file.txt", False, 12, 0.0, None)
    dialog._current_path = "/tmp/bad\udce5dir"
    dialog._is_remote_file = lambda: True

    dialogs.PropertiesDialog._create_parent_folder_row(dialog)

    assert row.calls[0][1]["subtitle"] == "/tmp/bad�dir"


def test_fallback_properties_dialog_heading_is_encodable(monkeypatch):
    import sys

    module = _load_file_manager_window()
    pane_mod = sys.modules["sshpilot.file_manager.pane"]
    message_dialog = _strict("MessageDialog")
    monkeypatch.setattr(pane_mod.Adw, "MessageDialog", message_dialog, raising=False)

    pane = module.FilePane.__new__(module.FilePane)
    entry = module.FileEntry(BAD_NAME, False, 12, 0.0, None)
    details = dict.fromkeys(("name", "type", "size", "modified", "location"), "—")

    module.FilePane._show_fallback_properties_dialog(pane, entry, details, None)

    assert message_dialog.calls[0][1]["heading"] == "� broken.txt Properties"


def test_editor_toast_is_encodable(monkeypatch):
    import sys

    _load_file_manager_window()
    editor = sys.modules["sshpilot.text_editor"]
    toast = _strict("Toast")
    monkeypatch.setattr(editor.Adw, "Toast", toast, raising=False)

    window = editor.RemoteFileEditorWindow.__new__(editor.RemoteFileEditorWindow)
    window._current_toast = None
    window._toast_overlay = type("Overlay", (), {"add_toast": lambda self, _t: None})()

    editor.RemoteFileEditorWindow._show_toast(window, f"Save failed: {BAD_NAME}")

    assert toast.calls[0][0][0] == "Save failed: � broken.txt"


def test_closing_an_edited_file_still_prompts(monkeypatch):
    """The unsaved-changes prompt must survive a name GTK cannot encode.

    ``_check_and_close`` runs inside the ``close-request`` handler, and
    PyGObject turns an exception there into a ``False`` return -- which closes
    the window and discards the changes the prompt was about.
    """
    import sys

    _load_file_manager_window()
    editor = sys.modules["sshpilot.text_editor"]
    alert = _strict("AlertDialog")
    monkeypatch.setattr(editor.Adw, "AlertDialog", alert, raising=False)

    window = editor.RemoteFileEditorWindow.__new__(editor.RemoteFileEditorWindow)
    window._buffer = type("Buffer", (), {"get_modified": lambda self: True})()
    window._is_local = True
    window._file_name = BAD_NAME

    editor.RemoteFileEditorWindow._check_and_close(window)

    assert alert.calls, "the prompt was skipped"
    assert alert.calls[0][0][1] == (
        "You have unsaved changes to � broken.txt. Save changes before closing?"
    )
