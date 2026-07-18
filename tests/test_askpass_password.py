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
