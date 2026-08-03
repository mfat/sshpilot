"""Tests for the default-keepalive injection (Phase 1) and the authoritative
ConnectionState model (Phase 2)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot import ssh_connection_builder as builder
from sshpilot.connection_manager import Connection, ConnectionState


def _inject(overrides=None, app_cfg=None):
    cmd = []
    builder._maybe_append_default_keepalive(cmd, overrides or [], app_cfg or {})
    return cmd


# --- Phase 1: default keepalive injection -----------------------------------

def test_default_keepalive_injected_when_unset():
    cmd = _inject(app_cfg={
        'apply_default_keepalive': True,
        'default_keepalive_interval': 15,
        'default_keepalive_count': 3,
    })
    assert cmd == ['-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']


def test_default_keepalive_respects_opt_out():
    cmd = _inject(app_cfg={'apply_default_keepalive': False})
    assert cmd == []


def test_default_keepalive_skipped_when_app_value_set():
    cmd = _inject(app_cfg={'apply_default_keepalive': True, 'keepalive_interval': 33})
    assert cmd == []


def test_default_keepalive_skipped_when_in_overrides():
    cmd = _inject(
        overrides=['-o', 'ServerAliveInterval=42'],
        app_cfg={'apply_default_keepalive': True},
    )
    assert cmd == []


def test_default_keepalive_defaults_to_on_when_flag_absent():
    # Missing apply_default_keepalive defaults to True.
    cmd = _inject(app_cfg={'default_keepalive_interval': 20, 'default_keepalive_count': 2})
    assert cmd == ['-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=2']


# --- Phase 2: ConnectionState model + is_connected compat -------------------

def test_initial_state_is_unknown():
    c = Connection({'nickname': 'a', 'host': 'a'})
    assert c.get_status() == ConnectionState.UNKNOWN
    assert c.is_connected is False


def test_is_connected_setter_maps_to_state():
    c = Connection({'nickname': 'a', 'host': 'a'})
    c.is_connected = True
    assert c.get_status() == ConnectionState.CONNECTED
    assert c.is_connected is True
    c.is_connected = False
    assert c.get_status() == ConnectionState.DISCONNECTED
    assert c.is_connected is False


def test_set_status_carries_reason():
    c = Connection({'nickname': 'a', 'host': 'a'})
    c.set_status(ConnectionState.FAILED, 'Authentication failed')
    assert c.get_status() == ConnectionState.FAILED
    assert c.get_status_reason() == 'Authentication failed'
    assert c.is_connected is False


def test_bool_false_does_not_clobber_failed():
    # A plain is_connected=False (e.g. generic cleanup) must not erase a richer
    # FAILED reason already recorded.
    c = Connection({'nickname': 'a', 'host': 'a'})
    c.set_status(ConnectionState.FAILED, 'Host unreachable')
    c.is_connected = False
    assert c.get_status() == ConnectionState.FAILED
    assert c.get_status_reason() == 'Host unreachable'


def test_connecting_is_not_connected():
    c = Connection({'nickname': 'a', 'host': 'a'})
    c.set_status(ConnectionState.CONNECTING)
    assert c.is_connected is False


# --- Phase 3: ssh exit classification ---------------------------------------

import pytest


@pytest.mark.parametrize('msg, code, was_connected, exp_state, exp_reason', [
    ('Permission denied (publickey,password).', 255, False, ConnectionState.FAILED, 'Authentication failed'),
    ('ssh: connect to host x port 22: Connection refused', 255, False, ConnectionState.FAILED, 'Connection refused'),
    ('ssh: connect to host x: No route to host', 255, False, ConnectionState.FAILED, 'Host unreachable'),
    ('Could not resolve hostname x: Name or service not known', 255, False, ConnectionState.FAILED, 'Host not found'),
    ('Host key verification failed.', 255, False, ConnectionState.FAILED, 'Host key verification failed'),
    (None, 255, True, ConnectionState.DISCONNECTED, 'Connection lost'),
    (None, 255, False, ConnectionState.FAILED, 'Connection failed'),
    (None, 1, False, ConnectionState.DISCONNECTED, ''),
])
def test_classify_exit(msg, code, was_connected, exp_state, exp_reason):
    # Importing the terminal module pulls in GTK/VTE; in a polluted suite run a
    # prior test may have stubbed those, so skip (don't fail) if it can't import.
    try:
        from sshpilot import terminal as terminal_mod
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"sshpilot.terminal unavailable: {exc}")

    class _T:
        last_error_message = msg
        _connect_failure_hint = ''

    state, reason = terminal_mod.TerminalWidget._classify_exit(_T(), code, was_connected)
    assert state == exp_state
    assert reason == exp_reason


# --- Connect-evidence gating (fixes false "Connected" on a stalled connect) ---

_TIMEOUT_V_LOG = """debug1: OpenSSH_10.2p1 Ubuntu-2ubuntu3.2, OpenSSL 3.5.5 27 Jan 2026
debug1: Reading configuration data /home/mahdi/.ssh/config
debug1: /home/mahdi/.ssh/config line 39: Applying options for arvan
debug1: Connecting to 188.121.117.208 [188.121.117.208] port 22.
debug1: connect to address 188.121.117.208 port 22: Connection timed out
ssh: connect to host 188.121.117.208 port 22: Connection timed out"""


@pytest.mark.parametrize('text, expected', [
    # The reported bug: a -v connect that times out must stay 'failed', never
    # produce evidence of being connected.
    (_TIMEOUT_V_LOG, 'failed'),
    # Mid-connect with verbosity on: only debug chatter, no failure yet → pending
    # (so the indicator stays CONNECTING instead of flashing green).
    ('debug1: OpenSSH_10.2\ndebug1: Connecting to host port 22.', 'pending'),
    ('user@host: Permission denied (publickey,password).', 'failed'),
    ('ssh: connect to host x port 22: Connection refused', 'failed'),
    ('Last login: Mon Jun  5 17:00:00 2026 from 1.2.3.4', 'connected'),
    ('debug1: Authenticated to host ([1.2.3.4]:22) using "publickey".', 'connected'),
    ('mahdi@server:~$ ', 'connected'),
    ('', 'pending'),
    # An unanswered auth prompt is not evidence of a session: it is drawn
    # before authentication succeeds (the passphrase one before a single packet
    # is sent), so the indicator must stay CONNECTING until the login lands.
    ("pilot@127.0.0.1's password: ", 'pending'),
    ('Password: ', 'pending'),
    ("Enter passphrase for key '/home/mahdi/.ssh/id_ed25519': ", 'pending'),
    # ssh-add's wording, without the "key" the client's own prompt has.
    ('Enter passphrase for /home/mahdi/.ssh/id_rsa: ', 'pending'),
    ('(pilot@127.0.0.1) Verification code: ', 'pending'),
    ('OTP: ', 'pending'),
    ('2FA code: ', 'pending'),
    ('Enter authentication code: ', 'pending'),
    ('Enter PIN for authenticator: ', 'pending'),
    # FIDO touch: no typed secret, but still nobody logged in.
    ('Confirm user presence for key ED25519-SK SHA256:abc', 'pending'),
    ('Touch your security key to continue', 'pending'),
    ("Warning: Permanently added '[127.0.0.1]:2203' (ED25519) to the list of "
     "known hosts.\npilot@127.0.0.1's password: ", 'pending'),
    # Whole host-key banner: its first line reads like remote output on its own,
    # so the trailing question has to gate the promotion.
    ("The authenticity of host '[127.0.0.1]:2201' can't be established.\n"
     'ED25519 key fingerprint is SHA256:abc.\n'
     'Are you sure you want to continue connecting (yes/no/[fingerprint])? ',
     'pending'),
    # ...and every partial state the poller can scrape while the banner is
    # still being drawn, before the question exists to gate on.
    ("The authenticity of host '[127.0.0.1]:2201' can't be established.",
     'pending'),
    ("The authenticity of host '[127.0.0.1]:2201' can't be established.\n"
     'ED25519 key fingerprint is SHA256:abc.', 'pending'),
    ("The authenticity of host '[127.0.0.1]:2201' can't be established.\n"
     'ED25519 key fingerprint is SHA256:abc.\n'
     'This key is not known by any other names.', 'pending'),
    ("The authenticity of host '[127.0.0.1]:2202' can't be established.\n"
     'ED25519 key fingerprint is SHA256:abc.\n'
     'This host key is known by the following other names/addresses:\n'
     '    ~/.ssh/known_hosts:3: [127.0.0.1]:2201', 'pending'),
    # The echoed answer, when the terminal puts it on its own line.
    ("The authenticity of host '[127.0.0.1]:2201' can't be established.\n"
     'Are you sure you want to continue connecting (yes/no/[fingerprint])? \n'
     'yes', 'pending'),
    # Once the session is really up, the banner in the scrollback must not
    # keep it pending forever.
    ("The authenticity of host '[127.0.0.1]:2201' can't be established.\n"
     'ED25519 key fingerprint is SHA256:abc.\n'
     'Are you sure you want to continue connecting (yes/no/[fingerprint])? yes\n'
     "Warning: Permanently added '[127.0.0.1]:2201' (ED25519) to the list of "
     'known hosts.\npilot@rig:~$ ', 'connected'),
    # A prompt that is no longer the trailing line is still not evidence.
    ("pilot@127.0.0.1's password: \ndebug1: pledge: filesystem", 'pending'),
    # ...but the prompt must not mask real evidence that arrives after it.
    ("pilot@127.0.0.1's password: \npilot@rig:~$ ", 'connected'),
    # Remote output that merely mentions an auth word is still remote output.
    ('This host uses an authenticator app for sudo.', 'connected'),
])
def test_scan_connect_evidence(text, expected):
    try:
        from sshpilot import terminal as terminal_mod
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"sshpilot.terminal unavailable: {exc}")

    class _FakeBackend:
        def get_content(self, n):
            return text[-n:]

    class _T:
        backend = _FakeBackend()
        _connect_failure_hint = ''
        _scrape_recent_terminal_text = terminal_mod.TerminalWidget._scrape_recent_terminal_text

    t = _T()
    assert terminal_mod.TerminalWidget._scan_connect_evidence(t) == expected
    if expected == 'failed':
        # A failure reason line is captured for the exit classifier.
        assert t._connect_failure_hint


def test_connect_evidence_poller_keeps_watching_after_grace(monkeypatch):
    """Late remote output must still promote without guessing from liveness."""
    try:
        from sshpilot import terminal as terminal_mod
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"sshpilot.terminal unavailable: {exc}")

    scheduled = []

    def _schedule_slow_poll(seconds, callback):
        scheduled.append((seconds, callback))
        return 123

    monkeypatch.setattr(
        terminal_mod.GLib, 'timeout_add_seconds', _schedule_slow_poll
    )

    class _T:
        _is_quitting = False
        connection_state = ConnectionState.CONNECTING
        _connect_poll_count = 59
        _connect_grace_timer_id = 42
        session_id = 'late-connect'
        verdict = 'pending'
        marked_connected = False

        _on_connect_grace_elapsed = (
            terminal_mod.TerminalWidget._on_connect_grace_elapsed
        )

        def _scan_connect_evidence(self):
            return self.verdict

        def _mark_connected(self):
            self.marked_connected = True

    terminal = _T()
    assert terminal._on_connect_grace_elapsed() is False
    assert terminal._connect_grace_timer_id == 123
    assert len(scheduled) == 1
    assert scheduled[0][0] == 5
    assert terminal.marked_connected is False

    terminal.verdict = 'connected'
    assert scheduled[0][1]() is False
    assert terminal.marked_connected is True
