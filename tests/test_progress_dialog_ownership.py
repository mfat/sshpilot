"""Copy/move/delete and batch transfers each drive only their own dialog.

A copy running while upload batches start on top must not close (and so
cancel) a batch, must not finish a batch's dialog, and window cleanup must
close every dialog still open.
"""

from concurrent.futures import Future


class _Dialog:
    def __init__(self, operation_type, total_files):
        self.operation_type = operation_type
        self.total_files = total_files
        self.files_completed = 0
        self.is_cancelled = False
        self.closed = False
        self.completions = []
        self._failed_files = []
        self._latest_fraction = None

    # -- SFTPProgressDialog surface used by the window --------------------
    def is_reusable(self):
        return not (self.closed or self.is_cancelled or self.completions)

    def get_visible(self):
        return not self.closed

    def set_operation_details(self, total_files, filename=None):
        self.total_files = max(self.total_files, total_files)

    def set_paths(self, *_args):
        pass

    def set_future(self, _future):
        pass

    def update_progress(self, fraction, message=None, current_file=None):
        self._latest_fraction = fraction

    def on_bytes(self, *_args):
        pass

    def set_files_completed(self, done):
        self.files_completed = done

    def increment_file_count(self):
        self.files_completed += 1

    def show_completion(self, success=True, error_message=None):
        self.completions.append((success, error_message))

    def close(self):
        if not self.completions:
            self.is_cancelled = True
        self.closed = True


class _Manager:
    def __init__(self):
        self.handlers = {}
        self.batches = []

    def connect(self, signal, handler):
        handler_id = len(self.handlers) + 1
        self.handlers[handler_id] = (signal, handler)
        return handler_id

    def disconnect(self, handler_id):
        self.handlers.pop(handler_id, None)

    def emit(self, signal, *args):
        for name, handler in list(self.handlers.values()):
            if name == signal:
                handler(self, *args)

    def transfer_batch(self, direction, items):
        future = Future()
        self.batches.append(future)
        return future


def _window(window_module, monkeypatch):
    # The stub's idle_add drops callbacks; run them on the spot instead.
    monkeypatch.setattr(
        window_module.GLib, "idle_add", lambda fn, *args, **kwargs: fn(*args), raising=False
    )
    window = window_module.FileManagerWindow.__new__(window_module.FileManagerWindow)
    window._manager = _Manager()
    window._transfer_dialogs = []
    window._progress_dialog = None
    window._aggregate_dialog = None
    window._pending_highlights = {}
    window.created = []

    def _present(operation_type, *, total_files, filename):
        dialog = _Dialog(operation_type, total_files)
        window.created.append(dialog)
        return dialog

    window._present_progress_dialog = _present
    window._attach_refresh = lambda *args, **kwargs: None
    return window


def test_copy_and_batches_keep_their_own_dialogs(load_file_manager_window, monkeypatch):
    window_module = load_file_manager_window()
    from sshpilot.daemon_sftp_backend import TransferBatchResult

    window = _window(window_module, monkeypatch)
    copies = [Future(), Future()]
    for future in copies:
        window._show_progress_dialog("copy", "big", future, total_files=2)
    (copy_dialog,) = window.created

    window._start_transfer_batch("upload", [("/l/a", "/r/a", False), ("/l/b", "/r/b", False)])
    window._start_transfer_batch("upload", [("/l/c", "/r/c", False)])
    first_batch, second_batch = window.created[1:]

    # Starting batches neither closed the copy nor each other.
    assert not copy_dialog.closed and not first_batch.closed
    assert window._aggregate_dialog is copy_dialog
    assert window._transfer_dialogs == [first_batch, second_batch]

    # Batch progress reaches only its own dialog.
    key = f"fut-{id(window._manager.batches[0])}"
    window._manager.emit("progress-bytes", 5, 10, key)
    assert first_batch._latest_fraction == 0.5
    assert second_batch._latest_fraction is None
    assert copy_dialog._latest_fraction is None

    # The copy finishing completes the copy dialog, not a batch dialog.
    for future in copies:
        future.set_result(None)
    assert copy_dialog.completions == [(True, None)]
    assert copy_dialog.files_completed == 2
    assert first_batch.completions == [] and second_batch.completions == []

    for batch in window._manager.batches:
        batch.set_result(TransferBatchResult(items_total=1, items_done=1))
    assert first_batch.completions == [(True, None)]
    assert second_batch.completions == [(True, None)]


def test_cleanup_closes_every_open_dialog(load_file_manager_window, monkeypatch):
    window_module = load_file_manager_window()
    window = _window(window_module, monkeypatch)
    window._show_progress_dialog("copy", "big", Future(), total_files=1)
    window._start_transfer_batch("upload", [("/l/a", "/r/a", False)])
    window._start_transfer_batch("download", [("/l/b", "/r/b", False)])

    window._clear_progress_toast()

    assert all(dialog.closed for dialog in window.created)
    assert window._transfer_dialogs == [] and window._aggregate_dialog is None


class _BackgroundDialog(_Dialog):
    """As the real dialog: closing a running transfer's details only hides
    them, and the dialog knows its own futures."""

    def __init__(self, operation_type, total_files):
        super().__init__(operation_type, total_files)
        self._futures = []

    def set_future(self, future):
        if future not in self._futures:
            self._futures.append(future)

    def close(self):
        self.closed = True

    def is_reusable(self):
        return not (self.is_cancelled or self.completions)

    def mark_cancelled(self):
        self.is_cancelled = True


