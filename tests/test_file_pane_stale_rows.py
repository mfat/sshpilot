"""Navigating must not paint the previous directory's names.

GTK binds the new rows synchronously from inside the list-store mutation, and
the cell factories resolve each row's entry through ``self._entries[position]``.
While the pane published the store before ``self._entries``, every row bound
during that window described a file from the directory the user just left --
so e.g. stepping /root -> / -> /root showed leftover 'bin'/'cache' rows that
only disappeared on a manual reload.
"""

import pytest

# _restore_module_registry is an autouse fixture: importing it here undoes this
# module's rebuilding of the gi stubs and its purge of the sshpilot.file_manager
# chain, both of which live in the process-global sys.modules.
from tests.test_file_pane_typeahead import (  # noqa: F401
    _load_file_manager_window,
    _restore_module_registry,
)


class FakeStringObject:
    def __init__(self, value):
        self._value = value

    @classmethod
    def new(cls, value):
        return cls(value)

    def get_string(self):
        return self._value


class FakeCell:
    """The bit of ``Gtk.ListItem`` the cell factories read."""

    def __init__(self, position, item):
        self._position = position
        self._item = item

    def get_position(self):
        return self._position

    def get_item(self):
        return self._item


class BindingListStore:
    """A list store that binds its rows the way GtkListView does: during the
    model mutation itself, resolving each row through the pane."""

    def __init__(self, pane):
        self.items = []
        self._pane = pane
        self.bound_names = []

    def get_n_items(self):
        return len(self.items)

    def remove_all(self):
        self.items = []
        self._bind_all()

    def append(self, item):
        self.items.append(item)
        self._bind_all()

    def splice(self, position, n_removals, additions):
        self.items[position:position + n_removals] = list(additions)
        self._bind_all()

    def _bind_all(self):
        """What every visible row would display right now."""
        self.bound_names = []
        for position, item in enumerate(self.items):
            _, entry = self._pane._entry_from_cell(FakeCell(position, item))
            if entry is None:
                self.bound_names.append(item.get_string())
            else:
                self.bound_names.append(entry.name + ("/" if entry.is_dir else ""))


class FakeSelectionModel:
    def __init__(self):
        self.selected = []

    def unselect_all(self):
        self.selected = []

    def select_item(self, index, _unselect_rest):
        self.selected.append(index)


def _entry(module, name, is_dir=False):
    return module.FileEntry(name, is_dir, 0, 0.0, None)


def _make_pane(module):
    FilePane = module.FilePane
    pane = FilePane.__new__(FilePane)
    pane._cached_entries = []
    pane._raw_entries = []
    pane._entries = []
    pane._show_hidden = False
    pane._sort_key = "name"
    pane._sort_descending = False
    pane._list_store = BindingListStore(pane)
    pane._selection_model = FakeSelectionModel()
    pane._selection_anchor = None
    pane._update_menu_state = lambda: None
    return pane


def _list(pane, entries):
    pane._cached_entries = list(entries)
    pane._apply_entry_filter(preserve_selection=False)


def test_rows_bound_during_the_update_describe_the_new_directory(monkeypatch):
    module = _load_file_manager_window()
    monkeypatch.setattr(module.Gtk, "StringObject", FakeStringObject, raising=False)
    pane = _make_pane(module)

    _list(pane, [_entry(module, "bin", is_dir=True), _entry(module, "cache", is_dir=True)])
    assert pane._list_store.bound_names == ["bin/", "cache/"]

    # Navigate into a directory holding entirely different names.
    _list(pane, [_entry(module, "notes.txt"), _entry(module, "ssh", is_dir=True)])

    assert pane._list_store.bound_names == ["ssh/", "notes.txt"]
    assert [entry.name for entry in pane._entries] == ["ssh", "notes.txt"]


def test_a_shorter_listing_leaves_no_row_from_the_previous_one(monkeypatch):
    module = _load_file_manager_window()
    monkeypatch.setattr(module.Gtk, "StringObject", FakeStringObject, raising=False)
    pane = _make_pane(module)

    _list(pane, [_entry(module, f"old{index}.txt") for index in range(6)])
    _list(pane, [_entry(module, "only.txt")])

    assert pane._list_store.bound_names == ["only.txt"]
    assert [item.get_string() for item in pane._list_store.items] == ["only.txt"]


def test_entries_are_published_before_the_store_changes(monkeypatch):
    """The ordering invariant itself, independent of how GTK binds."""
    module = _load_file_manager_window()
    monkeypatch.setattr(module.Gtk, "StringObject", FakeStringObject, raising=False)
    pane = _make_pane(module)

    _list(pane, [_entry(module, "old.txt")])

    seen = []
    original_splice = pane._list_store.splice

    def _recording_splice(position, n_removals, additions):
        seen.append([entry.name for entry in pane._entries])
        return original_splice(position, n_removals, additions)

    pane._list_store.splice = _recording_splice
    _list(pane, [_entry(module, "new.txt")])

    assert seen == [["new.txt"]]
