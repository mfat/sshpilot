"""Askpass / interaction policy tests."""
from __future__ import annotations

from sshpilot.core.interaction import (
    InteractionOutcome,
    InteractionRequest,
    InteractionResponse,
    PromptKind,
    ResponseType,
    build_request_from_prompt,
    classify_prompt,
    decide_headless,
    validate_response,
)


def test_classify_password_passphrase_hostkey():
    assert classify_prompt("alice@host's password:") == PromptKind.PASSWORD
    assert classify_prompt("Enter passphrase for key '/home/a/.ssh/id_ed25519':") == PromptKind.PASSPHRASE
    assert classify_prompt("Are you sure you want to continue connecting (yes/no/[fingerprint])?") == PromptKind.HOST_KEY


def test_password_request_and_cancel():
    req = build_request_from_prompt("Password:", hostname="h", username="u")
    assert req.kind == PromptKind.PASSWORD
    ok = validate_response(
        req,
        InteractionResponse(
            outcome=InteractionOutcome.ACCEPTED,
            response_type=ResponseType.SECRET,
            secret="s3cret",
            remember=True,
        ),
    )
    assert ok.outcome == InteractionOutcome.ACCEPTED
    cancelled = validate_response(
        req,
        InteractionResponse(outcome=InteractionOutcome.CANCELLED, response_type=ResponseType.CANCEL),
    )
    assert cancelled.outcome == InteractionOutcome.CANCELLED


def test_timeout_retry_remember_headless_malformed():
    req = InteractionRequest(kind=PromptKind.PASSWORD, attempt=1)
    assert (
        validate_response(
            req, InteractionResponse(outcome=InteractionOutcome.TIMEOUT)
        ).outcome
        == InteractionOutcome.TIMEOUT
    )
    exhausted = InteractionRequest(kind=PromptKind.PASSWORD, attempt=99)
    assert (
        validate_response(
            exhausted,
            InteractionResponse(
                outcome=InteractionOutcome.ACCEPTED,
                response_type=ResponseType.SECRET,
                secret="x",
            ),
        ).outcome
        == InteractionOutcome.RETRY_EXHAUSTED
    )
    host = InteractionRequest(kind=PromptKind.HOST_KEY, attempt=1)
    remembered = validate_response(
        host,
        InteractionResponse(
            outcome=InteractionOutcome.ACCEPTED,
            response_type=ResponseType.ACCEPT,
            remember=True,
        ),
    )
    assert remembered.remember is False
    assert decide_headless(req).outcome == InteractionOutcome.UNAVAILABLE
    malformed = validate_response(
        req, InteractionResponse(outcome=InteractionOutcome.ACCEPTED, response_type=None)
    )
    assert malformed.outcome == InteractionOutcome.MALFORMED


def test_gi_blocked():
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    script = (
        "import builtins, importlib, sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "real = builtins.__import__\n"
        "builtins.__import__ = lambda name, *a, **k: (_ for _ in ()).throw(ImportError('blocked')) "
        "if name == 'gi' or name.startswith('gi.') else real(name, *a, **k)\n"
        "importlib.import_module('sshpilot.core.interaction')\n"
        "print('OK')\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
