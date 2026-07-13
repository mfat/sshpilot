from pathlib import Path


from sshpilot.key_utils import _is_private_key


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_is_private_key_rejects_invalid_format(tmp_path):
    key_path = _write(tmp_path, "invalid", "not a key\n")
    assert not _is_private_key(key_path)


def test_is_private_key_accepts_openssh_key(tmp_path):
    key_path = _write(
        tmp_path, "id_ed25519",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA...\n-----END OPENSSH PRIVATE KEY-----\n",
    )
    assert _is_private_key(key_path)


def test_is_private_key_accepts_encrypted_pem_key(tmp_path):
    # Encrypted keys still carry the armor header — the sniff must accept them
    # (the old ssh-keygen path relied on parsing the passphrase error).
    key_path = _write(
        tmp_path, "id_rsa",
        "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n...\n"
        "-----END RSA PRIVATE KEY-----\n",
    )
    assert _is_private_key(key_path)


def test_is_private_key_skips_pub_and_known_files(tmp_path):
    pub = _write(tmp_path, "id_ed25519.pub", "ssh-ed25519 AAAA... user@host\n")
    known = _write(tmp_path, "known_hosts", "host ssh-ed25519 AAAA...\n")
    assert not _is_private_key(pub)
    assert not _is_private_key(known)
