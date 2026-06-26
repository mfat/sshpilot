"""TerminalWidget PTY auto-fill: one-shot typing when a known prompt appears."""
import types

import pytest

pytest.importorskip("gi")

from sshpilot.terminal import TerminalWidget


def _term(*, prompt="[sshPilot] sudo password:", response="s3cret"):
    t = TerminalWidget.__new__(TerminalWidget)
    t._pty_autofill = (prompt, response)
    t._pty_autofill_done = False
    t._pty_autofill_handler = 42
    t._pty_autofill_timeout_id = 99
    fed = []
    t.backend = types.SimpleNamespace(feed_child=lambda data: fed.append(data))
    t.vte = None
    t._scrape_recent_terminal_text = lambda max_chars=2000: (
        f"docker logs\n{prompt}"
    )
    t._fed = fed
    return t


def test_pty_autofill_feeds_response_on_prompt_match():
    t = _term()
    t._on_pty_autofill_changed(None)
    assert t._fed == [b"s3cret\n"]
    assert t._pty_autofill is None
    assert t._pty_autofill_done is True
    assert t._pty_autofill_handler is None
    assert t._pty_autofill_timeout_id is None


def test_pty_autofill_ignores_output_without_prompt():
    t = _term()
    t._scrape_recent_terminal_text = lambda max_chars=2000: "docker logs only"
    t._on_pty_autofill_changed(None)
    assert t._fed == []
    assert t._pty_autofill == ("[sshPilot] sudo password:", "s3cret")
    assert t._pty_autofill_done is False


def test_pty_autofill_falls_back_to_vte_feed_child():
    t = _term()
    t.backend = None
    fed = []
    t.vte = types.SimpleNamespace(feed_child=lambda data: fed.append(data))
    t._on_pty_autofill_changed(None)
    assert fed == [b"s3cret\n"]


def test_cancel_pty_autofill_clears_state():
    t = _term()
    disconnected = []
    t.vte = types.SimpleNamespace(disconnect=lambda hid: disconnected.append(hid))
    t._cancel_pty_autofill()
    assert t._pty_autofill is None
    assert t._pty_autofill_done is True
    assert disconnected == [42]
    assert t._pty_autofill_handler is None
    assert t._pty_autofill_timeout_id is None


def test_install_pty_autofill_noop_without_config():
    t = TerminalWidget.__new__(TerminalWidget)
    t._pty_autofill = None
    t._install_pty_autofill()  # must not raise
