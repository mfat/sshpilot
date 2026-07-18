"""Askpass login-password autofill and MFA decline."""
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


def test_askpass_answers_password_from_session_file(monkeypatch, tmp_path):
    path = stage_session_password("s3cret")
    assert path and os.path.isfile(path)
    monkeypatch.setenv("SSHPILOT_SESSION_PASSWORD_FILE", path)
    monkeypatch.setenv("SSHPILOT_PASSWORD_USER", "alice")
    monkeypatch.setenv("SSHPILOT_PASSWORD_HOSTS", "example.com")
    monkeypatch.delenv("SSHPILOT_ASKPASS_SOCKET", raising=False)

    assert handle_askpass_cli("Password:") == "s3cret"
    assert not os.path.exists(path)  # one-shot


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
