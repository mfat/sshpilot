"""Askpass login-password autofill, MFA, and FIDO presence hints."""
import os

import pytest

from sshpilot.askpass_utils import (
    classify_prompt,
    handle_askpass_cli,
    stage_session_password,
)


def test_classify_prompt_password_vs_otp():
    assert classify_prompt("Password:") == "password"
    assert classify_prompt("Password for alice@host:") == "password"
    assert classify_prompt("Verification code:") == "interactive"
    assert classify_prompt("Enter passphrase for key '/tmp/id':") == "passphrase"


def test_classify_prompt_markers_need_word_boundaries():
    # 'pin' ⊂ 'alpine', 'pinar'; 'otp' ⊂ 'scotp1' — still password prompts.
    assert classify_prompt("user@alpine's password:") == "password"
    assert classify_prompt("pinar@host's password:") == "password"
    assert classify_prompt("root@scotp1's password:") == "password"
    # Real OTP/PIN prompts still classify as interactive.
    assert classify_prompt("OTP for alice:") == "interactive"
    assert classify_prompt("Enter one-time password:") == "interactive"


def test_classify_prompt_fido_presence_and_pin():
    assert (
        classify_prompt("Confirm user presence for key ED25519-SK SHA256:abcd")
        == "presence"
    )
    assert classify_prompt("Tap your security key") == "presence"
    assert classify_prompt("Tap secure token") == "presence"
    assert classify_prompt("Enter PIN for authenticator:") == "interactive"
    assert classify_prompt("Enter PIN for authenticator") == "interactive"
    assert (
        classify_prompt(
            "Are you sure you want to continue connecting (yes/no/[fingerprint])? "
        )
        == "interactive"
    )


def test_askpass_answers_password_from_session_file(monkeypatch, tmp_path):
    path = stage_session_password("s3cret")
    assert path and os.path.isfile(path)
    monkeypatch.setenv("SSHPILOT_SESSION_PASSWORD_FILE", path)
    monkeypatch.setenv("SSHPILOT_PASSWORD_USER", "alice")
    monkeypatch.setenv("SSHPILOT_PASSWORD_HOSTS", "example.com")
    monkeypatch.delenv("SSHPILOT_ASKPASS_SOCKET", raising=False)

    assert handle_askpass_cli("Password:") == "s3cret"
    assert not os.path.exists(path)  # one-shot


def test_stage_session_password_prefers_runtime_dir(monkeypatch, tmp_path):
    # XDG_RUNTIME_DIR is tmpfs — the secret never rests on real disk.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = stage_session_password("s3cret")
    try:
        assert os.path.dirname(path) == str(tmp_path)
        assert oct(os.stat(path).st_mode & 0o777) == oct(0o600)
    finally:
        os.unlink(path)


