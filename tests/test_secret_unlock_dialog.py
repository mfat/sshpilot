"""prompt_unlock's daemon-backed contract.

The GTK unlock flow no longer owns secret backends: it drives a daemon-owned
``SecretBackendsController`` on a worker and reports through ``on_done`` on the GLib
main loop. These tests exercise the daemon path with a fake controller — no
``secret_storage``, no ``SecretManager``, no master-password collection.

Owned-vs-rode return contract (kept for the connect flow): a call that *starts* the
unlock (or needs no unlock) returns True; a call that merely *rides* an already-open
prompt returns False, so the caller won't silently proceed when a ridden prompt (e.g.
a deferred startup unlock) resolves still-locked.
"""

import pytest

from sshpilot.api.models.secrets import SecretMessageCode, UnlockResultKind
from sshpilot import secret_unlock_dialog as d


@pytest.fixture(autouse=True)
def _reset_unlock_state():
    """Each test starts and ends with no in-flight prompt or queued callbacks."""
    d._unlock_in_progress = False
    d._pending_callbacks.clear()
    yield
    d._unlock_in_progress = False
    d._pending_callbacks.clear()


class _FakeState:
    def __init__(self, *, needs_unlock, login_required=False, selected_backend="bitwarden"):
        self.needs_unlock = needs_unlock
        self.locked = needs_unlock
        self.login_required = login_required
        self.selected_backend = selected_backend


class _FakeController:
    def __init__(self, state=None, result=None, results=None):
        self.state = state
        self.result = result
        # ``results`` scripts one result per unlock() call, so a retry can answer
        # differently from the first attempt.
        self.results = list(results) if results is not None else None
        self.unlock_calls = []
        self.lock_calls = []

    def load_state(self):
        return self.state

    def unlock(self):
        self.unlock_calls.append(True)
        if self.results is not None:
            return self.results.pop(0) if self.results else self.result
        return self.result

    def lock(self):
        self.lock_calls.append(True)
        return self.state


class _FakeUnlockResult:
    def __init__(self, kind, *, backend="bitwarden", message_code=None):
        self.kind = kind
        self.backend = backend
        self.message_code = message_code


class _FakeParent:
    def __init__(self, controller):
        self.secrets_controller = controller


class _FakeSpinner:
    """Mimics the spinner dialog: 'closed' fires when close() is called.

    Like the real dialog it emits 'closed' once — the callbacks are dropped as they
    run, so a retry that opens a second spinner cannot re-fire the first one's."""

    def __init__(self):
        self.callbacks = []

    def connect(self, signal, callback):
        self.callbacks.append(callback)

    def close(self):
        callbacks, self.callbacks = self.callbacks, []
        for cb in callbacks:
            cb()


def _install_unlock_harness(monkeypatch):
    """Run the unlock worker and its GLib sequencing synchronously.

    Each call gets its own spinner, as the real one does, so a retry's spinner is
    independent of the attempt before it. Returns the list of spinners created."""
    spinners = []

    def _fake_spinner_dialog(parent, heading, body):
        spinner = _FakeSpinner()
        spinners.append(spinner)
        return (lambda _text: None, spinner.close, spinner)

    monkeypatch.setattr(d, "_spinner_dialog", _fake_spinner_dialog)
    monkeypatch.setattr(
        d.GLib, "idle_add",
        lambda callback, *args: (callback(*args), False)[1], raising=False,
    )

    class SyncThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None, name=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}
            self.daemon = daemon

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(d.threading, "Thread", SyncThread)
    return spinners


def test_prompt_unlock_no_controller_reports_false():
    calls = []
    owned = d.prompt_unlock(None, on_done=lambda ok: calls.append(ok))
    assert owned is True            # owns / no-op — GTK must not fall back locally
    assert calls == [False]


def test_prompt_unlock_daemon_path_unlocks_when_needed(monkeypatch):
    controller = _FakeController(
        state=_FakeState(needs_unlock=True),
        result=_FakeUnlockResult(UnlockResultKind.UNLOCKED),
    )
    _install_unlock_harness(monkeypatch)
    calls = []
    owned = d.prompt_unlock(_FakeParent(controller), on_done=lambda ok: calls.append(ok))

    assert owned is True
    assert controller.unlock_calls == [True]   # daemon unlock driven from a worker
    assert controller.lock_calls == []         # no stale session to drop
    assert calls == [True]


def test_prompt_unlock_reports_success_when_no_unlock_needed(monkeypatch):
    controller = _FakeController(state=_FakeState(needs_unlock=False))
    _install_unlock_harness(monkeypatch)
    calls = []
    owned = d.prompt_unlock(_FakeParent(controller), on_done=lambda ok: calls.append(ok))

    assert owned is True
    assert controller.unlock_calls == []       # daemon said no unlock is needed
    assert calls == [True]


