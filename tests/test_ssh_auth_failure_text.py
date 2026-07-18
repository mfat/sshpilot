"""Classification of SSH auth-failure stderr / messages."""

from sshpilot.ssh_utils import clean_ssh_stderr, is_ssh_auth_failure_text


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


def test_clean_ssh_stderr_strips_debug_chatter():
    raw = (
        "debug1: Reading configuration data /etc/ssh/ssh_config\n"
        "debug2: resolving host\n"
        "  debug3: indented debug line\n"
        "root@host: Permission denied (publickey,keyboard-interactive).\n"
    )
    cleaned = clean_ssh_stderr(raw)
    assert "debug" not in cleaned
    assert cleaned == "root@host: Permission denied (publickey,keyboard-interactive)."


def test_clean_ssh_stderr_empty_when_only_debug():
    assert clean_ssh_stderr("debug1: foo\ndebug1: bar\n") == ""
    assert clean_ssh_stderr("") == ""
    assert clean_ssh_stderr(None) == ""
