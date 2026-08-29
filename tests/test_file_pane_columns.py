import re
from types import SimpleNamespace

from sshpilot.file_manager import format_utils
from sshpilot.file_manager.common import FileEntry
from sshpilot.file_manager.pane import FilePane, _column_view_supported


def test_size_column_text_uses_item_count_for_folders():
    folder = FileEntry("docs", True, 0, 0, item_count=3)
    empty = FileEntry("empty", True, 0, 0, item_count=None)
    file_entry = FileEntry("notes.txt", False, 2048, 0)

    assert FilePane._size_column_text(folder) == "3 items"
    assert FilePane._size_column_text(empty) == "—"
    assert FilePane._size_column_text(file_entry) == "2.0 KB"


def test_item_count_uses_gettext_plural_before_formatting(monkeypatch):
    calls = []

    def translate(singular, plural, count):
        calls.append((singular, plural, count))
        return "{count} élément" if count == 1 else "{count} éléments"

    monkeypatch.setattr(format_utils, "ngettext", translate)

    assert format_utils._item_count_text(0) == "0 éléments"
    assert format_utils._item_count_text(1) == "1 élément"
    assert format_utils._item_count_text(4) == "4 éléments"
    assert calls == [
        ("{count} item", "{count} items", 0),
        ("{count} item", "{count} items", 1),
        ("{count} item", "{count} items", 4),
    ]


def test_modified_column_text_formats_timestamp():
    missing = FileEntry("a", False, 1, 0)
    dated = FileEntry("b", False, 1, 1_700_000_000)

    assert FilePane._modified_column_text(missing) == "—"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", FilePane._modified_column_text(dated))


def test_sort_entries_by_modified_keeps_directories_first():
    pane = FilePane.__new__(FilePane)
    pane._sort_key = "modified"
    pane._sort_descending = True
    older = FileEntry("old.txt", False, 1, 100)
    newer = FileEntry("new.txt", False, 1, 200)
    dir_old = FileEntry("adir", True, 0, 50)
    dir_new = FileEntry("zdir", True, 0, 300)

    ordered = pane._sort_entries([older, newer, dir_old, dir_new])
    assert [entry.name for entry in ordered] == ["zdir", "adir", "new.txt", "old.txt"]


def test_column_sorter_changed_updates_sort_key():
    pane = FilePane.__new__(FilePane)
    pane._sort_key = "name"
    pane._sort_descending = False
    pane._syncing_column_sort = False
    pane._use_column_view = True
    pane._refresh_calls = []
    pane._direction_updates = 0

    def _refresh(*, preserve_selection):
        pane._refresh_calls.append(preserve_selection)

    pane._refresh_sorted_entries = _refresh
    pane._update_sort_direction_states = lambda: setattr(
        pane, "_direction_updates", pane._direction_updates + 1
    )

    class _Column:
        def get_id(self):
            return "modified"

    class _Sorter:
        def get_primary_sort_column(self):
            return _Column()

        def get_primary_sort_order(self):
            from gi.repository import Gtk

            return Gtk.SortType.DESCENDING

    pane._on_column_sorter_changed(_Sorter())
    assert pane._sort_key == "modified"
    assert pane._sort_descending is True
    assert pane._refresh_calls == [True]
    assert pane._direction_updates == 1


def test_column_sorter_ignored_on_legacy_list_view():
    pane = FilePane.__new__(FilePane)
    pane._sort_key = "name"
    pane._sort_descending = False
    pane._syncing_column_sort = False
    pane._use_column_view = False
    called = []

    def _refresh(**_kwargs):
        called.append(True)

    pane._refresh_sorted_entries = _refresh
    pane._on_column_sorter_changed(object())
    assert pane._sort_key == "name"
    assert called == []


class _GtkVersion:
    def __init__(self, major, minor, *, column_view=True, column_view_cell=True):
        self.MAJOR_VERSION = major
        self.MINOR_VERSION = minor
        self.ColumnView = object if column_view else None
        self.ColumnViewCell = object if column_view_cell else None


def test_column_view_supported_requires_gtk_412():
    assert not _column_view_supported(_GtkVersion(4, 6))
    assert not _column_view_supported(_GtkVersion(4, 10))
    assert not _column_view_supported(_GtkVersion(4, 11))
    assert _column_view_supported(_GtkVersion(4, 12))
    assert _column_view_supported(_GtkVersion(4, 22))


def test_column_view_supported_false_without_column_view_cell():
    assert not _column_view_supported(_GtkVersion(4, 12, column_view_cell=False))
    assert not _column_view_supported(_GtkVersion(4, 22, column_view=False))


def test_column_view_supported_false_for_non_numeric_version():
    assert not _column_view_supported(SimpleNamespace())


def test_update_item_counts_refreshes_legacy_list_rows():
    pane = FilePane.__new__(FilePane)
    pane._current_path = "/home"
    pane._is_remote = True
    folder = FileEntry("docs", True, 0, 0, item_count=None)
    pane._cached_entries = [folder]
    pane._bound_size_labels = set()

    class _Label:
        def __init__(self):
            self.text = "—"
            self.tooltip = None

        def set_text(self, text):
            self.text = text

        def set_tooltip_text(self, text):
            self.tooltip = text

    class _Box:
        def __init__(self, entry, label):
            self._pane_entry = entry
            self.metadata_label = label

    label = _Label()
    pane._bound_list_boxes = {_Box(folder, label)}

    pane.update_item_counts("/home", {"docs": 4})
    assert folder.item_count == 4
    assert label.text == "4 items"
    assert label.tooltip == "4 items"


def test_sync_column_sort_indicator_noop_on_legacy_list_view():
    pane = FilePane.__new__(FilePane)
    pane._use_column_view = False
    called = []
    pane._list_view = SimpleNamespace(sort_by_column=lambda *_args: called.append(True))
    pane._list_columns = {"name": object()}
    pane._sort_key = "name"
    pane._sort_descending = False
    pane._sync_column_sort_indicator()
    assert called == []