def test_prompt_unlock_rides_in_flight_prompt():
    controller = _FakeController(
        state=_FakeState(needs_unlock=True),
        result=_FakeUnlockResult(UnlockResultKind.UNLOCKED),
    )
    d._unlock_in_progress = True               # a prompt is already open
    d._pending_callbacks.clear()
    try:
        calls = []
        assert d.prompt_unlock(_FakeParent(controller), on_done=lambda ok: calls.append(ok)) is False
        assert len(d._pending_callbacks) == 1  # callback queued on the in-flight unlock
    finally:
        d._unlock_in_progress = False
        d._pending_callbacks.clear()


def test_prompt_unlock_cancelled_interaction_reports_failure(monkeypatch):
    controller = _FakeController(
        state=_FakeState(needs_unlock=True),
        result=_FakeUnlockResult(UnlockResultKind.INTERACTION_REQUIRED),
    )
    _install_unlock_harness(monkeypatch)
    calls = []
    owned = d.prompt_unlock(_FakeParent(controller), on_done=lambda ok: calls.append(ok))

    assert owned is True
    assert controller.unlock_calls == [True]
    assert calls == [False]                    # user cancelled the daemon prompt


def test_prompt_unlock_rejected_master_password_is_not_reported_as_unavailable(monkeypatch):
    """A wrong master password must not claim the vault is missing (issue #1245).

    The daemon answers both with ``backend_unavailable``; only the
    ``vault_unlock_failed`` message code separates a rejected password from a
    backend that cannot run at all."""
    controller = _FakeController(
        state=_FakeState(needs_unlock=True, selected_backend="keepassxc"),
        result=_FakeUnlockResult(
            UnlockResultKind.BACKEND_UNAVAILABLE,
            backend="keepassxc",
            message_code=SecretMessageCode.VAULT_UNLOCK_FAILED,
        ),
    )
    _install_unlock_harness(monkeypatch)
    wrong_password = []
    unavailable = []
    monkeypatch.setattr(d, "_prompt_wrong_master_password",
                        lambda parent, backend, on_response: (
                            wrong_password.append(backend), on_response(False))[1])
    monkeypatch.setattr(d, "_prompt_unavailable_backend",
                        lambda parent, backend: unavailable.append(backend))
    calls = []

    owned = d.prompt_unlock(_FakeParent(controller), on_done=lambda ok: calls.append(ok))

    assert owned is True
    assert unavailable == []
    assert wrong_password == ["keepassxc"]     # named by the daemon, not a generic "vault"
    assert calls == [False]


def test_prompt_unlock_retries_after_a_rejected_master_password(monkeypatch):
    """“Try again” re-runs the unlock on the same in-flight interaction: the caller
    hears one outcome, once, after the second attempt succeeds."""
    controller = _FakeController(
        state=_FakeState(needs_unlock=True, selected_backend="keepassxc"),
        results=[
            _FakeUnlockResult(
                UnlockResultKind.BACKEND_UNAVAILABLE,
                backend="keepassxc",
                message_code=SecretMessageCode.VAULT_UNLOCK_FAILED,
            ),
            _FakeUnlockResult(UnlockResultKind.UNLOCKED, backend="keepassxc"),
        ],
    )
    _install_unlock_harness(monkeypatch)
    answers = iter([True])          # retry once, then the vault opens
    monkeypatch.setattr(d, "_prompt_wrong_master_password",
                        lambda parent, backend, on_response: on_response(next(answers)))
    calls = []

    owned = d.prompt_unlock(_FakeParent(controller), on_done=lambda ok: calls.append(ok))

    assert owned is True
    assert controller.unlock_calls == [True, True]   # the retry really re-unlocks
    assert calls == [True]                           # resolved once, after the retry
    assert d._unlock_in_progress is False


def test_prompt_unlock_keeps_the_interaction_open_while_the_retry_is_offered(monkeypatch):
    """The caller must not be told the unlock failed while the user is still
    deciding — otherwise the connect flow opens its terminal behind the dialog."""
    controller = _FakeController(
        state=_FakeState(needs_unlock=True, selected_backend="keepassxc"),
        result=_FakeUnlockResult(
            UnlockResultKind.BACKEND_UNAVAILABLE,
            backend="keepassxc",
            message_code=SecretMessageCode.VAULT_UNLOCK_FAILED,
        ),
    )
    _install_unlock_harness(monkeypatch)
    calls = []
    pending = []
    monkeypatch.setattr(d, "_prompt_wrong_master_password",
                        lambda parent, backend, on_response: pending.append(on_response))

    d.prompt_unlock(_FakeParent(controller), on_done=lambda ok: calls.append(ok))

    assert calls == []                  # still undecided — nothing reported yet
    assert d._unlock_in_progress is True
    pending[0](False)                   # "Not now"
    assert calls == [False]
    assert d._unlock_in_progress is False


