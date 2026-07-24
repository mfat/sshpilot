"""Tests for the ConnectionDialog inline-validation mixin.

The inline form-field validation logic was extracted verbatim from
connection_dialog.py into the ConnectionDialogValidationMixin
(sshpilot/connection_dialog_validation.py). These tests guard the extraction
contract (ConnectionDialog still inherits every method) and exercise the pure
validation rules through a lightweight fake row — they never open an SSH
connection and don't need real GTK (the suite stubs gi).
"""

from sshpilot.connection_dialog import ConnectionDialog
from sshpilot.connection_dialog_validation import ConnectionDialogValidationMixin
from sshpilot.ssh_connection_validator import SSHConnectionValidator


def test_connection_dialog_inherits_the_validation_mixin():
    assert issubclass(ConnectionDialog, ConnectionDialogValidationMixin)


def test_moved_methods_are_owned_by_the_mixin():
    for name in (
        "_validate_field_row",
        "_is_nickname_taken",
        "_install_inline_validators",
        "_validate_all_required_for_save",
    ):
        assert (
            getattr(ConnectionDialog, name).__module__
            == "sshpilot.connection_dialog_validation"
        )


class FakeRow:
    """Minimal stand-in for an Adw.EntryRow used by the validators."""

    def __init__(self, text=""):
        self._text = text
        self.subtitle = None
        self.css = set()

    def get_text(self):
        return self._text

    def set_subtitle(self, s):
        self.subtitle = s

    def set_tooltip_text(self, s):
        pass

    def add_css_class(self, c):
        self.css.add(c)

    def remove_css_class(self, c):
        self.css.discard(c)


def _dialog():
    d = ConnectionDialog.__new__(ConnectionDialog)
    d.validator = SSHConnectionValidator()
    d.validation_results = {}
    d._save_buttons = []
    d.parent_window = None
    d.is_editing = False
    d.connection = None
    return d


class TestNicknameTaken:
    def _mgr_dialog(self, names, editing_nickname=None):
        d = _dialog()
        conns = [type("C", (), {"nickname": n})() for n in names]
        d.parent_window = type("P", (), {"connection_manager": type("M", (), {"connections": conns})()})()
        if editing_nickname is not None:
            d.is_editing = True
            d.connection = type("C", (), {"nickname": editing_nickname})()
        return d

    def test_taken_is_true(self):
        d = self._mgr_dialog({"Prod", "Dev"})
        assert d._is_nickname_taken("prod") is True

    def test_free_is_false(self):
        d = self._mgr_dialog({"Prod"})
        assert d._is_nickname_taken("staging") is False

    def test_current_name_excluded_when_editing(self):
        d = self._mgr_dialog({"Prod"}, editing_nickname="Prod")
        assert d._is_nickname_taken("prod") is False


class TestValidateFieldRow:
    def test_records_result_and_returns_it(self):
        d, row = _dialog(), FakeRow("good-name")
        result = d._validate_field_row("name", row)
        assert result.is_valid is True
        assert d.validation_results["name"] is result

    def test_invalid_name_marks_error(self):
        d, row = _dialog(), FakeRow("bad name")  # whitespace not allowed
        result = d._validate_field_row("name", row)
        assert result.is_valid is False
        assert "error" in row.css

    def test_invalid_host_marks_error(self):
        d, row = _dialog(), FakeRow("999.1.1.1")
        result = d._validate_field_row("hostname", row)
        assert result.is_valid is False
        assert "error" in row.css

    def test_invalid_port_marks_error(self):
        d, row = _dialog(), FakeRow("70000")
        result = d._validate_field_row("port", row)
        assert result.is_valid is False
        assert "error" in row.css
