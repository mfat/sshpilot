import json
import os

from tests.test_file_pane_typeahead import _load_file_manager_window


def test_drop_rejects_stale_drag_metadata(monkeypatch):
    module = _load_file_manager_window()

    FilePane = module.FilePane
    FileEntry = module.FileEntry

    source_pane = FilePane.__new__(FilePane)
    entry_alpha = FileEntry("alpha", False, 0, 0)
    entry_beta = FileEntry("beta", False, 0, 0)
    source_pane._entries = [entry_alpha, entry_beta]
    source_pane._current_path = "/source"
    source_pane._is_remote = True

    target_pane = FilePane.__new__(FilePane)
    target_pane._is_remote = False
    target_pane._current_path = "/target"

    toasts = []
    download_calls = []

    target_pane.show_toast = lambda message, timeout=-1: toasts.append(message)
    target_pane._handle_download_from_drag = (
        lambda *args, **kwargs: download_calls.append((args, kwargs))
    )

    DummyWindow = type("DummyWindow", (), {})
    monkeypatch.setattr(module, "FileManagerWindow", DummyWindow)

    window = DummyWindow()
    window._left_pane = source_pane
    window._right_pane = target_pane

    target_pane._get_file_manager_window = lambda: window

    payload = {
        "pane_id": id(source_pane),
        "path": source_pane._current_path,
        "position": 0,
        "entry_name": entry_alpha.name,
        "entry_path": os.path.join(source_pane._current_path, entry_alpha.name),
    }
    drag_value = json.dumps(
        {"format": "sshpilot_drag", "payload": payload},
        separators=(",", ":"),
        sort_keys=True,
    )

    # Simulate entries being reordered and refreshed without the dragged item
    source_pane._entries = [
        FileEntry("beta", False, 0, 0),
        FileEntry("gamma", False, 0, 0),
    ]

    result = target_pane._on_drop_string(None, drag_value, 0.0, 0.0)

    assert result is False
    assert download_calls == []
    assert toasts
    assert toasts[-1] == "Dragged item is no longer available"


def test_drag_payload_with_colons_is_parsed(monkeypatch):
    module = _load_file_manager_window()

    FilePane = module.FilePane
    FileEntry = module.FileEntry

    source_pane = FilePane.__new__(FilePane)
    entry = FileEntry("report:2024.txt", False, 0, 0)
    source_pane._entries = [entry]
    source_pane._current_path = "/var:data"
    source_pane._is_remote = True

    target_pane = FilePane.__new__(FilePane)
    target_pane._is_remote = False
    target_pane._current_path = "/tmp"

    download_calls = []
    target_pane.show_toast = lambda *args, **kwargs: None
    target_pane._handle_download_from_drag = (
        lambda *args, **kwargs: download_calls.append((args, kwargs))
    )

    DummyWindow = type("DummyWindow", (), {})
    monkeypatch.setattr(module, "FileManagerWindow", DummyWindow)

    window = DummyWindow()
    window._left_pane = source_pane
    window._right_pane = target_pane

    target_pane._get_file_manager_window = lambda: window

    payload = {
        "pane_id": id(source_pane),
        "path": source_pane._current_path,
        "position": 0,
        "entry_name": entry.name,
        "entry_path": os.path.join(source_pane._current_path, entry.name),
    }
    drag_value = json.dumps(
        {"format": "sshpilot_drag", "payload": payload},
        separators=(",", ":"),
        sort_keys=True,
    )

    result = target_pane._on_drop_string(None, drag_value, 0.0, 0.0)

    assert result is True
    assert len(download_calls) == 1
    args, kwargs = download_calls[0]
    assert kwargs == {}
    assert len(args[0]) == 1
    assert args[0][0][0] == os.path.join(source_pane._current_path, entry.name)


def test_drop_transfers_all_selected_entries(monkeypatch):
    module = _load_file_manager_window()

    FilePane = module.FilePane
    FileEntry = module.FileEntry

    source_pane = FilePane.__new__(FilePane)
    entry_alpha = FileEntry("alpha.txt", False, 0, 0)
    entry_beta = FileEntry("beta.txt", False, 0, 0)
    entry_gamma = FileEntry("gamma.txt", False, 0, 0)
    source_pane._entries = [entry_alpha, entry_beta, entry_gamma]
    source_pane._current_path = "/source"
    source_pane._is_remote = False

    target_pane = FilePane.__new__(FilePane)
    target_pane._is_remote = True
    target_pane._current_path = "/remote"

    upload_calls = []
    target_pane.show_toast = lambda *args, **kwargs: None
    target_pane._handle_upload_from_drag = (
        lambda items, target_folder=None: upload_calls.append((items, target_folder))
    )
    target_pane._resolve_drop_target_folder = lambda *args, **kwargs: None

    DummyWindow = type("DummyWindow", (), {})
    monkeypatch.setattr(module, "FileManagerWindow", DummyWindow)

    window = DummyWindow()
    window._left_pane = source_pane
    window._right_pane = target_pane

    target_pane._get_file_manager_window = lambda: window

    payload = {
        "pane_id": id(source_pane),
        "path": source_pane._current_path,
        "position": 1,
        "entry_name": entry_beta.name,
        "entry_path": os.path.join(source_pane._current_path, entry_beta.name),
        "entries": [
            {
                "entry_name": entry_alpha.name,
                "entry_path": os.path.join(source_pane._current_path, entry_alpha.name),
            },
            {
                "entry_name": entry_beta.name,
                "entry_path": os.path.join(source_pane._current_path, entry_beta.name),
            },
        ],
    }
    drag_value = json.dumps(
        {"format": "sshpilot_drag", "payload": payload},
        separators=(",", ":"),
        sort_keys=True,
    )

    result = target_pane._on_drop_string(None, drag_value, 0.0, 0.0)

    assert result is True
    assert len(upload_calls) == 1
    items, target_folder = upload_calls[0]
    assert target_folder is None
    assert len(items) == 2
    assert [entry.name for _, entry in items] == ["alpha.txt", "beta.txt"]


def test_drag_prepare_includes_multi_selection(monkeypatch):
    module = _load_file_manager_window()

    FilePane = module.FilePane
    FileEntry = module.FileEntry

    pane = FilePane.__new__(FilePane)
    entries = [
        FileEntry("one.txt", False, 0, 0),
        FileEntry("two.txt", False, 0, 0),
        FileEntry("three.txt", False, 0, 0),
    ]
    pane._entries = entries
    pane._current_path = "/home/user"

    class FakeSelection:
        def is_selected(self, index: int) -> bool:
            return index in (0, 2)

    pane._selection_model = FakeSelection()

    drag_entries = pane._entries_for_drag_at_position(0)
    assert [entry.name for entry in drag_entries] == ["one.txt", "three.txt"]

    drag_entries = pane._entries_for_drag_at_position(1)
    assert [entry.name for entry in drag_entries] == ["two.txt"]
