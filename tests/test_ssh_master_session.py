"""Tests for the PTY-backed MasterSession (master-first file-manager auth).

A fake "ssh" python script runs on a real PTY and plays the server's part:
prompting for a password, an OTP code, or failing outright. The tests assert
the session auto-answers stored secrets, escalates unanswerable prompts with
the PTY fd + transcript, and reports failures with the transcript tail.
"""

import os
import sys
import threading
import types

import pytest

import sshpilot.ssh_master_session as mss


FAKE_SSH = r"""
import sys, time
mode, flag = sys.argv[1], sys.argv[2]
if mode == "password":
    sys.stdout.write("user@host's password: ")
    sys.stdout.flush()
    line = sys.stdin.readline().strip()
    if line == "sekret":
        with open(flag, "w") as fh:
            fh.write("ok")
        sys.stdout.write("\nconnected\n")
        sys.stdout.flush()
        time.sleep(30)  # emulate ssh -N staying attached
    else:
        sys.stdout.write("Permission denied, please try again.\n")
        sys.exit(255)
elif mode == "otp":
    sys.stdout.write("Verification code: ")
    sys.stdout.flush()
    time.sleep(30)
elif mode == "fail":
    sys.stdout.write("ssh: connect to host example port 22: Connection refused\n")
    sys.stdout.flush()
    sys.exit(255)
"""


class _Callbacks:
    def __init__(self):
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.needs_interaction = threading.Event()
        self.need_password = threading.Event()
        self.interaction_fd = None
        self.interaction_transcript = ""
        self.failed_tail = ""
        self.password_prompt = ""

    def on_ready(self):
        self.ready.set()

    def on_needs_interaction(self, fd, transcript):
        self.interaction_fd = fd
        self.interaction_transcript = transcript
        self.needs_interaction.set()

    def on_need_password(self, prompt):
        self.password_prompt = prompt
        self.need_password.set()

    def on_failed(self, tail):
        self.failed_tail = tail
        self.failed.set()


@pytest.fixture
def harness(monkeypatch, tmp_path):
    script = tmp_path / "fake_ssh.py"
    script.write_text(FAKE_SSH)
    flag = tmp_path / "authed.flag"

    monkeypatch.setattr(mss, "_CHECK_INTERVAL", 0.1)
    # The flag file written by the fake ssh after successful auth stands in
    # for a live control socket.
    monkeypatch.setattr(
        mss, "check_master_alive", lambda *a, **k: flag.exists()
    )
    monkeypatch.setattr(mss, "_get_stored_password", lambda *a: None)

    def make_session(mode, password=None):
        monkeypatch.setattr(
            mss,
            "build_ssh_connection",
            lambda ctx: types.SimpleNamespace(
                command=[sys.executable, str(script), mode, str(flag)],
                env={},
                use_sshpass=bool(password),
                password=password,
                use_askpass=False,
            ),
        )
        callbacks = _Callbacks()
        connection = types.SimpleNamespace(
            nickname="host", resolved_identity_files=[]
        )
        session = mss.MasterSession(
            connection,
            None,
            None,
            on_ready=callbacks.on_ready,
            on_needs_interaction=callbacks.on_needs_interaction,
            on_need_password=callbacks.on_need_password,
            on_failed=callbacks.on_failed,
        )
        return session, callbacks

    return make_session


def test_auto_answers_stored_password(harness):
    session, callbacks = harness("password", password="sekret")
    session.start()
    assert callbacks.ready.wait(10), "master never reported ready"
    assert not callbacks.failed.is_set()
    assert not callbacks.need_password.is_set()


def test_unstored_password_escalates_to_callback(harness):
    session, callbacks = harness("password", password=None)
    session.start()
    assert callbacks.need_password.wait(10), "password callback never fired"
    assert "password" in callbacks.password_prompt.lower()
    session.send_secret("sekret")
    assert callbacks.ready.wait(10), "master never reported ready after answer"


def test_interactive_prompt_hands_off_pty(harness):
    session, callbacks = harness("otp")
    session.start()
    assert callbacks.needs_interaction.wait(10), "interaction callback never fired"
    assert "verification code" in callbacks.interaction_transcript.lower()
    assert callbacks.interaction_fd is not None
    # The UI owns the fd after handover; answering through it completes nothing
    # here (fake ssh ignores it) — just verify it is a live PTY fd and clean up.
    os.write(callbacks.interaction_fd, b"\x03")
    session.cancel()
    os.close(callbacks.interaction_fd)


def test_exit_before_ready_reports_failure(harness):
    session, callbacks = harness("fail")
    session.start()
    assert callbacks.failed.wait(10), "failure callback never fired"
    assert "connection refused" in callbacks.failed_tail.lower()


def test_invalidate_master_runs_stop_via_builder(monkeypatch):
    """invalidate_master must go through build_ssh_connection (single command
    path: -F config → same %C) and use -O stop, which drains live sessions
    instead of killing them like -O exit would."""
    captured = {}

    monkeypatch.setattr(
        mss,
        "build_ssh_connection",
        lambda ctx: (
            captured.update(extra_args=ctx.extra_args),
            types.SimpleNamespace(command=["ssh", "-O", "stop", "host"], env={}),
        )[1],
    )

    ran = {}
    monkeypatch.setattr(
        mss.subprocess, "run",
        lambda argv, **kw: ran.update(argv=argv)
        or types.SimpleNamespace(returncode=0),
    )

    connection = types.SimpleNamespace(nickname="host")
    mss.invalidate_master(connection, background=False)

    assert captured["extra_args"][:2] == ["-O", "stop"]
    assert any(str(a).startswith("ControlPath=") for a in captured["extra_args"])
    assert ran["argv"] == ["ssh", "-O", "stop", "host"]


def test_classify_prompt():
    assert mss.classify_prompt("user@host's password: ") == "password"
    assert (
        mss.classify_prompt("Enter passphrase for key '/home/u/.ssh/id_ed25519': ")
        == "passphrase"
    )
    assert mss.classify_prompt("Verification code: ") == "interactive"
    assert mss.classify_prompt("Enter PIN for authenticator: ") == "interactive"
    assert mss.classify_prompt("Confirm user presence for key ED25519-SK") == "interactive"
    assert (
        mss.classify_prompt(
            "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
        )
        == "interactive"
    )
    # Statements, not prompts.
    assert mss.classify_prompt("Permission denied (publickey,password).") is None
    assert mss.classify_prompt("") is None
