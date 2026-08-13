"""Unit tests for the trusted OpenSSH diagnostics parser."""

from __future__ import annotations

from sshpilot.core.ssh_diagnostics import (
    SshDiagnosticParser,
    SshDiagnosticState,
)


def _feed(*chunks: str):
    parser = SshDiagnosticParser()
    result = None
    for chunk in chunks:
        result = parser.feed(chunk.encode("utf-8"))
        if result is not None:
            break
    if result is None:
        result = parser.close()
    return result


def test_direct_publickey_authentication_marker():
    result = _feed(
        "debug1: KEX: host key algorithm: ssh-ed25519\n"
        'debug1: Authenticated to example.test ([127.0.0.1]:22) '
        'using "publickey".\n'
        "debug1: channel 0: new [client-session]\n"
    )
    assert result is not None
    assert result.state is SshDiagnosticState.AUTHENTICATED


def test_keyboard_interactive_authentication_marker():
    result = _feed(
        "debug1: Authentications that can continue: "
        "publickey,keyboard-interactive,password\n"
        'debug1: Authenticated to gateway.example ([10.0.0.1]:2222) '
        'using "keyboard-interactive".\n'
    )
    assert result is not None
    assert result.state is SshDiagnosticState.AUTHENTICATED


def test_controlmaster_client_session_marker():
    result = _feed(
        "debug1: Connection to master at /run/user/1000/sshpilot/cm/abc "
        "running pid 1234 using control socket\n"
        "debug1: mux_client_request_session: master session id: 7\n"
        "debug1: channel 0: new [client-session]\n"
    )
    assert result is not None
    assert result.state is SshDiagnosticState.MUX_SESSION_OPENED


def test_connection_refused_is_failed():
    result = _feed("ssh: connect to host example.test port 22: Connection refused\n")
    assert result is not None
    assert result.state is SshDiagnosticState.FAILED
    assert "Connection refused" in result.detail


def test_no_route_to_host_is_failed():
    result = _feed("ssh: connect to host 192.0.2.1 port 22: No route to host\n")
    assert result is not None
    assert result.state is SshDiagnosticState.FAILED
    assert "No route to host" in result.detail


def test_unknown_host_is_failed():
    result = _feed(
        "ssh: Could not resolve hostname does-not-exist: "
        "Name or service not known\n"
    )
    assert result is not None
    assert result.state is SshDiagnosticState.FAILED
    assert "could not resolve" in result.detail.lower()


def test_host_key_verification_failed_is_failed():
    result = _feed(
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
        "Host key verification failed.\n"
    )
    assert result is not None
    assert result.state is SshDiagnosticState.FAILED
    assert "Host key verification failed" in result.detail


def test_connection_timed_out_is_failed():
    result = _feed("ssh: connect to host example.test port 22: Connection timed out\n")
    assert result is not None
    assert result.state is SshDiagnosticState.FAILED
    assert "timed out" in result.detail


def test_terminal_permission_denied_is_failed():
    result = _feed(
        "debug1: Authentications that can continue: publickey,password\n"
        "debug1: No more authentication methods to try.\n"
        "alice@example.test: Permission denied (publickey,password).\n"
    )
    assert result is not None
    assert result.state is SshDiagnosticState.FAILED
    assert "Permission denied" in result.detail


def test_retriable_permission_denied_stays_pending():
    # A wrong first password must not kill a session that may still succeed.
    result = _feed("Permission denied, please try again.\n")
    assert result is None
    result = _feed(
        "Permission denied, please try again.\n"
        'debug1: Authenticated to example.test ([127.0.0.1]:22) '
        'using "password".\n'
    )
    assert result is not None
    assert result.state is SshDiagnosticState.AUTHENTICATED


def test_debug_noise_and_prompts_stay_pending():
    result = _feed(
        "debug1: Connecting to example.test [127.0.0.1] port 22.\n"
        "debug1: kex: algorithm: curve25519-sha256\n"
        "debug1: Server host key: ssh-ed25519 SHA256:AAAA\n"
        "debug1: Next authentication method: password\n"
        "debug1: Authentications that can continue: publickey,password\n"
    )
    assert result is None
    assert _feed("") is None


def test_partial_lines_are_retained_across_chunks():
    parser = SshDiagnosticParser()
    assert parser.feed(b"debug1: Authenticated to exa") is None
    assert parser.feed(b'mple.test ([127.0.0.1]:22) using "pub') is None
    result = parser.feed(b'lickey".\n')
    assert result is not None
    assert result.state is SshDiagnosticState.AUTHENTICATED


def test_trailing_unterminated_line_is_flushed_on_close():
    parser = SshDiagnosticParser()
    assert parser.feed(b"ssh: connect to host x port 22: Connection refused") is None
    result = parser.close()
    assert result is not None
    assert result.state is SshDiagnosticState.FAILED


def test_decisive_result_latches_and_ignores_later_input():
    parser = SshDiagnosticParser()
    first = parser.feed(
        b'debug1: Authenticated to example.test ([127.0.0.1]:22) '
        b'using "publickey".\n'
    )
    assert first is not None
    assert first.state is SshDiagnosticState.AUTHENTICATED
    assert parser.feed(b"ssh: connect to host x port 22: Connection refused\n") is None
    assert parser.close() is None


def test_multiple_failure_markers_report_first():
    result = _feed(
        "debug1: connect to address 127.0.0.1 port 22: Connection refused\n"
        "debug1: Trying again\n"
        "ssh: connect to host example.test port 22: Connection refused\n"
    )
    assert result is not None
    assert result.state is SshDiagnosticState.FAILED
