"""Welcome page empty-state copy for Recent."""

from sshpilot.welcome_page import WelcomePage


def test_empty_recent_message_no_connection_rows():
    assert WelcomePage._empty_recent_message(False) == (
        'Press + to create a new connection'
    )


def test_empty_recent_message_with_connection_rows():
    assert WelcomePage._empty_recent_message(True) == (
        'Double click a host to connect or press + to create a new connection'
    )


def test_recent_read_error_message_is_safe_and_non_blocking():
    assert WelcomePage._recent_read_error_message() == (
        'Recent connections are temporarily unavailable'
    )
