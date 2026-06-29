"""Tests for the .spbk container (sshpilot/backup_archive.py)."""

import os

import pytest

import sshpilot.backup_archive as A


def test_encrypted_roundtrip(tmp_path):
    manifest = {"version": 1, "credentials": [{"id": "u@h", "secret": "pw"}]}
    path = str(tmp_path / "a.spbk")
    A.write_spbk(path, manifest, "correct horse")
    assert A.is_spbk(path) is True
    assert A.spbk_is_encrypted(path) is True
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert A.read_spbk(path, "correct horse") == manifest


def test_plaintext_roundtrip(tmp_path):
    manifest = {"version": 1, "credentials": []}
    path = str(tmp_path / "b.spbk")
    A.write_spbk(path, manifest, None)
    assert A.is_spbk(path) is True
    assert A.spbk_is_encrypted(path) is False
    assert A.read_spbk(path) == manifest          # no passphrase needed


def test_wrong_passphrase_raises(tmp_path):
    path = str(tmp_path / "c.spbk")
    A.write_spbk(path, {"x": 1}, "right")
    with pytest.raises(A.SpbkPassphraseError):
        A.read_spbk(path, "wrong")


def test_encrypted_requires_passphrase(tmp_path):
    path = str(tmp_path / "d.spbk")
    A.write_spbk(path, {"x": 1}, "pw")
    with pytest.raises(A.SpbkPassphraseError):
        A.read_spbk(path, None)


def test_tamper_detected(tmp_path):
    path = str(tmp_path / "e.spbk")
    A.write_spbk(path, {"x": 1}, "pw")
    with open(path, "r+b") as f:                  # flip the last ciphertext byte
        f.seek(-1, os.SEEK_END)
        last = f.read(1)
        f.seek(-1, os.SEEK_END)
        f.write(bytes([last[0] ^ 0xFF]))
    with pytest.raises(A.SpbkPassphraseError):
        A.read_spbk(path, "pw")


def test_bad_magic(tmp_path):
    path = str(tmp_path / "f.json")
    with open(path, "wb") as f:
        f.write(b'{"version": 1}')
    assert A.is_spbk(path) is False
    with pytest.raises(A.SpbkFormatError):
        A.read_spbk(path)


def test_truncated_header(tmp_path):
    path = str(tmp_path / "g.spbk")
    with open(path, "wb") as f:
        f.write(A.MAGIC + b'{"v":1')              # no newline terminator
    with pytest.raises(A.SpbkFormatError):
        A.read_spbk(path)
