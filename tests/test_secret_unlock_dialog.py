"""prompt_unlock's owned-vs-rode return contract.

The connect flow relies on this: a call that *shows* the dialog (or needs no unlock)
returns True ("we own this interaction"); a call that merely *rides* an already-open
prompt returns False, so the caller won't silently proceed when a ridden prompt (e.g. a
deferred startup unlock) resolves still-locked.
"""

import sshpilot.secret_storage as ss
from sshpilot import secret_unlock_dialog as d


def test_prompt_unlock_returns_true_when_no_unlock_needed(monkeypatch):
    mgr = ss.get_secret_manager()
    monkeypatch.setattr(mgr, 'selected_needs_unlock', lambda: False)
    got = []
    assert d.prompt_unlock(None, on_done=got.append) is True   # owns / no-op
    assert got == [True]


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
