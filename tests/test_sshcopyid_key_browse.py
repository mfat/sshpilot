"""Tests for resolving a browsed public-key file into an SSHKey.

ssh-copy-id installs the *public* key (``-i <pub>``), so a file chosen via the
browse dialog must yield an SSHKey whose ``public_path`` is exactly that file,
regardless of extension.
"""
import os

from sshpilot.sshcopyid_window import _ssh_key_from_public_path


def test_pub_file_uses_exact_public_path():
    path = "/home/alice/keys/server.pub"
    key = _ssh_key_from_public_path(path)
    assert key.public_path == path
    assert key.private_path == "/home/alice/keys/server"  # .pub stripped


def test_non_pub_file_keeps_exact_public_path():
    """A chosen file without a .pub suffix still becomes the public_path verbatim."""
    path = "/home/alice/keys/id_custom"
    key = _ssh_key_from_public_path(path)
    assert key.public_path == path
    assert key.private_path == path


def test_dropdown_label_matches_private_basename():
    key = _ssh_key_from_public_path("/tmp/elsewhere/work.pub")
    # The sidebar/dropdown label uses basename(private_path) for both discovered
    # and browsed keys, so it stays consistent.
    assert os.path.basename(key.private_path) == "work"
