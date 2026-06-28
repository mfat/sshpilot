import subprocess
from pathlib import Path

import pytest

from sshpilot.key_utils import _is_private_key


class DummyCompletedProcess(subprocess.CompletedProcess):
    def __init__(self, args, returncode=0, stdout="", stderr=""):
        super().__init__(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def _touch_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text("dummy", encoding="utf-8")
    return path


def test_is_private_key_rejects_invalid_format(tmp_path, monkeypatch):
    key_path = _touch_file(tmp_path, "invalid")

    def fake_run(cmd, capture_output, text, check):
        assert "-y" in cmd and "-f" in cmd and "-P" in cmd
        return DummyCompletedProcess(
            cmd,
            returncode=1,
            stdout="",
            stderr=f'Load key "{key_path}": invalid format\n',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert not _is_private_key(key_path)


def test_is_private_key_accepts_passphrase_error(tmp_path, monkeypatch):
    key_path = _touch_file(tmp_path, "passphrase")

    def fake_run(cmd, capture_output, text, check):
        return DummyCompletedProcess(
            cmd,
            returncode=1,
            stdout="",
            stderr=f'Load key "{key_path}": incorrect passphrase supplied\n',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _is_private_key(key_path)
