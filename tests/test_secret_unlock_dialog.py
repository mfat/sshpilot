"""prompt_unlock's owned-vs-rode return contract.

The connect flow relies on this: a call that *shows* the dialog (or needs no unlock)
returns True ("we own this interaction"); a call that merely *rides* an already-open
prompt returns False, so the caller won't silently proceed when a ridden prompt (e.g. a
deferred startup unlock) resolves still-locked.
"""

import pytest

import sshpilot.secret_storage as ss
from sshpilot import secret_unlock_dialog as d


def test_prompt_unlock_returns_true_when_no_unlock_needed(monkeypatch):
    mgr = ss.get_secret_manager()
    monkeypatch.setattr(mgr, 'selected_needs_unlock', lambda: False)
    got = []
    assert d.prompt_unlock(None, on_done=got.append) is True   # owns / no-op
    assert got == [True]


def test_should_finish_cancel_is_signal_order_independent():
    f = d._should_finish_cancel
    # Outcome unknown (None) — happens when the dialog's 'closed' fires BEFORE 'response'
    # (e.g. pressing Enter). Must NOT finish, or the unlock is aborted and the connection
    # starts while the worker is still unlocking (the reported bug).
    assert f(None, True, True) is False
    assert f(None, True, False) is False
    # 'unlock' is owned by the spinner/worker path — never finish-cancel here.
    assert f('unlock', True, True) is False
    assert f('unlock', True, False) is False
    # A real cancel finishes once the dialog is gone (closed fired)...
    assert f('cancel', True, True) is True
    # ...but waits while it's still closing (closed not yet fired)...
    assert f('cancel', True, False) is False
    # ...and on the legacy path (no 'closed' signal) finishes immediately.
    assert f('cancel', False, False) is True


def test_prompt_unlock_returns_false_when_riding(monkeypatch):
    mgr = ss.get_secret_manager()
    monkeypatch.setattr(mgr, 'selected_needs_unlock', lambda: True)
    monkeypatch.setattr(d, '_unlock_in_progress', True)   # a prompt is already open
    d._pending_callbacks.clear()
    try:
        assert d.prompt_unlock(None, on_done=lambda _s: None) is False   # rode
        assert len(d._pending_callbacks) == 1                            # callback queued
    finally:
        d._pending_callbacks.clear()


@pytest.mark.parametrize("name, session_backed", [
    ("bitwarden", True),   # session backend
    ("rbw", False),        # passive backend — same check now covers it
    ("pass", False),
])
def test_unlock_at_startup_prompts_when_backend_unavailable(monkeypatch, name, session_backed):
    class FakeBackend:
        def __init__(self):
            self.name = name
            self.session_backed = session_backed

        def is_available(self):
            return False

    class FakeManager:
        def set_selected(self, _name):
            pass

        def selected_backend(self):
            return FakeBackend()

        def selected_needs_unlock(self):
            return False

    prompted = []
    monkeypatch.setattr(d, "get_secret_manager", lambda: FakeManager())
    monkeypatch.setattr(
        d, "_prompt_unavailable_backend",
        lambda parent, backend: prompted.append(backend.name),
    )
    monkeypatch.setattr(d, "prompt_unlock", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("prompt_unlock should not run when backend is unavailable")
    ))

    assert d.unlock_at_startup(None) is False
    assert prompted == [name]   # every selected+unavailable backend is surfaced


def test_unlock_at_startup_noop_for_available_passive_backend(monkeypatch):
    # An available passive backend (rbw installed) has no unlock lifecycle -> no prompt.
    class FakeBackend:
        name = "rbw"
        session_backed = False

        def is_available(self):
            return True

    class FakeManager:
        def set_selected(self, _name):
            pass

        def selected_backend(self):
            return FakeBackend()

        def selected_needs_unlock(self):
            raise AssertionError("passive backend must not reach the unlock check")

    monkeypatch.setattr(d, "get_secret_manager", lambda: FakeManager())
    monkeypatch.setattr(d, "_prompt_unavailable_backend", lambda *_a: (_ for _ in ()).throw(
        AssertionError("available backend must not prompt unavailable")
    ))
    assert d.unlock_at_startup(None) is False


def test_startup_unlock_prompts_when_signed_in_but_locked(monkeypatch):
    class FakeManager:
        def selected_backend(self):
            return object()

        def selected_needs_unlock(self):
            return True

    unlocked = []
    monkeypatch.setattr(d, "get_secret_manager", lambda: FakeManager())
    monkeypatch.setattr(d, "prompt_unlock", lambda parent: unlocked.append(parent))

    d._startup_unlock_after_probe("win", needs_login=False)
    assert unlocked == ["win"]


def test_startup_unlock_notifies_but_does_not_unlock_when_not_signed_in(monkeypatch):
    # An unauthenticated vault must get a sign-in notice, never a doomed unlock prompt.
    backend = object()

    class FakeManager:
        def selected_backend(self):
            return backend

    notified = []
    monkeypatch.setattr(d, "get_secret_manager", lambda: FakeManager())
    monkeypatch.setattr(d, "_prompt_not_signed_in", lambda parent, b: notified.append(b))
    monkeypatch.setattr(d, "prompt_unlock", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("must not prompt for unlock when not signed in")
    ))

    d._startup_unlock_after_probe("win", needs_login=True)
    assert notified == [backend]

