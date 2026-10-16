"""The native askpass launcher must be startable by the platform's OpenSSH.

``ensure_key_in_agent`` (agent preload) runs ``ssh-add`` with
``get_ssh_env_with_askpass``, outside the daemon broker. OpenSSH treats an
askpass it cannot start as an empty answer, so a launcher OpenSSH cannot run
(a shell script on Windows: CreateProcessW error 193) fails without any
visible error.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from sshpilot import askpass_utils


@pytest.fixture
def fresh_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(askpass_utils, "_ASKPASS_DIR", None)
    monkeypatch.setattr(askpass_utils, "_ASKPASS_SCRIPT", None)
    monkeypatch.setattr(askpass_utils, "ensure_askpass_log_forwarder", lambda: None)
    log_dir = tmp_path / "askpass-log"
    monkeypatch.setenv("SSHPILOT_ASKPASS_LOG_DIR", str(log_dir))
    monkeypatch.setattr(askpass_utils, "_ASKPASS_LOG_PATH", None)
    yield log_dir / "sshpilot-askpass.log"
    directory = askpass_utils._ASKPASS_DIR
    if directory:
        shutil.rmtree(directory, ignore_errors=True)


def test_launcher_returns_the_staged_passphrase_for_an_ssh_add_prompt(
    tmp_path, fresh_launcher
):
    # ssh-add names the key in its prompt; the launcher must pass that prompt
    # through intact (cmd.exe's %* on Windows) and print only the secret.
    env = askpass_utils.get_ssh_env_with_askpass(
        "force", session_passphrase="staged-pp value"
    )
    key = tmp_path / "my keys" / "id_ed25519"
    completed = subprocess.run(
        [env["SSH_ASKPASS"], f"Enter passphrase for {key}: "],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.rstrip(b"\r\n") == b"staged-pp value"


def test_openssh_starts_the_native_askpass_launcher(tmp_path, fresh_launcher):
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        pytest.skip("ssh-keygen is unavailable")
    key = tmp_path / "id_ed25519"
    subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-N", "launcher-test-pp", "-f", str(key)],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    env = askpass_utils.get_ssh_env_with_askpass("force")
    # ``ssh-keygen -y`` prompts without a key path, so the helper declines
    # without opening a dialog; what matters is that OpenSSH ran it.
    completed = subprocess.run(
        [keygen, "-y", "-f", str(key)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=60,
    )

    assert "CreateProcess" not in completed.stderr
    assert "posix_spawn" not in completed.stderr
    assert fresh_launcher.is_file(), completed.stderr
    assert "ASKPASS called with prompt" in fresh_launcher.read_text(encoding="utf-8")
