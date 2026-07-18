"""Classification of SSH auth-failure stderr / messages."""

from sshpilot.ssh_utils import is_ssh_auth_failure_text


def test_auth_failure_markers():
    assert is_ssh_auth_failure_text(
        "root@host: Permission denied (publickey,password)."
    )
    assert is_ssh_auth_failure_text("Permission denied, please try again.")
    assert is_ssh_auth_failure_text("Authentication failed")
    assert is_ssh_auth_failure_text("Too many authentication failures")


def test_non_auth_errors():
    # Remote *file* permission error, not an auth failure.
    assert not is_ssh_auth_failure_text("scp: /root/secret: Permission denied")
    assert not is_ssh_auth_failure_text("Connection refused")
    assert not is_ssh_auth_failure_text("No such file or directory")
    assert not is_ssh_auth_failure_text("")
    assert not is_ssh_auth_failure_text(None)
