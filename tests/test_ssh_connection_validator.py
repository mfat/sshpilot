"""Unit tests for SSHConnectionValidator / ValidationResult.

These were extracted verbatim from connection_dialog.py into the leaf module
sshpilot/ssh_connection_validator.py. They validate user-entered form fields and
never open an SSH connection. Existing suites (test_duplicate_nickname,
test_ssh_config_tokenization) already import SSHConnectionValidator from
connection_dialog (back-compat re-export); this file covers the validation rules
directly, which had no focused coverage before.
"""

from sshpilot.ssh_connection_validator import (
    SSHConnectionValidator,
    ValidationResult,
)


def test_reexport_from_connection_dialog_still_works():
    # back-compat: tests and external callers import it from connection_dialog
    from sshpilot.connection_dialog import SSHConnectionValidator as FromDialog
    assert FromDialog is SSHConnectionValidator


class TestConnectionName:
    def setup_method(self):
        self.v = SSHConnectionValidator()

    def test_required(self):
        assert self.v.validate_connection_name("").is_valid is False
        assert self.v.validate_connection_name("   ").severity == "error"

    def test_no_whitespace(self):
        assert self.v.validate_connection_name("my host").is_valid is False

    def test_duplicate_rejected(self):
        self.v.set_existing_names({"Prod"})
        assert self.v.validate_connection_name("prod").is_valid is False

    def test_valid(self):
        assert self.v.validate_connection_name("prod-1").is_valid is True


class TestHostname:
    def setup_method(self):
        self.v = SSHConnectionValidator()

    def test_required_unless_allowed_empty(self):
        assert self.v.validate_hostname("").is_valid is False
        assert self.v.validate_hostname("", allow_empty=True).is_valid is True

    def test_private_ip(self):
        r = self.v.validate_hostname("192.168.1.10")
        assert r.is_valid is True and r.severity == "info"

    def test_loopback(self):
        assert self.v.validate_hostname("127.0.0.1").is_valid is True

    def test_invalid_numeric_ip(self):
        assert self.v.validate_hostname("999.1.1.1").is_valid is False

    def test_consecutive_dots_rejected(self):
        assert self.v.validate_hostname("a..b.com").is_valid is False

    def test_fqdn_valid(self):
        assert self.v.validate_hostname("example.com").is_valid is True

    def test_bare_hostname_warns(self):
        r = self.v.validate_hostname("server")
        assert r.is_valid is True and r.severity == "warning"


class TestPort:
    def setup_method(self):
        self.v = SSHConnectionValidator()

    def test_required(self):
        assert self.v.validate_port("").is_valid is False

    def test_non_numeric(self):
        assert self.v.validate_port("abc").is_valid is False

    def test_out_of_range(self):
        assert self.v.validate_port("0").is_valid is False
        assert self.v.validate_port("70000").is_valid is False

    def test_standard_ssh_port_info(self):
        r = self.v.validate_port("22", context="SSH")
        assert r.is_valid is True and r.severity == "info"

    def test_unusual_for_ssh_warns(self):
        r = self.v.validate_port("80", context="SSH")
        assert r.is_valid is True and r.severity == "warning"


class TestUsername:
    def test_required_then_valid(self):
        v = SSHConnectionValidator()
        assert v.validate_username("").is_valid is False
        assert v.validate_username("alice").is_valid is True


class TestValidationResultDefaults:
    def test_defaults(self):
        r = ValidationResult()
        assert (r.is_valid, r.message, r.severity) == (True, "", "info")
