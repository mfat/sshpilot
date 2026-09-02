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
