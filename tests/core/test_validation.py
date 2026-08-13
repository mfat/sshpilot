"""Connection / forwarding validation and error-code stability."""
from __future__ import annotations

from sshpilot.core.errors import ErrorCode
from sshpilot.core.forwards import validate_forwarding_rule
from sshpilot.core.validation import SSHConnectionValidator


def test_error_codes_stable():
    assert ErrorCode.VALIDATION_ERROR.value == "VALIDATION_ERROR"
    assert ErrorCode.SECRET_LOCKED.value == "SECRET_LOCKED"
    assert ErrorCode.KNOWN_HOST_CONFLICT.value == "KNOWN_HOST_CONFLICT"


def test_connection_name_rejects_whitespace():
    v = SSHConnectionValidator()
    result = v.validate_connection_name("Bad Name")
    assert result.is_valid is False


def test_hostname_and_port_ok():
    v = SSHConnectionValidator()
    assert v.validate_hostname("example.com").is_valid
    assert v.validate_port("22").is_valid
    assert v.validate_username("alice").is_valid


def test_forwarding_rule_requires_dest_for_local():
    errors = validate_forwarding_rule(
        {"type": "local", "listen_port": 8080, "remote_host": "", "remote_port": 80}
    )
    assert any(e.field == "remote_host" for e in errors)


def test_dynamic_forward_skips_remote_host():
    errors = validate_forwarding_rule(
        {"type": "dynamic", "listen_port": 1080}
    )
    assert errors == []


def test_remote_socks_and_listen_addr_alias():
    errors = validate_forwarding_rule(
        {"type": "remote", "listen_addr": "", "listen_port": 8080, "socks": True}
    )
    assert errors == []
    errors = validate_forwarding_rule(
        {
            "type": "remote",
            "listen_addr": "0.0.0.0",
            "listen_port": 9000,
            "local_host": "127.0.0.1",
            "local_port": 22,
        }
    )
    assert errors == []
