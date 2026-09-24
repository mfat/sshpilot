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


def test_common_value_requires_agreement():
    assert properties._common_value(["a", "a", "a"]) == "a"
    assert properties._common_value(["a", "b"]) is None
    assert properties._common_value([None, None]) is None
