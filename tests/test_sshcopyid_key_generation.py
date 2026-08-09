"""ssh-copy-id key generation runs off the GTK thread.

Generation guards: one in-flight mutation, controls disabled while running,
no passphrase in the worker request, completion callbacks ignored after close,
no sleep or local file-existence checks, and mutation ambiguity reloads without
an automatic retry.
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("gi")

from sshpilot import sshcopyid_window as win_mod
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.key_manager import SSHKey

_SOURCE = (
    Path(__file__).resolve().parents[1] / "src" / "sshpilot" / "sshcopyid_window.py"
)


class _ThreadSpy:
    instances: list = []

    def __init__(self, target=None, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon
        _ThreadSpy.instances.append(self)

    def start(self):
        pass


@pytest.fixture(autouse=True)
def _reset_spy():
    _ThreadSpy.instances = []
    yield


@pytest.fixture
def idle(monkeypatch):
    callbacks = []
    monkeypatch.setattr(win_mod.GLib, "idle_add", lambda fn, *args: callbacks.append((fn, args)))
    return callbacks


def _make_window():
    window = win_mod.SshCopyIdWindow.__new__(win_mod.SshCopyIdWindow)
    window._closed = False
    window._loading_keys = False
    window._generating = False
    window._km = MagicMock()
    window._parent = MagicMock()
    window._conn = object()
    window._cm = None
    window._existing_keys_cache = []
    window._last_real_selection = 0
    window.dropdown_existing = MagicMock()
    window.radio_existing = MagicMock()
    window.radio_existing.get_active.return_value = True
    window.radio_generate = MagicMock()
    window.radio_generate.get_active.return_value = False
    window.btn_ok = MagicMock()
    window.row_key_name = MagicMock()
    window.type_dropdown = MagicMock()
    window.row_pass_toggle = MagicMock()
    window.pass1 = MagicMock()
    window.pass2 = MagicMock()
    window.pass_box = MagicMock()
    window.force_toggle = MagicMock()
    window._server_label = MagicMock()
    window._server_row = MagicMock()
    window._error = MagicMock()
    window._info = MagicMock()
    window.close = MagicMock()
    window._reload_existing_keys = MagicMock()
    return window


def _set_generate_form(window, name="id_rsa", type_index=1, passphrase=False):
    window.row_key_name.get_text.return_value = name
    window.type_dropdown.get_selected.return_value = type_index
    window.row_pass_toggle.get_active.return_value = passphrase
    window.radio_generate.get_active.return_value = True
    window.radio_existing.get_active.return_value = False
    window.force_toggle.get_active.return_value = True


# ---------------------------------------------------------------------------
# Worker start / guards
# ---------------------------------------------------------------------------
def test_generate_disables_controls_and_starts_one_worker(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)

    window._do_generate_and_copy()

    assert len(_ThreadSpy.instances) == 1
    thread = _ThreadSpy.instances[0]
    assert thread.target.__self__ is window
    assert thread.target.__func__.__name__ == "_generate_worker"
    assert thread.daemon is True
    window.btn_ok.set_sensitive.assert_any_call(False)
    window.row_key_name.set_sensitive.assert_any_call(False)
    window.type_dropdown.set_sensitive.assert_any_call(False)
    window.force_toggle.set_sensitive.assert_any_call(False)


def test_repeated_generate_click_starts_one_mutation(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)

    window._do_generate_and_copy()
    window._do_generate_and_copy()

    assert len(_ThreadSpy.instances) == 1


def test_invalid_name_never_starts_worker(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window, name="../evil")

    window._do_generate_and_copy()

    assert _ThreadSpy.instances == []
    window._error.assert_called_once()


def test_encrypted_generation_request_is_secret_free(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window, passphrase=True)
    window.pass1.get_text.return_value = "s3cret"
    window.pass2.get_text.return_value = "s3cret"

    window._do_generate_and_copy()

    thread = _ThreadSpy.instances[0]
    request = thread.args[3]
    assert request["encrypted"] is True
    assert request["interaction_scope_id"].startswith("key-operation-")
    assert "passphrase" not in request
    window.pass1.get_text.assert_not_called()
    window.pass2.get_text.assert_not_called()
    assert "s3cret" not in repr(window)

    # After the worker completes, the request is not retained on the window.
    window._km.generate_key.return_value = SSHKey("/daemon/keys/k", name="k")
    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)
    assert "s3cret" not in repr(window)


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------
def test_generate_success_runs_runner_with_returned_key(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)
    window._do_generate_and_copy()
    thread = _ThreadSpy.instances[0]

    new_key = SSHKey("/daemon/keys/k", name="k", public_path="/daemon/keys/k.pub")
    window._km.generate_key.return_value = new_key
    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    # The daemon result is authoritative and handed to the M7 ssh-copy-id runner.
    window._parent._show_ssh_copy_id_terminal_using_main_widget.assert_called_once_with(
        window._conn, new_key, True
    )
    window.close.assert_called_once()
    # Controls restored.
    window.btn_ok.set_sensitive.assert_any_call(True)


def test_generate_failure_restores_controls_and_shows_error(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)
    window._do_generate_and_copy()
    thread = _ThreadSpy.instances[0]

    window._km.generate_key.side_effect = ValueError("invalid key type")
    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    window.btn_ok.set_sensitive.assert_any_call(True)
    window._error.assert_called_once()
    window._parent._show_ssh_copy_id_terminal_using_main_widget.assert_not_called()
    window.close.assert_not_called()


def test_generate_duplicate_shows_existing_name_message(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)
    window._do_generate_and_copy()
    thread = _ThreadSpy.instances[0]

    window._km.generate_key.side_effect = FileExistsError("a key with that name exists")
    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    body = " ".join(str(part) for part in window._error.call_args[0])
    assert "already exists" in body


def test_mutation_ambiguity_reloads_without_retry(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)
    window._do_generate_and_copy()
    thread = _ThreadSpy.instances[0]

    window._km.generate_key.side_effect = SshPilotError(
        ErrorCode.MUTATION_AMBIGUOUS, "the SSH key generation may have completed"
    )
    thread.target(*thread.args)
    fn, args = idle[0]
    fn(*args)

    window._info.assert_called_once()
    window._reload_existing_keys.assert_called_once()
    # Exactly one mutation attempt — never an automatic retry.
    window._km.generate_key.assert_called_once()
    window._parent._show_ssh_copy_id_terminal_using_main_widget.assert_not_called()


def test_close_during_successful_generation_no_runner_no_close(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)
    window._do_generate_and_copy()
    thread = _ThreadSpy.instances[0]

    new_key = SSHKey("/daemon/keys/k", name="k")
    window._km.generate_key.return_value = new_key
    thread.target(*thread.args)
    window._closed = True
    fn, args = idle[0]
    fn(*args)

    window._parent._show_ssh_copy_id_terminal_using_main_widget.assert_not_called()
    window.close.assert_not_called()
    window._error.assert_not_called()
    assert window._generating is False


def test_generation_completion_after_close_does_not_update_controls(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)
    window._do_generate_and_copy()
    thread = _ThreadSpy.instances[0]
    window._set_generating_state = MagicMock()

    new_key = SSHKey("/daemon/keys/k", name="k")
    window._km.generate_key.return_value = new_key
    thread.target(*thread.args)
    window._closed = True
    fn, args = idle[0]
    fn(*args)

    window._set_generating_state.assert_not_called()
    window._parent._show_ssh_copy_id_terminal_using_main_widget.assert_not_called()
    window.close.assert_not_called()


def test_generation_failure_after_close_shows_no_dialog(monkeypatch, idle):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)
    window._do_generate_and_copy()
    thread = _ThreadSpy.instances[0]
    window._set_generating_state = MagicMock()

    window._km.generate_key.side_effect = RuntimeError("boom")
    thread.target(*thread.args)
    window._closed = True
    fn, args = idle[0]
    fn(*args)

    window._set_generating_state.assert_not_called()
    window._error.assert_not_called()
    assert window._generating is False


def test_ok_click_while_generating_starts_no_second_mutation(monkeypatch):
    monkeypatch.setattr(win_mod.threading, "Thread", _ThreadSpy)
    window = _make_window()
    _set_generate_form(window)
    window._generating = True

    window._on_ok_clicked()

    assert _ThreadSpy.instances == []
    window._km.generate_key.assert_not_called()


# ---------------------------------------------------------------------------
# Static checks: no sleep / no local generated-file checks
# ---------------------------------------------------------------------------
_GENERATION_FUNCS = frozenset(
    {
        "_do_generate_and_copy",
        "_generate_worker",
        "_on_key_generated",
        "_on_key_generation_failed",
        "_on_key_mutation_ambiguous",
    }
)


def _function_ids(func) -> set:
    ids = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Name):
            ids.add(node.id)
        elif isinstance(node, ast.Attribute):
            ids.add(node.attr)
    return ids


def test_generation_flow_has_no_sleep_or_local_file_checks():
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in _GENERATION_FUNCS:
            continue
        ids = _function_ids(node)
        assert "sleep" not in ids, f"{node.name} sleeps"
        assert "time" not in ids, f"{node.name} imports/uses time"
        assert not ({"exists", "isfile"} & ids), (
            f"{node.name} performs local generated-file checks"
        )


def test_daemon_result_is_authoritative_no_local_pub_read():
    """The generation path never reads the generated .pub file locally."""
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in _GENERATION_FUNCS:
            continue
        ids = _function_ids(node)
        assert "open" not in ids, f"{node.name} opens a file"
        assert "read_text" not in ids, f"{node.name} reads a file"
        assert "read_bytes" not in ids, f"{node.name} reads a file"
