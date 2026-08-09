import logging
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

from sshpilot.askpass_utils import handle_askpass_cli, staged_session_passphrase
from sshpilot.key_manager import KeyManager
from sshpilot.ssh_connection_validator import SSHConnectionValidator


SECRET = "  leading and trailing passphrase\t "


def _successful_result():
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _assert_mode_0600(path):
    # Windows' stat result maps every writable regular file to 0666; POSIX
    # permission bits are meaningful (and enforced) on the project's Unix
    # runtime targets and CI.
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_staged_passphrase_is_0600_reusable_exact_and_removed(monkeypatch):
    staged_path = None

    with staged_session_passphrase(SECRET) as path:
        staged_path = path
        _assert_mode_0600(path)
        assert Path(path).read_text(encoding="utf-8") == SECRET

        monkeypatch.setenv("SSHPILOT_SESSION_PASSPHRASE_FILE", path)
        assert handle_askpass_cli("Enter passphrase (empty for no passphrase):") == SECRET
        assert handle_askpass_cli("Enter same passphrase again:") == SECRET
        assert os.path.exists(path)

    assert staged_path is not None
    assert not os.path.exists(staged_path)


def test_staged_passphrase_is_removed_when_command_raises():
    staged_path = None

    try:
        with staged_session_passphrase(SECRET) as path:
            staged_path = path
            raise RuntimeError("simulated command failure")
    except RuntimeError:
        pass

    assert staged_path is not None
    assert not os.path.exists(staged_path)


def test_sweep_removes_only_stale_staged_passphrase_files(monkeypatch, tmp_path):
    from sshpilot.askpass_utils import (
        _SESSION_PASSWORD_TTL,
        _sweep_stale_session_files,
    )

    now = 1_000_000.0
    stale_path = tmp_path / "sshpilot-passphrase-stale"
    fresh_path = tmp_path / "sshpilot-passphrase-fresh"
    stale_path.write_text("stale", encoding="utf-8")
    fresh_path.write_text("fresh", encoding="utf-8")
    stale_time = now - _SESSION_PASSWORD_TTL - 1
    os.utime(stale_path, (stale_time, stale_time))
    os.utime(fresh_path, (now, now))
    monkeypatch.setattr("sshpilot.askpass_utils.time.time", lambda: now)

    _sweep_stale_session_files(str(tmp_path))

    assert not stale_path.exists()
    assert fresh_path.exists()


def test_generate_key_keeps_passphrase_out_of_argv_env_and_logs(
    monkeypatch, tmp_path, caplog
):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        key_path = Path(argv[argv.index("-f") + 1])
        key_path.write_text("private", encoding="utf-8")
        Path(f"{key_path}.pub").write_text("public", encoding="utf-8")
        passphrase_file = kwargs["env"]["SSHPILOT_SESSION_PASSPHRASE_FILE"]
        _assert_mode_0600(passphrase_file)
        assert Path(passphrase_file).read_text(encoding="utf-8") == SECRET
        return _successful_result()

    monkeypatch.setattr(
        "sshpilot.key_manager.get_ssh_env_with_askpass",
        lambda _require: {"SAFE_VALUE": "visible"},
    )
    monkeypatch.setattr("sshpilot.key_manager.subprocess.run", fake_run)

    manager = KeyManager(tmp_path)
    monkeypatch.setattr(manager, "emit", lambda *_args: None, raising=False)
    caplog.set_level(logging.DEBUG)
    key = manager.generate_key("id_secure", passphrase=SECRET)

    assert key is not None
    argv, kwargs = calls[0]
    assert SECRET not in argv
    assert "-N" not in argv
    assert all(SECRET not in str(value) for value in kwargs["env"].values())
    staged_path = kwargs["env"]["SSHPILOT_SESSION_PASSPHRASE_FILE"]
    assert not os.path.exists(staged_path)
    assert SECRET not in caplog.text


def test_verify_key_passphrase_keeps_secret_out_of_argv_and_env(
    monkeypatch, tmp_path, caplog
):
    key_path = tmp_path / "id_secure"
    key_path.write_text("private", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        passphrase_file = kwargs["env"]["SSHPILOT_SESSION_PASSPHRASE_FILE"]
        assert Path(passphrase_file).read_text(encoding="utf-8") == SECRET
        return _successful_result()

    monkeypatch.setattr(
        "sshpilot.ssh_connection_validator.get_ssh_env_with_askpass",
        lambda _require: {"SAFE_VALUE": "visible"},
    )
    monkeypatch.setattr(
        "sshpilot.ssh_connection_validator.subprocess.run", fake_run
    )

    caplog.set_level(logging.DEBUG)
    assert SSHConnectionValidator().verify_key_passphrase(str(key_path), SECRET)

    argv, kwargs = calls[0]
    assert SECRET not in argv
    assert "-P" not in argv
    assert all(SECRET not in str(value) for value in kwargs["env"].values())
    staged_path = kwargs["env"]["SSHPILOT_SESSION_PASSPHRASE_FILE"]
    assert not os.path.exists(staged_path)
    assert SECRET not in caplog.text


def test_verify_key_passphrase_empty_uses_explicit_empty_passphrase(
    monkeypatch, tmp_path
):
    key_path = tmp_path / "id_unencrypted"
    key_path.write_text("private", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _successful_result()

    monkeypatch.setattr(
        "sshpilot.ssh_connection_validator.subprocess.run", fake_run
    )

    assert SSHConnectionValidator().verify_key_passphrase(str(key_path), "")

    argv, kwargs = calls[0]
    assert argv == ["ssh-keygen", "-y", "-P", "", "-f", str(key_path)]
    assert "env" not in kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
