"""Tests for the PTY-backed MasterSession (master-first file-manager auth).

Auth is askpass-only: MasterSession does not scrape the PTY or hand off an
AuthTerminalDialog. These tests cover askpass env wiring, ready/failed
lifecycle, and ControlMaster stop via the single command builder.
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
if mode == "ready":
    with open(flag, "w") as fh:
        fh.write("ok")
    sys.stdout.write("connected\n")
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
        self.failed_tail = ""

    def on_ready(self):
        self.ready.set()

    def on_failed(self, tail):
        self.failed_tail = tail
        self.failed.set()


@pytest.fixture
def harness(monkeypatch, tmp_path):
    script = tmp_path / "fake_ssh.py"
    script.write_text(FAKE_SSH)
    flag = tmp_path / "authed.flag"

    monkeypatch.setattr(mss, "_CHECK_INTERVAL", 0.1)
    monkeypatch.setattr(
        mss, "check_master_alive", lambda *a, **k: flag.exists()
    )

    def make_session(mode, *, prepared_env=None, password=None):
        captured = {}

        def _build(ctx):
            env = dict(prepared_env or {})
            return types.SimpleNamespace(
                command=[sys.executable, str(script), mode, str(flag)],
                env=env,
                use_sshpass=False,
                password=password,
                use_askpass=bool(env.get("SSH_ASKPASS")),
            )

        monkeypatch.setattr(mss, "build_ssh_connection", _build)
        monkeypatch.setattr(
            mss,
            "_askpass_env_for_connection",
            lambda *a, **k: {
                "SSH_ASKPASS": "/tmp/fake-askpass",
                "SSH_ASKPASS_REQUIRE": "prefer",
            },
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
            on_failed=callbacks.on_failed,
        )
        return session, callbacks, captured

    return make_session


def test_forces_askpass_prefer_when_resolver_omits_it(harness, monkeypatch):
    spawned = {}

    real_popen = mss.subprocess.Popen

    def _popen(*args, **kwargs):
        spawned["env"] = kwargs.get("env") or {}
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(mss.subprocess, "Popen", _popen)
    session, callbacks, _ = harness("ready", prepared_env={})
    session.start()
    assert callbacks.ready.wait(10), "master never reported ready"
    assert spawned["env"].get("SSH_ASKPASS") == "/tmp/fake-askpass"
    assert spawned["env"].get("SSH_ASKPASS_REQUIRE") == "prefer"
    session.cancel()


def test_keeps_resolver_askpass_and_forces_prefer(harness, monkeypatch):
    spawned = {}

    real_popen = mss.subprocess.Popen

    def _popen(*args, **kwargs):
        spawned["env"] = kwargs.get("env") or {}
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(mss.subprocess, "Popen", _popen)
    session, callbacks, _ = harness(
        "ready",
        prepared_env={
            "SSH_ASKPASS": "/resolver/askpass",
            "SSH_ASKPASS_REQUIRE": "force",
        },
    )
    session.start()
    assert callbacks.ready.wait(10)
    assert spawned["env"].get("SSH_ASKPASS") == "/resolver/askpass"
    assert spawned["env"].get("SSH_ASKPASS_REQUIRE") == "prefer"
    session.cancel()


def test_exit_before_ready_reports_failure(harness):
    session, callbacks, _ = harness("fail")
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


def test_classify_prompt_reexport():
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
    assert mss.classify_prompt("Permission denied (publickey,password).") is None
    assert mss.classify_prompt("") is None
