"""'Edit as root' command builders (sudo cat / sudo tee) and the sudo-denial
classifier they rely on. Pure-logic tests — no host or GTK display needed."""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.askpass_utils import is_sudo_denied_error

pytest.importorskip("gi")

from sshpilot.text_editor import RemoteFileEditorWindow as Ed


def test_read_cmd_with_password():
    assert Ed._root_read_cmd("/etc/hosts", has_pw=True) == \
        "sudo -S -p '' -- cat -- /etc/hosts"


def test_read_cmd_passwordless():
    assert Ed._root_read_cmd("/etc/hosts", has_pw=False) == \
        "sudo -n -- cat -- /etc/hosts"


def test_write_cmd_with_password():
    assert Ed._root_write_cmd("/etc/hosts", has_pw=True) == \
        "sudo -S -p '' -- tee -- /etc/hosts > /dev/null"


def test_write_cmd_passwordless():
    assert Ed._root_write_cmd("/etc/hosts", has_pw=False) == \
        "sudo -n -- tee -- /etc/hosts > /dev/null"


def test_cmd_quotes_paths_with_spaces_and_dashes():
    # shlex.quote guards spaces; the leading `--` guards dash-prefixed paths.
    assert Ed._root_read_cmd("/tmp/a b", has_pw=False) == \
        "sudo -n -- cat -- '/tmp/a b'"
    assert "-- -weird" in Ed._root_write_cmd("-weird", has_pw=False)


@pytest.mark.parametrize("text,expected", [
    ("webuser is not in the sudoers file", True),
    ("user is not allowed to execute '/usr/bin/tee'", True),
    ("sudo: unknown user: ghost", True),
    ("sudo: a password is required", False),
    ("Sorry, try again.", False),
    ("", False),
])
def test_is_sudo_denied_error(text, expected):
    assert is_sudo_denied_error(text) is expected
