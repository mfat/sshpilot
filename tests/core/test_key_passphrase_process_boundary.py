"""Secret-free native process boundaries for SSH-key operations."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import sshpilot.core.keys.service as key_service_module
from sshpilot.core.errors import CoreError
from sshpilot.core.keys.service import KeyGenerateSpec, KeyService

SENTINEL = "KEY_PASSPHRASE_SENTINEL_8F1C29"


def _fake_successful_keygen(calls):
    def run(argv, **kwargs):
        argv = list(argv)
        calls.append((argv, kwargs))
        if "-f" in argv and "-y" not in argv:
            path = key_service_module.Path(argv[argv.index("-f") + 1])
            path.write_text("private")
            path.with_suffix(path.suffix + ".pub").write_text(
                "ssh-ed25519 AAAA-generated\n"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_unencrypted_generation_uses_only_literal_empty_n_value(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        key_service_module.subprocess,
        "run",
        _fake_successful_keygen(calls),
    )

    KeyService(tmp_path).generate_key(
        KeyGenerateSpec(key_name="id_ed25519", encrypted=False)
    )

    argv, kwargs = calls[0]
    assert argv[argv.index("-N") + 1] == ""
    assert "-P" not in argv
    assert SENTINEL not in argv
    assert kwargs["env"] is None


def test_encrypted_generation_omits_secret_from_argv_and_environment(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        key_service_module.subprocess,
        "run",
        _fake_successful_keygen(calls),
    )
    secret = bytearray(SENTINEL.encode())

    def prepare(argv):
        assert SENTINEL not in argv
        assert "-N" not in argv
        assert "-P" not in argv
        return tuple(argv), {
            "SSH_ASKPASS": "/opaque/askpass",
            "SSHPILOT_DAEMON_ASKPASS_TOKEN": "opaque-token",
        }

    try:
        KeyService(tmp_path).generate_key(
            KeyGenerateSpec(key_name="id_ed25519", encrypted=True),
            prepare_launch=prepare,
        )
    finally:
        secret[:] = b"\0" * len(secret)
        secret.clear()

    argv, kwargs = calls[0]
    assert "-N" not in argv
    assert "-P" not in argv
    assert SENTINEL not in argv
    assert SENTINEL not in kwargs["env"].values()
    assert secret == bytearray()


def test_passphrase_verification_omits_p_and_secret_environment(
    tmp_path, monkeypatch
):
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("private")
    calls = []
    monkeypatch.setattr(
        key_service_module.subprocess,
        "run",
        _fake_successful_keygen(calls),
    )

    valid = KeyService(tmp_path).verify_key_passphrase(
        key_path,
        prepare_launch=lambda argv: (
            tuple(argv),
            {
                "SSH_ASKPASS": "/opaque/askpass",
                "SSHPILOT_DAEMON_ASKPASS_TOKEN": "opaque-token",
            },
        ),
    )

    assert valid is True
    argv, kwargs = calls[0]
    assert argv == ["ssh-keygen", "-y", "-f", str(key_path)]
    assert "-N" not in argv
    assert "-P" not in argv
    assert SENTINEL not in argv
    assert SENTINEL not in kwargs["env"].values()


def test_keygen_stderr_is_not_exposed_to_frontend_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        key_service_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"failure {SENTINEL}",
        ),
    )

    with pytest.raises(CoreError) as excinfo:
        KeyService(tmp_path).generate_key(
            KeyGenerateSpec(key_name="id_ed25519", encrypted=False)
        )

    assert SENTINEL not in str(excinfo.value)
