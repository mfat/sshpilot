"""Tests for the shared wrap_argv_with_sshpass helper."""

import os


from sshpilot import ssh_password_exec as spe


def test_wrap_prepends_sshpass_and_mutates_env(monkeypatch, tmp_path):
    monkeypatch.setattr(spe, "get_sshpass_path", lambda: "/usr/bin/sshpass")

    env = {"SSH_ASKPASS": "/tmp/helper", "SSH_ASKPASS_REQUIRE": "prefer", "FOO": "bar"}
    argv = ["scp", "-v", "user@host:/x", "/local"]

    wrapped, cleanup = spe.wrap_argv_with_sshpass(argv, "secret", env=env)

    # [sshpass, -f, <fifo>, *argv]
    assert wrapped[0] == "/usr/bin/sshpass"
    assert wrapped[1] == "-f"
    fifo = wrapped[2]
    assert wrapped[3:] == argv
    assert os.path.basename(fifo) == "pw.fifo"

    # env mutated: askpass stripped, REQUIRE=never, other keys intact
    assert "SSH_ASKPASS" not in env
    assert env["SSH_ASKPASS_REQUIRE"] == "never"
    assert env["FOO"] == "bar"

    # cleanup removes the temp dir
    tmpdir = os.path.dirname(fifo)
    assert os.path.isdir(tmpdir)
    cleanup()
    assert not os.path.exists(tmpdir)


def test_wrap_without_sshpass_returns_argv_unchanged(monkeypatch):
    monkeypatch.setattr(spe, "get_sshpass_path", lambda: None)

    env = {"SSH_ASKPASS": "/tmp/helper"}
    argv = ["ssh-copy-id", "-i", "k.pub", "user@host"]

    wrapped, cleanup = spe.wrap_argv_with_sshpass(argv, "secret", env=env)

    assert wrapped == argv
    # env still mutated so caller falls back to interactive prompt cleanly
    assert "SSH_ASKPASS" not in env
    assert env["SSH_ASKPASS_REQUIRE"] == "never"
    cleanup()  # no-op, must not raise


def test_wrap_without_env_arg_is_allowed(monkeypatch):
    monkeypatch.setattr(spe, "get_sshpass_path", lambda: "/usr/bin/sshpass")
    wrapped, cleanup = spe.wrap_argv_with_sshpass(["ssh", "host"], "pw")
    assert wrapped[0] == "/usr/bin/sshpass"
    cleanup()
