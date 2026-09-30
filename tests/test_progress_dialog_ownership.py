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