def test_prompt_unlock_unavailable_backend_keeps_the_unavailable_notice(monkeypatch):
    controller = _FakeController(
        state=_FakeState(needs_unlock=True, selected_backend="keepassxc"),
        result=_FakeUnlockResult(
            UnlockResultKind.BACKEND_UNAVAILABLE,
            backend="keepassxc",
            message_code=SecretMessageCode.SECRET_BACKEND_UNAVAILABLE,
        ),
    )
    _install_unlock_harness(monkeypatch)
    wrong_password = []
    unavailable = []
    monkeypatch.setattr(d, "_prompt_wrong_master_password",
                        lambda parent, backend: wrong_password.append(backend))
    monkeypatch.setattr(d, "_prompt_unavailable_backend",
                        lambda parent, backend: unavailable.append(backend))
    calls = []

    owned = d.prompt_unlock(_FakeParent(controller), on_done=lambda ok: calls.append(ok))

    assert owned is True
    assert wrong_password == []
    assert unavailable == ["keepassxc"]
    assert calls == [False]


def test_unlock_result_outcome_separates_a_rejected_password():
    rejected = _FakeUnlockResult(
        UnlockResultKind.BACKEND_UNAVAILABLE,
        backend="keepassxc",
        message_code=SecretMessageCode.VAULT_UNLOCK_FAILED,
    )
    assert d._unlock_result_outcome(rejected) == (False, "wrong_password", "keepassxc")

    missing = _FakeUnlockResult(
        UnlockResultKind.BACKEND_UNAVAILABLE,
        backend="rbw",
        message_code=SecretMessageCode.SECRET_BACKEND_UNAVAILABLE,
    )
    assert d._unlock_result_outcome(missing) == (False, "unavailable", "rbw")

    unlocked = _FakeUnlockResult(UnlockResultKind.UNLOCKED, backend="keepassxc")
    assert d._unlock_result_outcome(unlocked) == (True, None, "keepassxc")


def test_friendly_backend_name_falls_back_for_the_none_placeholder():
    # The daemon calls an absent backend "none"; that is not a name to show.
    assert d._friendly_backend_name("none") == d._friendly_backend_name("")


def test_prompt_unlock_already_unlocked_backend_object_reports_success():
    # Explicit backend shim (backup destination / setup): duck-typed state only,
    # no daemon round-trip, no secret_storage.
    class FakeBackend:
        name = "bitwarden"
        session_backed = True

        def is_available(self):
            return True

        def is_unlocked(self):
            return True

    calls = []
    owned = d.prompt_unlock(None, backend=FakeBackend(), on_done=lambda ok: calls.append(ok))
    assert owned is True
    assert calls == [True]


def test_prompt_unlock_backend_object_needing_unlock_uses_daemon(monkeypatch):
    class FakeBackend:
        name = "bitwarden"
        session_backed = True

        def is_available(self):
            return True

        def is_unlocked(self):
            return False

    controller = _FakeController(
        state=_FakeState(needs_unlock=True),
        result=_FakeUnlockResult(UnlockResultKind.UNLOCKED),
    )
    _install_unlock_harness(monkeypatch)
    calls = []
    owned = d.prompt_unlock(
        _FakeParent(controller), backend=FakeBackend(), on_done=lambda ok: calls.append(ok)
    )

    assert owned is True
    assert controller.unlock_calls == [True]   # the daemon owns the actual unlock
    assert calls == [True]


def test_unlock_at_startup_noop_without_controller(monkeypatch):
    prompted = []
    monkeypatch.setattr(d, "prompt_unlock", lambda win: prompted.append(win))
    assert d.unlock_at_startup(object()) is False   # no secrets_controller
    assert prompted == []


def test_unlock_at_startup_unlocks_when_needed(monkeypatch):
    _install_unlock_harness(monkeypatch)
    controller = _FakeController(state=_FakeState(needs_unlock=True))
    window = _FakeParent(controller)
    prompted = []
    monkeypatch.setattr(d, "prompt_unlock", lambda win: prompted.append(win))

    assert d.unlock_at_startup(window) is False
    assert prompted == [window]


def test_unlock_at_startup_prompts_when_rbw_needs_unlock(monkeypatch):
    _install_unlock_harness(monkeypatch)
    controller = _FakeController(
        state=_FakeState(needs_unlock=True, selected_backend="rbw")
    )
    window = _FakeParent(controller)
    prompted = []
    monkeypatch.setattr(d, "prompt_unlock", lambda win: prompted.append(win))

    assert d.unlock_at_startup(window) is False
    assert prompted == [window]


