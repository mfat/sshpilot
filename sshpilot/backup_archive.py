"""The ``.spbk`` sshPilot backup container: an optionally-encrypted archive of a backup
manifest (config + credentials).

Layout (binary):

    SPBK1\\n                      # 6-byte magic
    <header-json>\\n              # one cleartext line; the GCM associated-data when encrypted
    <payload>                    # inner zip (manifest.json), AES-256-GCM-encrypted iff header.enc

The header records the KDF + AEAD parameters so a reader can derive the key from a passphrase:

    {"v":1, "enc": {"algo":"AES-256-GCM","kdf":"scrypt","n":..,"r":8,"p":1,
                    "salt":<b64>,"nonce":<b64>}}      # or  {"v":1,"enc":null} for plaintext

Crypto uses only the already-present ``cryptography`` dependency (scrypt + AES-GCM); GTK-free
and importable anywhere. The inner zip is read by member name (``manifest.json``) so there is no
zip-slip surface.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import zipfile
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"SPBK1\n"
FORMAT_VERSION = 1
_MANIFEST_NAME = "manifest.json"

# scrypt work factor — memory-hard; n=2**15 is a common interactive default.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32          # AES-256
_SALT_LEN = 16
_NONCE_LEN = 12

# Hard ceilings enforced when READING a (possibly hostile) file, so a crafted header can't
# turn "open this backup" into a memory-hard DoS. scrypt memory ≈ 128 * n * r bytes, so the
# n cap below bounds a single derivation to ~1 GiB even at the max r; legitimate backups use
# n=2**15 (~32 MiB). Kept generous enough to allow raising the work factor later.
_MAX_SCRYPT_N = 2 ** 20
_MAX_SCRYPT_R = 32
_MAX_SCRYPT_P = 16
_MAX_SALT_LEN = 1024
_MAX_NONCE_LEN = 64
# Ceiling on the inflated manifest, so a zip-bomb payload can't exhaust memory on read.
# A real manifest is a few MiB at most (JSON + base64 keys); 64 MiB is comfortable headroom.
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024


class SpbkError(Exception):
    """Base error for ``.spbk`` handling."""


class SpbkFormatError(SpbkError):
    """Not a ``.spbk`` file, or its header/payload is malformed."""


class SpbkPassphraseError(SpbkError):
    """Missing or wrong passphrase (or the file was tampered with)."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive_key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=_KEY_LEN, n=n, r=r, p=p).derive(
        (passphrase or "").encode("utf-8"))


def _zip_manifest(manifest: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_MANIFEST_NAME, json.dumps(manifest).encode("utf-8"))
    return buf.getvalue()