def test_stage_session_password_sweeps_stale_files(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    stale = tmp_path / "sshpilot-pw-stale"
    stale.write_text("old-secret")
    os.utime(stale, (0, 0))  # ancient mtime
    fresh = tmp_path / "sshpilot-pw-fresh"
    fresh.write_text("fresh-secret")

    path = stage_session_password("s3cret")
    try:
        assert not stale.exists()   # unconsumed leftover removed
        assert fresh.exists()       # concurrent fresh file untouched
    finally:
        os.unlink(path)
        os.unlink(fresh)


def test_no_staging_when_backend_serves_password(monkeypatch):
    # Keyring-backed password: helper looks it up by host/user, no temp file.
    from sshpilot.ssh_connection_builder import _askpass_env_for_connection
    import types

    monkeypatch.setattr(
        "sshpilot.askpass_utils.lookup_ssh_password",
        lambda host, user: "vaulted" if user == "alice" else "",
    )
    connection = types.SimpleNamespace(
        username="alice", hostname="example.com", host="", nickname="ex"
    )
    env = _askpass_env_for_connection(connection, session_password="vaulted")
    assert "SSHPILOT_SESSION_PASSWORD_FILE" not in env

    # In-memory password the backend does NOT have → staged.
    env = _askpass_env_for_connection(connection, session_password="typed-now")
    staged = env.get("SSHPILOT_SESSION_PASSWORD_FILE")
    assert staged and os.path.isfile(staged)
    os.unlink(staged)


def test_autofill_only_mode_never_shows_ui(monkeypatch):
    monkeypatch.setenv("SSHPILOT_ASKPASS_AUTOFILL_ONLY", "1")
    monkeypatch.delenv("SSH_ASKPASS_PROMPT", raising=False)
    monkeypatch.delenv("SSHPILOT_SESSION_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("SSHPILOT_PASSWORD_USER", "alice")
    monkeypatch.setenv("SSHPILOT_PASSWORD_HOSTS", "example.com")

    def _boom(*_a, **_k):
        raise AssertionError("UI must not be reached in autofill-only mode")

    for name in (
        "_route_challenge_to_main_app",
        "_route_password_to_main_app",
        "_route_presence_to_main_app",
        "_run_challenge_dialog",
        "_run_presence_dialog",
    ):
        monkeypatch.setattr(f"sshpilot.askpass_utils.{name}", _boom)

    # Stored password → silent autofill.
    monkeypatch.setattr(
        "sshpilot.askpass_utils.lookup_ssh_password",
        lambda host, user: "vaulted" if user == "alice" else "",
    )
    assert handle_askpass_cli("alice@example.com's password:") == "vaulted"

    # Nothing stored → silent decline (no dialog).
    monkeypatch.setattr(
        "sshpilot.askpass_utils.lookup_ssh_password", lambda *_: ""
    )
    assert handle_askpass_cli("alice@example.com's password:") is None
    assert handle_askpass_cli("Verification code:") is None
    assert handle_askpass_cli("Touch your security key") is None


def _serve_one_reply(tmp_path, reply_json):
    """One-shot Unix-socket server standing in for the main-app askpass IPC."""
    import socket
    import threading

    sock_path = str(tmp_path / "askpass.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)

    def _serve():
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(reply_json.encode() + b"\n")
        server.close()

    threading.Thread(target=_serve, daemon=True).start()
    return sock_path


def test_ask_main_app_preserves_empty_string_reply(monkeypatch, tmp_path):
    # ok+"" (acknowledged presence / empty kbd-interactive answer) must not
    # collapse into None, which callers treat as cancel (helper exits 1).
    from sshpilot.askpass_utils import _ask_main_app

    sock_path = _serve_one_reply(tmp_path, '{"ok": true, "value": ""}')
    monkeypatch.setenv("SSHPILOT_ASKPASS_SOCKET", sock_path)
    monkeypatch.setenv("SSHPILOT_ASKPASS_TOKEN", "tok")

    handled, value = _ask_main_app(
        {"type": "presence", "prompt": "touch"}, lambda m: None, ok_key="value"
    )
    assert handled is True
    assert value == ""  # not None


def test_ask_main_app_cancel_is_none(monkeypatch, tmp_path):
    from sshpilot.askpass_utils import _ask_main_app

    sock_path = _serve_one_reply(tmp_path, '{"ok": false}')
    monkeypatch.setenv("SSHPILOT_ASKPASS_SOCKET", sock_path)
    monkeypatch.setenv("SSHPILOT_ASKPASS_TOKEN", "tok")

    handled, value = _ask_main_app(
        {"type": "presence", "prompt": "touch"}, lambda m: None, ok_key="value"
    )
    assert handled is True
    assert value is None


def test_askpass_prompts_user_for_otp(monkeypatch):
    # OpenSSH prefer does not fall back to TTY — interactive prompts must ask
    # the user (never autofill MFA from the vault).
    monkeypatch.setenv("SSHPILOT_SESSION_PASSWORD_FILE", "")
    monkeypatch.setenv("SSHPILOT_PASSWORD_USER", "alice")
    monkeypatch.setenv("SSHPILOT_PASSWORD_HOSTS", "example.com")
    monkeypatch.setattr(
        "sshpilot.askpass_utils._route_challenge_to_main_app",
        lambda prompt, _log: (True, "123456"),
    )
    assert handle_askpass_cli("Verification code:") == "123456"


def test_askpass_otp_cancel(monkeypatch):
    monkeypatch.setattr(
        "sshpilot.askpass_utils._route_challenge_to_main_app",
        lambda prompt, _log: (True, None),
    )
    assert handle_askpass_cli("(user@host) Verification code:") is None


def test_askpass_password_from_backend(monkeypatch):
    calls = []

    def _lookup(host, user):
        calls.append((host, user))
        if host == "example.com" and user == "alice":
            return "from-vault"
        return ""

    monkeypatch.setattr(
        "sshpilot.askpass_utils.lookup_ssh_password", _lookup
    )
    monkeypatch.delenv("SSHPILOT_SESSION_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("SSHPILOT_PASSWORD_USER", "alice")
    monkeypatch.setenv("SSHPILOT_PASSWORD_HOSTS", "other\nexample.com")

    assert handle_askpass_cli("password for alice@example.com:") == "from-vault"
    assert ("example.com", "alice") in calls


def test_askpass_prompts_user_for_unstored_password(monkeypatch):
    # prefer does not fall back to TTY — unstored login passwords need a dialog.
    monkeypatch.delenv("SSHPILOT_SESSION_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("SSHPILOT_PASSWORD_USER", "alice")
    monkeypatch.setenv("SSHPILOT_PASSWORD_HOSTS", "example.com")
    monkeypatch.setattr(
        "sshpilot.askpass_utils.lookup_ssh_password", lambda *_a, **_k: ""
    )
    monkeypatch.setattr(
        "sshpilot.askpass_utils._route_password_to_main_app",
        lambda prompt, _log: (True, "typed-in"),
    )
    assert handle_askpass_cli("alice@example.com's password: ") == "typed-in"


def test_askpass_unstored_password_cancel(monkeypatch):
    monkeypatch.setattr(
        "sshpilot.askpass_utils.lookup_ssh_password", lambda *_a, **_k: ""
    )
    monkeypatch.setattr(
        "sshpilot.askpass_utils._route_password_to_main_app",
        lambda prompt, _log: (True, None),
    )
    assert handle_askpass_cli("Password:") is None


def test_askpass_honors_prompt_hint_none(monkeypatch):
    """OpenSSH notify_start sets SSH_ASKPASS_PROMPT=none for FIDO touch."""
    monkeypatch.setenv("SSH_ASKPASS_PROMPT", "none")
    monkeypatch.setattr(
        "sshpilot.askpass_utils._route_presence_to_main_app",
        lambda prompt, _log: (True, ""),
    )
    assert handle_askpass_cli("Confirm user presence for key ED25519-SK") == ""


def test_askpass_honors_prompt_hint_confirm(monkeypatch):
    monkeypatch.setenv("SSH_ASKPASS_PROMPT", "confirm")
    monkeypatch.setattr(
        "sshpilot.askpass_utils._route_confirm_to_main_app",
        lambda prompt, _log: (True, "yes"),
    )
    assert handle_askpass_cli("Allow use of key?") == "yes"


def test_askpass_presence_classifier_uses_reminder_not_challenge(monkeypatch):
    monkeypatch.delenv("SSH_ASKPASS_PROMPT", raising=False)
    called = {"challenge": False, "presence": False}

    def _challenge(*_a):
        called["challenge"] = True
        return (True, "nope")

    def _presence(*_a):
        called["presence"] = True
        return (True, "")

    monkeypatch.setattr(
        "sshpilot.askpass_utils._route_challenge_to_main_app", _challenge
    )
    monkeypatch.setattr(
        "sshpilot.askpass_utils._route_presence_to_main_app", _presence
    )
    assert handle_askpass_cli("Tap secure token") == ""
    assert called["presence"] and not called["challenge"]


def test_askpass_unrecognized_prompt_treated_as_challenge(monkeypatch):
    monkeypatch.delenv("SSH_ASKPASS_PROMPT", raising=False)
    monkeypatch.setattr(
        "sshpilot.askpass_utils._route_challenge_to_main_app",
        lambda prompt, _log: (True, "custom-mfa-response"),
    )
    assert handle_askpass_cli("Duo Push: enter a passcode from your phone") == (
        "custom-mfa-response"
    )
