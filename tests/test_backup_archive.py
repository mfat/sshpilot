"""Tests for the .spbk container (sshpilot/backup_archive.py)."""

import base64
import io
import json
import os
import zipfile

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


def _write_raw_spbk(path, header, payload=b""):
    with open(path, "wb") as f:
        f.write(A.MAGIC + json.dumps(header).encode("utf-8") + b"\n" + payload)


def test_oversized_scrypt_n_rejected_before_derivation(tmp_path, monkeypatch):
    """A crafted header with a huge n must raise BEFORE scrypt runs (DoS-on-open guard)."""
    path = str(tmp_path / "dos.spbk")
    _write_raw_spbk(path, {"v": 1, "enc": {
        "algo": "AES-256-GCM", "kdf": "scrypt",
        "n": 2 ** 30, "r": 8, "p": 1,
        "salt": base64.b64encode(os.urandom(16)).decode(),
        "nonce": base64.b64encode(os.urandom(12)).decode()}})

    def _boom(*_a, **_k):                          # scrypt must never be reached
        raise AssertionError("KDF derivation attempted on an out-of-range n")

    monkeypatch.setattr(A, "_derive_key", _boom)
    with pytest.raises(A.SpbkFormatError):
        A.read_spbk(path, "whatever")


@pytest.mark.parametrize("enc", [
    {"n": 3, "r": 8, "p": 1},                      # n not a power of two
    {"n": 2 ** 15, "r": 999, "p": 1},              # r too large
    {"n": 2 ** 15, "r": 8, "p": 999},              # p too large
])
def test_out_of_range_params_rejected(tmp_path, enc):
    path = str(tmp_path / "bad.spbk")
    _write_raw_spbk(path, {"v": 1, "enc": {
        "algo": "AES-256-GCM", "kdf": "scrypt",
        "salt": base64.b64encode(os.urandom(16)).decode(),
        "nonce": base64.b64encode(os.urandom(12)).decode(), **enc}})
    with pytest.raises(A.SpbkFormatError):
        A.read_spbk(path, "pw")


def test_unsupported_header_version_rejected(tmp_path):
    path = str(tmp_path / "v2.spbk")
    _write_raw_spbk(path, {"v": A.FORMAT_VERSION + 1, "enc": None}, A._zip_manifest({"x": 1}))
    with pytest.raises(A.SpbkFormatError):
        A.read_spbk(path)


def test_zip_bomb_manifest_rejected(tmp_path):
    """A highly-compressible oversized manifest must be refused, not inflated into memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", b"\0" * (A._MAX_MANIFEST_BYTES + 1))
    path = str(tmp_path / "bomb.spbk")
    _write_raw_spbk(path, {"v": 1, "enc": None}, buf.getvalue())
    with pytest.raises(A.SpbkFormatError):
        A.read_spbk(path)