def _unzip_manifest(raw: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            info = zf.getinfo(_MANIFEST_NAME)        # read by name only — no extractall / zip-slip
            if info.file_size > _MAX_MANIFEST_BYTES:  # refuse a zip-bomb before inflating it
                raise SpbkFormatError(
                    f"manifest too large ({info.file_size} bytes); refusing to inflate")
            with zf.open(info) as member:
                data = member.read(_MAX_MANIFEST_BYTES + 1)  # bounded read guards a lying header
            if len(data) > _MAX_MANIFEST_BYTES:
                raise SpbkFormatError("manifest exceeds the maximum allowed size")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise SpbkFormatError(f"corrupt or unexpected archive contents: {exc}") from exc
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SpbkFormatError(f"invalid manifest JSON: {exc}") from exc


def _atomic_write_bytes(path: str, data: bytes, mode: int = 0o600) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".spbk-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_spbk(path: str, manifest: dict, passphrase: Optional[str] = None) -> None:
    """Write ``manifest`` to ``path`` as a ``.spbk`` file. Encrypted with ``passphrase`` (scrypt
    + AES-256-GCM) when one is given, else plaintext. The file is written atomically, mode 600."""
    inner = _zip_manifest(manifest)
    if passphrase:
        salt = os.urandom(_SALT_LEN)
        nonce = os.urandom(_NONCE_LEN)
        key = _derive_key(passphrase, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
        header = {"v": FORMAT_VERSION, "enc": {
            "algo": "AES-256-GCM", "kdf": "scrypt",
            "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
            "salt": _b64e(salt), "nonce": _b64e(nonce)}}
        header_bytes = json.dumps(header).encode("utf-8")
        payload = AESGCM(key).encrypt(nonce, inner, header_bytes)   # header = associated data
    else:
        header = {"v": FORMAT_VERSION, "enc": None}
        header_bytes = json.dumps(header).encode("utf-8")
        payload = inner
    _atomic_write_bytes(path, MAGIC + header_bytes + b"\n" + payload)


def _split(path: str):
    """Return ``(header_dict, header_bytes, payload_bytes)`` or raise SpbkFormatError."""
    with open(path, "rb") as f:
        blob = f.read()
    if not blob.startswith(MAGIC):
        raise SpbkFormatError("not a .spbk file (bad magic)")
    rest = blob[len(MAGIC):]
    nl = rest.find(b"\n")
    if nl < 0:
        raise SpbkFormatError("truncated .spbk header")
    header_bytes = rest[:nl]
    payload = rest[nl + 1:]
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SpbkFormatError(f"invalid .spbk header: {exc}") from exc
    if not isinstance(header, dict):
        raise SpbkFormatError("invalid .spbk header")
    version = header.get("v")
    if not isinstance(version, int) or isinstance(version, bool) or version > FORMAT_VERSION:
        raise SpbkFormatError(f"unsupported .spbk format version: {version!r}")
    return header, header_bytes, payload


def _is_power_of_two(value: int) -> bool:
    return value > 1 and (value & (value - 1)) == 0


def _validate_enc_params(salt: bytes, nonce: bytes, n: int, r: int, p: int) -> None:
    """Reject out-of-range KDF/AEAD parameters from a (still-unauthenticated) header BEFORE the
    key is derived, so a hostile ``n`` can't trigger a memory-hard DoS on open."""
    if not (_is_power_of_two(n) and n <= _MAX_SCRYPT_N):
        raise SpbkFormatError(f"scrypt n out of range: {n!r}")
    if not (1 <= r <= _MAX_SCRYPT_R):
        raise SpbkFormatError(f"scrypt r out of range: {r!r}")
    if not (1 <= p <= _MAX_SCRYPT_P):
        raise SpbkFormatError(f"scrypt p out of range: {p!r}")
    if not (8 <= len(salt) <= _MAX_SALT_LEN):
        raise SpbkFormatError(f"invalid salt length: {len(salt)}")
    if not (1 <= len(nonce) <= _MAX_NONCE_LEN):
        raise SpbkFormatError(f"invalid nonce length: {len(nonce)}")


def is_spbk(path: str) -> bool:
    """True if ``path`` looks like a ``.spbk`` file (magic check; no decryption)."""
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def spbk_is_encrypted(path: str) -> bool:
    """Whether the ``.spbk`` at ``path`` is encrypted (so the caller knows to prompt)."""
    header, _hb, _pl = _split(path)
    return bool(header.get("enc"))


def read_spbk(path: str, passphrase: Optional[str] = None) -> dict:
    """Read and return the manifest dict from a ``.spbk`` file. For an encrypted file, supply
    ``passphrase``; a wrong/missing passphrase (or tampering) raises :class:`SpbkPassphraseError`.
    A non-``.spbk`` or malformed file raises :class:`SpbkFormatError`."""
    header, header_bytes, payload = _split(path)
    enc = header.get("enc")
    if not enc:
        return _unzip_manifest(payload)
    if not passphrase:
        raise SpbkPassphraseError("this backup is encrypted; a passphrase is required")
    try:
        salt = _b64d(enc["salt"])
        nonce = _b64d(enc["nonce"])
        n, r, p = int(enc["n"]), int(enc["r"]), int(enc["p"])
    except (KeyError, ValueError, TypeError) as exc:
        raise SpbkFormatError(f"invalid encryption header: {exc}") from exc
    # Validate the (still-unauthenticated) work factors BEFORE deriving — the GCM tag that would
    # catch tampering is only checked afterwards, so an unbounded n would DoS us before then.
    _validate_enc_params(salt, nonce, n, r, p)
    key = _derive_key(passphrase, salt, n, r, p)
    try:
        inner = AESGCM(key).decrypt(nonce, payload, header_bytes)
    except InvalidTag as exc:
        raise SpbkPassphraseError("wrong passphrase or corrupted backup") from exc
    return _unzip_manifest(inner)