def _background_window(window_module, monkeypatch):
    window = _window(window_module, monkeypatch)

    def _present(operation_type, *, total_files, filename, **_panes):
        dialog = _BackgroundDialog(operation_type, total_files)
        window.created.append(dialog)
        return dialog

    window._present_progress_dialog = _present
    return window


def test_replaced_transfer_still_reports_success(load_file_manager_window, monkeypatch):
    """A copy still running when another kind of operation takes the dialog
    slot must finish in its own dialog, not show as running for good."""
    window_module = load_file_manager_window()
    window = _background_window(window_module, monkeypatch)
    copies = [Future(), Future()]
    for future in copies:
        window._show_progress_dialog("copy", "big", future, total_files=2)
    move = Future()
    window._show_progress_dialog("move", "other", move, total_files=1)
    copy_dialog, move_dialog = window.created

    copies[0].set_result(None)
    assert copy_dialog.completions == []
    copies[1].set_result(None)

    assert copy_dialog.completions == [(True, None)]
    assert copy_dialog.files_completed == 2
    assert move_dialog.completions == [] and move_dialog.files_completed == 0

    move.set_result(None)
    assert move_dialog.completions == [(True, None)]


def test_replaced_transfer_still_reports_failure(load_file_manager_window, monkeypatch):
    window_module = load_file_manager_window()
    window = _background_window(window_module, monkeypatch)
    copies = [Future(), Future()]
    for future in copies:
        window._show_progress_dialog("copy", "big", future, total_files=2)
    window._show_progress_dialog("move", "other", Future(), total_files=1)
    copy_dialog, move_dialog = window.created

    copies[0].set_result(None)
    copies[1].set_exception(OSError("disk full"))

    assert len(copy_dialog.completions) == 1
    success, message = copy_dialog.completions[0]
    assert success is False and "1 of 2" in message
    assert move_dialog.completions == []


def test_replaced_transfer_on_another_server_reports_too(load_file_manager_window, monkeypatch):
    """The same kind of operation on a second server also gets its own dialog."""
    window_module = load_file_manager_window()
    window = _background_window(window_module, monkeypatch)
    first = Future()
    window._show_progress_dialog("copy", "a", first, total_files=1)
    other = _Manager()
    second = Future()
    window._show_progress_dialog("copy", "b", second, total_files=1, manager=other)
    first_dialog, second_dialog = window.created

    # The first transfer keeps listening to its own server for its own
    # dialog (its progress must not freeze), until it ends.
    assert window._manager.handlers != {}

    first.set_exception(OSError("gone"))
    assert first_dialog.completions == [(False, "gone")]
    assert second_dialog.completions == []
    assert window._manager.handlers == {}


def test_replaced_transfer_cancel_is_not_a_failure(load_file_manager_window, monkeypatch):
    window_module = load_file_manager_window()
    window = _background_window(window_module, monkeypatch)
    copy = Future()
    window._show_progress_dialog("copy", "big", copy, total_files=1)
    window._show_progress_dialog("move", "other", Future(), total_files=1)
    copy_dialog = window.created[0]

    copy.set_exception(window_module.TransferCancelledException("cancelled"))

    assert copy_dialog.is_cancelled and copy_dialog.completions == []


def test_replaced_transfer_keeps_its_own_progress(load_file_manager_window, monkeypatch):
    """A copy replaced by a newer run still moves its own bar, and its
    progress never reaches the newer dialog."""
    window_module = load_file_manager_window()
    window = _background_window(window_module, monkeypatch)
    copy = Future()
    window._show_progress_dialog("copy", "big", copy, total_files=2, expected_bytes=100)
    second = Future()
    window._show_progress_dialog("copy", "small", second, total_files=2, expected_bytes=100)
    move = Future()
    window._show_progress_dialog("move", "other", move, total_files=1)
    copy_dialog, move_dialog = window.created

    window._manager.emit("progress-bytes", 50, 100, f"fut-{id(copy)}")

    assert copy_dialog._latest_fraction is not None and copy_dialog._latest_fraction > 0
    assert move_dialog._latest_fraction is None


def test_success_waits_for_every_file_to_be_counted(load_file_manager_window, monkeypatch):
    """Both futures finish before the main loop runs: the run must not call
    the first one a success of the whole copy and then drop the failure."""
    window_module = load_file_manager_window()
    queued = []
    window = _background_window(window_module, monkeypatch)
    monkeypatch.setattr(
        window_module.GLib, "idle_add",
        lambda fn, *args, **kwargs: queued.append((fn, args)) or 1, raising=False,
    )
    copies = [Future(), Future()]
    for future in copies:
        window._show_progress_dialog("copy", "big", future, total_files=2)
    copy_dialog = window.created[0]

    copies[0].set_result(None)
    copies[1].set_exception(OSError("disk full"))
    for fn, args in list(queued):
        fn(*args)

    assert len(copy_dialog.completions) == 1
    success, message = copy_dialog.completions[0]
    assert success is False and "1 of 2" in message
    assert copy_dialog.files_completed == 1
