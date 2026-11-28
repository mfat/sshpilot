import json
import os
import posixpath


def test_drop_rejects_stale_drag_metadata(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()
    from sshpilot.file_manager import pane as pane_module

    monkeypatch.setattr(pane_module, "_", lambda msg: f"translated:{msg}")

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
        "entry_path": posixpath.join(source_pane._current_path, entry_alpha.name),
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
    assert toasts[-1] == "translated:Dragged item is no longer available"


def test_drag_payload_with_colons_is_parsed(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

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
        "entry_path": posixpath.join(source_pane._current_path, entry.name),
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
    assert args[0][0][0] == posixpath.join(source_pane._current_path, entry.name)


def test_drop_transfers_all_selected_entries(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

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


def test_drag_prepare_includes_multi_selection(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

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


def _dummy_window_for_drop(module, source_pane, *, local_pane=None):
    DummyWindow = type("DummyWindow", (), {})
    module.FileManagerWindow = DummyWindow

    window = DummyWindow()
    window._left_pane = local_pane or type("LocalPane", (), {})()
    window._right_pane = source_pane
    source_pane._get_file_manager_window = lambda: window
    return window


def _make_drag_value(module, source_pane, entry):
    payload = {
        "pane_id": id(source_pane),
        "path": source_pane._current_path,
        "position": 0,
        "entry_name": entry.name,
        # What _on_drag_prepare stores: POSIX for a remote pane.
        "entry_path": (posixpath if source_pane._is_remote else os.path).join(
            source_pane._current_path, entry.name
        ),
    }
    return json.dumps(
        {"format": "sshpilot_drag", "payload": payload},
        separators=(",", ":"),
        sort_keys=True,
    )


def test_same_pane_remote_drop_into_folder_moves(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

    FilePane = module.FilePane
    FileEntry = module.FileEntry

    source_pane = FilePane.__new__(FilePane)
    entry = FileEntry("report.txt", False, 0, 0)
    folder = FileEntry("archive", True, 0, 0)
    source_pane._entries = [entry, folder]
    source_pane._current_path = "/srv/data"
    source_pane._is_remote = True
    source_pane.show_toast = lambda *args, **kwargs: None

    window = _dummy_window_for_drop(module, source_pane)
    calls = []
    window._perform_remote_clipboard_operation = (
        lambda *args: calls.append(args)
    )
    source_pane._resolve_drop_target_folder = lambda *args, **kwargs: folder

    drag_value = _make_drag_value(module, source_pane, entry)

    result = source_pane._on_drop_string(None, drag_value, 0.0, 0.0)

    assert result is True
    assert len(calls) == 1
    entries, source_dir, destination, move = calls[0]
    assert [e.name for e in entries] == ["report.txt"]
    assert source_dir == "/srv/data"
    assert destination == "/srv/data/archive"
    assert move is True


def test_same_pane_remote_drop_on_empty_space_is_rejected(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

    FilePane = module.FilePane
    FileEntry = module.FileEntry

    source_pane = FilePane.__new__(FilePane)
    entry = FileEntry("report.txt", False, 0, 0)
    source_pane._entries = [entry]
    source_pane._current_path = "/srv/data"
    source_pane._is_remote = True
    source_pane.show_toast = lambda *args, **kwargs: None

    window = _dummy_window_for_drop(module, source_pane)
    calls = []
    window._perform_remote_clipboard_operation = (
        lambda *args: calls.append(args)
    )
    source_pane._resolve_drop_target_folder = lambda *args, **kwargs: None

    drag_value = _make_drag_value(module, source_pane, entry)

    result = source_pane._on_drop_string(None, drag_value, 0.0, 0.0)

    assert result is False
    assert calls == []


def test_same_pane_remote_drop_copies_with_primary_modifier(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

    FilePane = module.FilePane
    FileEntry = module.FileEntry

    source_pane = FilePane.__new__(FilePane)
    entry = FileEntry("report.txt", False, 0, 0)
    folder = FileEntry("archive", True, 0, 0)
    source_pane._entries = [entry, folder]
    source_pane._current_path = "/srv/data"
    source_pane._is_remote = True
    source_pane.show_toast = lambda *args, **kwargs: None

    window = _dummy_window_for_drop(module, source_pane)
    calls = []
    window._perform_remote_clipboard_operation = (
        lambda *args: calls.append(args)
    )
    source_pane._resolve_drop_target_folder = lambda *args, **kwargs: folder

    class FakeEvent:
        def get_modifier_state(self):
            return 1  # CONTROL_MASK

    class FakeDropTarget:
        def get_current_event(self):
            return FakeEvent()

    drag_value = _make_drag_value(module, source_pane, entry)

    result = source_pane._on_drop_string(FakeDropTarget(), drag_value, 0.0, 0.0)

    assert result is True
    assert len(calls) == 1
    assert calls[0][3] is False  # copy, not move


def test_same_pane_local_drop_into_folder_moves(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

    FilePane = module.FilePane
    FileEntry = module.FileEntry

    source_pane = FilePane.__new__(FilePane)
    entry = FileEntry("notes.md", False, 0, 0)
    folder = FileEntry("docs", True, 0, 0)
    source_pane._entries = [entry, folder]
    source_pane._current_path = "/home/user"
    source_pane._is_remote = False
    source_pane.show_toast = lambda *args, **kwargs: None

    window = _dummy_window_for_drop(module, source_pane, local_pane=source_pane)
    calls = []
    window._perform_local_clipboard_operation = (
        lambda *args: calls.append(args)
    )
    source_pane._resolve_drop_target_folder = lambda *args, **kwargs: folder

    drag_value = _make_drag_value(module, source_pane, entry)

    result = source_pane._on_drop_string(None, drag_value, 0.0, 0.0)

    assert result is True
    assert len(calls) == 1
    entries, source_dir, destination, move = calls[0]
    assert [e.name for e in entries] == ["notes.md"]
    assert source_dir == "/home/user"
    assert destination == os.path.join("/home/user", "docs")
    assert move is True


def test_remote_clipboard_prompts_before_self_descendant_paste(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

    FileEntry = module.FileEntry
    window = module.FileManagerWindow.__new__(module.FileManagerWindow)
    window._right_pane = type("Pane", (), {"show_toast": lambda *a, **k: None})()
    calls = []
    prompts = []
    window._manager = type(
        "Manager", (), {"copy_remote": lambda *a, **k: calls.append((a, k))}
    )()
    window._show_progress_dialog = lambda *a, **k: None
    window._attach_refresh = lambda *a, **k: None

    def _confirm(*, move, total_count, invalid_count, on_skip):
        prompts.append((move, total_count, invalid_count))
        # Cancel: do not call on_skip.

    window._confirm_into_itself_skip = _confirm

    docs = FileEntry("docs", True, 0, 0)

    window._perform_remote_clipboard_operation(
        [docs], "/srv/data", "/srv/data", False
    )
    window._perform_remote_clipboard_operation(
        [docs], "/srv/data", "/srv/data/docs/sub", True
    )
    assert calls == []
    assert prompts == [(False, 1, 1), (True, 1, 1)]

    prompts.clear()
    window._perform_remote_clipboard_operation(
        [docs], "/srv/data", "/srv/backup", True
    )
    assert prompts == []
    assert len(calls) == 1
    manager, source_path, destination_path = calls[0][0]
    assert source_path == "/srv/data/docs"
    assert destination_path == "/srv/backup/docs"
    assert calls[0][1]["recursive"] is True
    assert calls[0][1]["move"] is True


def test_remote_clipboard_skip_continues_with_sibling_files(load_file_manager_window, monkeypatch):
    """Dropping folder A + files onto A: Skip transfers only the files (Nautilus)."""
    module = load_file_manager_window()

    FileEntry = module.FileEntry
    window = module.FileManagerWindow.__new__(module.FileManagerWindow)
    window._right_pane = type("Pane", (), {"show_toast": lambda *a, **k: None})()
    calls = []
    window._manager = type(
        "Manager", (), {"copy_remote": lambda *a, **k: calls.append((a, k))}
    )()
    window._show_progress_dialog = lambda *a, **k: None
    window._attach_refresh = lambda *a, **k: None
    window._confirm_into_itself_skip = (
        lambda **kwargs: kwargs["on_skip"]()
    )

    folder = FileEntry("archive", True, 0, 0)
    file_a = FileEntry("a.txt", False, 0, 0)
    file_b = FileEntry("b.txt", False, 0, 0)

    window._perform_remote_clipboard_operation(
        [folder, file_a, file_b], "/srv/data", "/srv/data/archive", True
    )

    assert [call[0][1] for call in calls] == [
        "/srv/data/a.txt",
        "/srv/data/b.txt",
    ]
    assert [call[0][2] for call in calls] == [
        "/srv/data/archive/a.txt",
        "/srv/data/archive/b.txt",
    ]


def test_remote_clipboard_cancel_aborts_entire_batch(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

    FileEntry = module.FileEntry
    window = module.FileManagerWindow.__new__(module.FileManagerWindow)
    window._right_pane = type("Pane", (), {"show_toast": lambda *a, **k: None})()
    calls = []
    window._manager = type(
        "Manager", (), {"copy_remote": lambda *a, **k: calls.append((a, k))}
    )()
    window._show_progress_dialog = lambda *a, **k: None
    window._attach_refresh = lambda *a, **k: None
    window._confirm_into_itself_skip = lambda **_kwargs: None

    folder = FileEntry("archive", True, 0, 0)
    file_a = FileEntry("a.txt", False, 0, 0)

    window._perform_remote_clipboard_operation(
        [folder, file_a], "/srv/data", "/srv/data/archive", True
    )
    assert calls == []


def test_local_clipboard_skip_continues_with_sibling_files(load_file_manager_window, monkeypatch, tmp_path):
    module = load_file_manager_window()

    FileEntry = module.FileEntry
    window = module.FileManagerWindow.__new__(module.FileManagerWindow)
    toasts = []
    window._left_pane = type("Pane", (), {"show_toast": lambda *a, **k: toasts.append(a)})()
    window._pending_highlights = {}
    window._normalize_local_path = lambda path: path
    window._confirm_into_itself_skip = lambda **kwargs: kwargs["on_skip"]()
    refreshes = []
    window._refresh_local_listing = lambda path: refreshes.append(path)

    source = tmp_path / "data"
    archive = source / "archive"
    archive.mkdir(parents=True)
    (source / "a.txt").write_text("a")
    (source / "b.txt").write_text("b")

    folder = FileEntry("archive", True, 0, 0)
    file_a = FileEntry("a.txt", False, 0, 0)
    file_b = FileEntry("b.txt", False, 0, 0)

    window._perform_local_clipboard_operation(
        [folder, file_a, file_b], str(source), str(archive), True
    )

    assert (archive / "a.txt").read_text() == "a"
    assert (archive / "b.txt").read_text() == "b"
    assert not (archive / "archive").exists()
    assert not (source / "a.txt").exists()
    assert not (source / "b.txt").exists()
    assert archive.is_dir()


def test_is_remote_descendant_guard(load_file_manager_window, monkeypatch):
    module = load_file_manager_window()

    cls = module.FileManagerWindow
    assert cls._is_remote_descendant("/a", "/a") is True
    assert cls._is_remote_descendant("/a", "/a/b") is True
    assert cls._is_remote_descendant("/a/b", "/a") is False
    assert cls._is_remote_descendant("/a", "/ab") is False
    assert cls._is_remote_descendant("/", "/a") is False
    assert cls._is_path_into_itself("/a", "/a/b", posix=False) is True
    assert cls._is_path_into_itself("/a", "/ab", posix=False) is False


def _server_to_server_drop(module, *, same_server, modifiers):
    """Drop a file from a left pane on one server onto the right pane."""
    FilePane = module.FilePane
    FileEntry = module.FileEntry
    entry = FileEntry("report.txt", False, 0, 0)

    source_pane = FilePane.__new__(FilePane)
    source_pane._entries = [entry]
    source_pane._current_path = "/srv/data"
    source_pane._is_remote = True
    target_pane = FilePane.__new__(FilePane)
    target_pane._entries = []
    target_pane._current_path = "/root"
    target_pane._is_remote = True
    target_pane.show_toast = lambda *args, **kwargs: None
    target_pane._resolve_drop_target_folder = lambda *args, **kwargs: None

    DummyWindow = type("DummyWindow", (), {})
    module.FileManagerWindow = DummyWindow
    window = DummyWindow()
    window._left_pane = source_pane
    window._right_pane = target_pane
    window._same_server = lambda a, b: same_server
    calls = []
    window._perform_pane_to_pane_remote_operation = (
        lambda *args, **kwargs: calls.append((args, kwargs))
    )
    target_pane._get_file_manager_window = lambda: window

    class FakeEvent:
        def get_modifier_state(self):
            return modifiers

    class FakeDropTarget:
        def get_current_event(self):
            return FakeEvent()

    drag_value = _make_drag_value(module, source_pane, entry)
    assert target_pane._on_drop_string(FakeDropTarget(), drag_value, 0.0, 0.0) is True
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["source_pane"] is source_pane
    assert kwargs["destination_pane"] is target_pane
    return args[3]  # move?


def test_drag_between_two_servers_copies_by_default(load_file_manager_window):
    """A plain drag must not delete the originals on another machine."""
    module = load_file_manager_window()
    assert _server_to_server_drop(module, same_server=False, modifiers=0) is False


def test_drag_between_two_servers_moves_with_shift(load_file_manager_window):
    module = load_file_manager_window()
    shift = module.Gdk.ModifierType.SHIFT_MASK
    assert _server_to_server_drop(module, same_server=False, modifiers=shift) is True


def test_drag_between_two_panes_on_one_server_moves(load_file_manager_window):
    """One server in two panes is one file system, like two folders."""
    module = load_file_manager_window()
    assert _server_to_server_drop(module, same_server=True, modifiers=0) is True
    control = module.Gdk.ModifierType.CONTROL_MASK
    assert _server_to_server_drop(module, same_server=True, modifiers=control) is False


def _copy_to_other_panes(module, *, left_local):
    FilePane = module.FilePane
    FileEntry = module.FileEntry
    left = FilePane.__new__(FilePane)
    right = FilePane.__new__(FilePane)
    left._is_remote = not left_local
    right._is_remote = True
    left._current_path = "/srv/data"
    right._current_path = "/root"
    left._partner_pane, right._partner_pane = right, left
    entry = FileEntry("report.txt", False, 0, 0)
    left.get_selected_entries = lambda: [entry]
    left.show_toast = lambda *a, **k: None

    DummyWindow = type("DummyWindow", (), {})
    module.FileManagerWindow = DummyWindow
    window = DummyWindow()
    window._left_pane, window._right_pane = left, right
    window._local_pane = lambda: left if left_local else None
    calls = []
    window._perform_pane_to_pane_remote_operation = (
        lambda *args, **kwargs: calls.append((args, kwargs))
    )
    left._get_file_manager_window = lambda: window
    right._get_file_manager_window = lambda: window
    return left, right, entry, calls


def test_copy_to_other_pane_is_offered_only_between_two_servers(load_file_manager_window):
    """Upload and Download need this computer on one side; with a server in
    each pane the transfer is Copy to Other Pane (#1356)."""
    module = load_file_manager_window()
    left, right, _entry, _calls = _copy_to_other_panes(module, left_local=False)
    assert left._copy_to_other_available() and right._copy_to_other_available()
    assert not left._download_available()

    left, right, _entry, _calls = _copy_to_other_panes(module, left_local=True)
    assert not left._copy_to_other_available()
    assert not right._copy_to_other_available()
    assert right._download_available()


def test_copy_to_other_pane_copies_into_the_other_panes_folder(load_file_manager_window):
    module = load_file_manager_window()
    left, right, entry, calls = _copy_to_other_panes(module, left_local=False)

    left._on_copy_to_other_clicked()

    args, kwargs = calls[0]
    assert args == ([entry], "/srv/data", "/root", False)
    assert kwargs == {"source_pane": left, "destination_pane": right}


def _drag_actions(module):
    return module.Gdk.DragAction


def _pointer_action(module, *, source_remote, target_remote, same_pane=False,
                    same_server=False, modifiers=0, dragging=True):
    """The move/copy action a pane reports while a drag hovers over it."""
    _drag_actions(module)
    FilePane = module.FilePane
    source_pane = FilePane.__new__(FilePane)
    source_pane._is_remote = source_remote
    target_pane = source_pane if same_pane else FilePane.__new__(FilePane)
    target_pane._is_remote = target_remote
    target_pane.add_css_class = lambda *_args: None

    DummyWindow = type("DummyWindow", (), {})
    module.FileManagerWindow = DummyWindow
    window = DummyWindow()
    window._same_server = lambda a, b: same_server
    target_pane._get_file_manager_window = lambda: window

    class FakeEvent:
        def get_modifier_state(self):
            return modifiers

    class FakeDropTarget:
        def get_current_event(self):
            return FakeEvent()

    if dragging:
        source_pane._on_drag_begin(type("DS", (), {"get_widget": lambda self: None})(), None)
    try:
        entered = target_pane._on_drop_enter(FakeDropTarget(), 0.0, 0.0)
        moved = target_pane._on_drop_motion(FakeDropTarget(), 0.0, 0.0)
        assert entered == moved
        return moved
    finally:
        if dragging:
            source_pane._on_drag_end(None, None, False)


def test_pointer_shows_move_between_two_panes_on_one_server(load_file_manager_window):
    """#1356: the drop moved while the pointer showed the copy "+"."""
    module = load_file_manager_window()
    Action = _drag_actions(module)
    control = module.Gdk.ModifierType.CONTROL_MASK
    assert _pointer_action(module, source_remote=True, target_remote=True,
                           same_server=True) == Action.MOVE
    assert _pointer_action(module, source_remote=True, target_remote=True,
                           same_server=True, modifiers=control) == Action.COPY


def test_pointer_shows_move_within_one_pane(load_file_manager_window):
    module = load_file_manager_window()
    Action = _drag_actions(module)
    control = module.Gdk.ModifierType.CONTROL_MASK
    for remote in (True, False):
        assert _pointer_action(module, source_remote=remote, target_remote=remote,
                               same_pane=True) == Action.MOVE
        assert _pointer_action(module, source_remote=remote, target_remote=remote,
                               same_pane=True, modifiers=control) == Action.COPY


def test_pointer_shows_copy_between_two_servers_and_move_with_shift(load_file_manager_window):
    module = load_file_manager_window()
    Action = _drag_actions(module)
    shift = module.Gdk.ModifierType.SHIFT_MASK
    assert _pointer_action(module, source_remote=True, target_remote=True) == Action.COPY
    assert _pointer_action(module, source_remote=True, target_remote=True,
                           modifiers=shift) == Action.MOVE


def test_pointer_shows_copy_for_upload_and_download(load_file_manager_window):
    module = load_file_manager_window()
    Action = _drag_actions(module)
    shift = module.Gdk.ModifierType.SHIFT_MASK
    assert _pointer_action(module, source_remote=False, target_remote=True) == Action.COPY
    assert _pointer_action(module, source_remote=True, target_remote=False,
                           modifiers=shift) == Action.COPY


def test_pointer_shows_copy_for_a_drag_from_outside_the_app(load_file_manager_window):
    module = load_file_manager_window()
    module.FilePane._active_drag_source = None
    assert _pointer_action(module, source_remote=True, target_remote=True,
                           same_pane=True, dragging=False) == _drag_actions(module).COPY


def test_drag_end_forgets_the_source_pane(load_file_manager_window):
    module = load_file_manager_window()
    FilePane = module.FilePane
    pane = FilePane.__new__(FilePane)
    pane._is_remote = True
    pane._on_drag_begin(type("DS", (), {"get_widget": lambda self: None})(), None)
    assert FilePane._active_drag_source() is pane
    pane._on_drag_end(None, None, False)
    assert FilePane._active_drag_source is None


class _RecordingAlertDialog:
    """Stands in for Adw.AlertDialog; keeps the body and responses."""

    last = None

    def __init__(self, heading, body):
        self.heading, self.body = heading, body
        self.responses = {}
        self.default = None
        self.handler = None
        _RecordingAlertDialog.last = self

    @classmethod
    def new(cls, heading, body):
        return cls(heading, body)

    def add_response(self, response_id, label):
        self.responses[response_id] = label

    def set_response_appearance(self, *_args):
        pass

    def set_default_response(self, response_id):
        self.default = response_id

    def set_close_response(self, *_args):
        pass

    def connect(self, _signal, handler):
        self.handler = handler

    def present(self, *_args):
        pass


def test_activating_a_file_between_two_servers_offers_copy_to_other_pane(
    load_file_manager_window, monkeypatch
):
    """Between two servers, double-click copies to the other pane; it must
    not offer a Download that does nothing (#1431)."""
    module = load_file_manager_window()
    from sshpilot.file_manager import pane as pane_module
    monkeypatch.setattr(pane_module.Adw, "AlertDialog", _RecordingAlertDialog, raising=False)
    monkeypatch.setattr(
        pane_module.Adw, "ResponseAppearance",
        type("ResponseAppearance", (), {"SUGGESTED": 1}), raising=False,
    )
    left, _right, entry, calls = _copy_to_other_panes(module, left_local=False)
    selected = []
    left._selection_model = type(
        "Selection", (), {"select_item": lambda _self, *args: selected.append(args)}
    )()

    left._offer_remote_file_actions(0, entry)

    dialog = _RecordingAlertDialog.last
    assert "download" not in dialog.responses
    assert dialog.responses["copy_to_other"] == "Copy to Other Pane"
    assert dialog.default == "copy_to_other"
    assert "other pane" in dialog.body

    assert dialog.responses["download_to_computer"] == "Download to This Computer…"

    dialog.handler(dialog, "copy_to_other")
    assert selected == [(0, True)]
    args, kwargs = calls[0]
    assert args == ([entry], "/srv/data", "/root", False)

    asked = []
    left._on_menu_download_to_computer = lambda: asked.append(True)
    dialog.handler(dialog, "download_to_computer")
    assert asked == [True]


def test_activating_a_server_file_beside_this_computer_offers_download(
    load_file_manager_window, monkeypatch
):
    module = load_file_manager_window()
    from sshpilot.file_manager import pane as pane_module
    monkeypatch.setattr(pane_module.Adw, "AlertDialog", _RecordingAlertDialog, raising=False)
    monkeypatch.setattr(
        pane_module.Adw, "ResponseAppearance",
        type("ResponseAppearance", (), {"SUGGESTED": 1}), raising=False,
    )
    _left, right, entry, _calls = _copy_to_other_panes(module, left_local=True)

    right._offer_remote_file_actions(0, entry)

    dialog = _RecordingAlertDialog.last
    assert dialog.default == "download"
    assert "copy_to_other" not in dialog.responses
    assert dialog.responses["download_to_computer"] == "Download to This Computer…"
    assert "this computer" in dialog.body


def test_download_to_this_computer_is_offered_on_every_server_pane(load_file_manager_window):
    module = load_file_manager_window()
    left, right, _entry, _calls = _copy_to_other_panes(module, left_local=False)
    assert left._download_to_computer_available()
    assert right._download_to_computer_available()

    left, right, _entry, _calls = _copy_to_other_panes(module, left_local=True)
    assert not left._download_to_computer_available()
    assert right._download_to_computer_available()


def test_download_to_this_computer_downloads_into_the_picked_folder(
    load_file_manager_window, monkeypatch, tmp_path
):
    module = load_file_manager_window()
    from sshpilot.file_manager import pane as pane_module

    class _Folder:
        def get_path(self):
            return str(tmp_path)

    class _FileDialog:
        def set_title(self, *_args):
            pass

        def set_initial_folder(self, *_args):
            pass

        def select_folder(self, _parent, _cancellable, callback):
            callback(self, None)

        def select_folder_finish(self, _result):
            return _Folder()

    monkeypatch.setattr(pane_module.Gtk, "FileDialog", _FileDialog, raising=False)
    monkeypatch.setattr(
        pane_module.GLib, "get_user_special_dir", lambda *_args: None, raising=False
    )
    left, _right, entry, _calls = _copy_to_other_panes(module, left_local=False)
    left.get_root = lambda: None
    emitted = []
    left.emit = lambda *args: emitted.append(args)

    left._on_menu_download_to_computer()

    assert emitted == [(
        "request-operation", "download",
        {"entries": [entry], "directory": "/srv/data", "destination": tmp_path},
    )]