def test_unlock_at_startup_rides_out_a_busy_controller(monkeypatch):
    """Startup runs this check alongside the startup diagnostics' own controller
    reads. The controller rejects overlapping guarded operations, so giving up on
    the first "already in progress" made the master-password prompt appear only
    on the runs that won that race."""
    _install_unlock_harness(monkeypatch)
    monkeypatch.setattr(d.time, "sleep", lambda _seconds: None)

    controller = _FakeController(state=_FakeState(needs_unlock=True))
    attempts = []

    def load_state():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("a secret backend operation is already in progress")
        return controller.state

    controller.load_state = load_state
    window = _FakeParent(controller)
    prompted = []
    monkeypatch.setattr(d, "prompt_unlock", lambda win: prompted.append(win))

    assert d.unlock_at_startup(window) is False
    assert len(attempts) == 3
    assert prompted == [window]


def test_unlock_at_startup_gives_up_after_bounded_retries(monkeypatch):
    """A controller that never frees up must not retry forever (or prompt on
    state it could not read)."""
    _install_unlock_harness(monkeypatch)
    monkeypatch.setattr(d.time, "sleep", lambda _seconds: None)

    controller = _FakeController(state=_FakeState(needs_unlock=True))
    attempts = []

    def load_state():
        attempts.append(1)
        raise RuntimeError("a secret backend operation is already in progress")

    controller.load_state = load_state
    window = _FakeParent(controller)
    prompted = []
    monkeypatch.setattr(d, "prompt_unlock", lambda win: prompted.append(win))

    assert d.unlock_at_startup(window) is False
    assert len(attempts) == d._BUSY_RETRY_ATTEMPTS
    assert prompted == []


def test_unlock_at_startup_reads_state_off_the_main_thread(monkeypatch):
    """The daemon reads are blocking RPCs (a locked KeePassXC database alone
    costs ~0.6s), so they must not run on the GTK main loop."""
    started = []

    class _RecordingThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon

        def start(self):
            started.append(self.daemon)

    monkeypatch.setattr(d.threading, "Thread", _RecordingThread)
    controller = _FakeController(state=_FakeState(needs_unlock=True))
    calls = []
    controller.load_state = lambda: calls.append(1)

    assert d.unlock_at_startup(_FakeParent(controller)) is False
    assert started == [True]   # worker started, daemon thread
    assert calls == []         # nothing read on the caller's (main) thread


def test_unlock_at_startup_noop_for_available_passive_backend(monkeypatch):
    _install_unlock_harness(monkeypatch)
    class Descriptor:
        name = "rbw"
        selected = True
        available = True

    class FakeRegistry:
        backends = (Descriptor(),)

    controller = _FakeController(state=_FakeState(needs_unlock=False, selected_backend="rbw"))
    controller.load_registry = lambda: FakeRegistry()
    window = _FakeParent(controller)

    prompted = []
    notified = []
    monkeypatch.setattr(d, "prompt_unlock", lambda win: prompted.append(win))
    monkeypatch.setattr(d, "_prompt_unavailable_backend", lambda win, b: notified.append(b))

    assert d.unlock_at_startup(window) is False
    assert prompted == []
    assert notified == []


def test_unlock_at_startup_notices_unavailable_selected_backend(monkeypatch):
    _install_unlock_harness(monkeypatch)

    class Descriptor:
        name = "bitwarden"
        selected = True
        available = False

    class FakeRegistry:
        backends = (Descriptor(),)

    controller = _FakeController(state=_FakeState(needs_unlock=False, selected_backend="bitwarden"))
    controller.load_registry = lambda: FakeRegistry()
    window = _FakeParent(controller)

    prompted = []
    notified = []
    monkeypatch.setattr(d, "prompt_unlock", lambda win: prompted.append(win))
    monkeypatch.setattr(d, "_prompt_unavailable_backend", lambda win, b: notified.append(b))

    assert d.unlock_at_startup(window) is False
    assert prompted == []
    assert [b.name for b in notified] == ["bitwarden"]


def test_unlock_at_startup_notices_when_not_signed_in(monkeypatch):
    _install_unlock_harness(monkeypatch)
    controller = _FakeController(
        state=_FakeState(needs_unlock=True, login_required=True, selected_backend="bitwarden")
    )
    window = _FakeParent(controller)

    notified = []
    monkeypatch.setattr(d, "_prompt_not_signed_in", lambda win, b: notified.append((win, b)))
    monkeypatch.setattr(d, "prompt_unlock", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("must not prompt for unlock when not signed in")
    ))

    assert d.unlock_at_startup(window) is False
    assert notified == [(window, "bitwarden")]


def test_secret_unlock_dialog_has_no_secret_storage_import():
    """GTK must never import secret_storage (the daemon owns the backends)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(d))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any("secret_storage" in (name or "") for name in names), (
            f"secret_unlock_dialog.py:{node.lineno} imports secret_storage"
        )
