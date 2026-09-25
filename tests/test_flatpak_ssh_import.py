"""Flatpak SSH key/cert import into ``~/.ssh``."""

from __future__ import annotations

import os
import stat

import pytest

from sshpilot import flatpak_ssh_import as imp


def _write(path: str, body: str = "key-material\n", mode: int = 0o600) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, mode)
    return path


def test_needs_import_false_when_not_flatpak(tmp_path):
    key = _write(str(tmp_path / "elsewhere" / "id_ed25519"))
    assert imp.needs_flatpak_ssh_import(key, flatpak=False, ssh_dir=str(tmp_path / ".ssh")) is False


def test_needs_import_false_when_already_under_ssh(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    key = _write(str(ssh_dir / "id_ed25519"))
    assert imp.needs_flatpak_ssh_import(key, flatpak=True, ssh_dir=str(ssh_dir)) is False


def test_needs_import_true_for_portal_or_outside_path(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    outside = _write(str(tmp_path / "doc" / "ABC" / "id_ed25519"))
    assert imp.needs_flatpak_ssh_import(outside, flatpak=True, ssh_dir=str(ssh_dir)) is True


def test_companion_paths_find_pub_and_cert(tmp_path):
    key = _write(str(tmp_path / "id_ed25519"))
    pub = _write(str(tmp_path / "id_ed25519.pub"), "ssh-ed25519 AAAA comment\n")
    cert = _write(str(tmp_path / "id_ed25519-cert.pub"), "ssh-ed25519-cert AAAA\n")
    assert imp.companion_paths_for_private_key(key) == [pub, cert]


def test_companion_paths_skip_when_primary_is_pub(tmp_path):
    pub = _write(str(tmp_path / "id_ed25519.pub"), "ssh-ed25519 AAAA\n")
    assert imp.companion_paths_for_private_key(pub) == []


def test_import_private_key_copies_companions_and_sets_mode(tmp_path):
    src_dir = tmp_path / "doc" / "XYZ"
    key = _write(str(src_dir / "work"), "PRIVATE\n")
    _write(str(src_dir / "work.pub"), "PUBLIC\n")
    _write(str(src_dir / "work-cert.pub"), "CERT\n")
    ssh_dir = tmp_path / ".ssh"

    dest, companions = imp.import_private_key_into_ssh_dir(key, ssh_dir=str(ssh_dir))

    assert dest == str(ssh_dir / "work")
    assert set(companions) == {str(ssh_dir / "work.pub"), str(ssh_dir / "work-cert.pub")}
    assert open(dest, encoding="utf-8").read() == "PRIVATE\n"
    assert open(ssh_dir / "work.pub", encoding="utf-8").read() == "PUBLIC\n"
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600


def test_import_renames_on_collision(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    _write(str(ssh_dir / "id_ed25519"), "existing\n")
    src = _write(str(tmp_path / "portal" / "id_ed25519"), "new\n")
    _write(str(tmp_path / "portal" / "id_ed25519.pub"), "newpub\n")

    dest, companions = imp.import_private_key_into_ssh_dir(src, ssh_dir=str(ssh_dir))

    assert dest == str(ssh_dir / "id_ed25519-1")
    assert companions == [str(ssh_dir / "id_ed25519-1.pub")]
    assert open(dest, encoding="utf-8").read() == "new\n"
    assert open(ssh_dir / "id_ed25519", encoding="utf-8").read() == "existing\n"


def test_import_certificate_copies_only_primary(tmp_path):
    src_dir = tmp_path / "portal"
    cert = _write(str(src_dir / "id_ed25519-cert.pub"), "CERT\n")
    _write(str(src_dir / "id_ed25519"), "PRIVATE\n")  # must not be pulled in
    ssh_dir = tmp_path / ".ssh"

    dest, companions = imp.import_certificate_into_ssh_dir(cert, ssh_dir=str(ssh_dir))

    assert dest == str(ssh_dir / "id_ed25519-cert.pub")
    assert companions == []
    assert not (ssh_dir / "id_ed25519").exists()


def test_import_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        imp.import_private_key_into_ssh_dir(
            str(tmp_path / "missing"), ssh_dir=str(tmp_path / ".ssh")
        )
