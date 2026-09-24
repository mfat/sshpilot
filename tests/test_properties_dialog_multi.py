"""Multi-selection helpers for the Nautilus-style properties dialog."""

from sshpilot.file_manager.common import FileEntry
from sshpilot.file_manager import properties_dialog as properties


def test_selection_title_lists_a_few_names():
    entries = [
        FileEntry("a.txt", False, 1, 0.0),
        FileEntry("b.txt", False, 2, 0.0),
    ]
    assert properties._selection_title(entries) == "a.txt, b.txt"


def test_selection_title_falls_back_to_a_count():
    entries = [FileEntry(f"f{i}.txt", False, i, 0.0) for i in range(6)]
    assert properties._selection_title(entries) == "6 Selected Items"


def test_selection_size_text_sums_like_nautilus():
    assert properties._selection_size_text(2, 2048) == "2 items, totalling 2.0 KB"
    assert properties._selection_size_text(1, 512) == "1 item, with size 512 B"


def test_folder_size_text_matches_nautilus_contents_line():
    assert properties._folder_size_text(0, 0) == "Empty folder"
    assert properties._folder_size_text(3, 2048) == "3 items, totalling 2.0 KB"


def test_header_icon_uses_colored_mimetype_names():
    from sshpilot.file_type_icons import ICON_FOLDER, ICON_GENERIC

    dialog = properties.PropertiesDialog.__new__(properties.PropertiesDialog)
    dialog._entry = FileEntry("notes.txt", False, 1, 0.0)
    dialog._entries = [dialog._entry]
    assert dialog._header_icon_name() == "text-x-generic"

    dialog._entry = FileEntry("docs", True, 0, 0.0)
    dialog._entries = [dialog._entry]
    assert dialog._header_icon_name() == ICON_FOLDER

    dialog._entries = [
        FileEntry("a", True, 0, 0.0),
        FileEntry("b.txt", False, 1, 0.0),
    ]
    dialog._entry = dialog._entries[0]
    assert dialog._header_icon_name() == ICON_GENERIC


def test_common_value_requires_agreement():
    assert properties._common_value(["a", "a", "a"]) == "a"
    assert properties._common_value(["a", "b"]) is None
    assert properties._common_value([None, None]) is None
