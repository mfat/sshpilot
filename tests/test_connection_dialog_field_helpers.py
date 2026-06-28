"""Tests for the ConnectionDialog field-input helpers mixin.

The autocomplete/picker helpers were extracted verbatim from
connection_dialog.py into ConnectionDialogFieldHelpersMixin
(sshpilot/connection_dialog_field_helpers.py). The popover builders and the
WoL detect button are GTK/thread-bound and aren't exercised here (the suite
stubs gi). These tests guard the extraction contract (ConnectionDialog still
inherits each method) and cover the pure candidate/text helpers, which had no
focused coverage before.
"""

from sshpilot.connection_dialog import ConnectionDialog
from sshpilot.connection_dialog_field_helpers import ConnectionDialogFieldHelpersMixin


def test_connection_dialog_inherits_the_field_helpers_mixin():
    assert issubclass(ConnectionDialog, ConnectionDialogFieldHelpersMixin)


def test_moved_methods_are_owned_by_the_mixin():
    for name in (
        "_on_wol_detect_mac_clicked",
        "_show_host_picker_popover",
        "_tag_candidates",
        "_jump_host_candidates",
        "_setup_comma_autocomplete",
        "_set_text_without_completion",
        "_show_tag_picker_popover",
    ):
        assert (
            getattr(ConnectionDialog, name).__module__
            == "sshpilot.connection_dialog_field_helpers"
        )


def _dialog():
    return ConnectionDialog.__new__(ConnectionDialog)


class _Conn:
    def __init__(self, nickname):
        self.nickname = nickname


class TestTagCandidates:
    def test_none_config_returns_empty(self):
        d = _dialog()
        d.parent_window = type("P", (), {"config": None})()
        assert d._tag_candidates() == []

    def test_returns_tag_names_dropping_counts(self):
        d = _dialog()
        cfg = type("C", (), {"get_all_tags": lambda self: [("work", 3), ("home", 1)]})()
        d.parent_window = type("P", (), {"config": cfg})()
        assert d._tag_candidates() == ["work", "home"]


class TestJumpHostCandidates:
    def test_no_manager_returns_empty(self):
        d = _dialog()
        d.connection_manager = None
        assert d._jump_host_candidates() == []

    def test_sorted_casefold_excluding_current_and_empty(self):
        d = _dialog()
        conns = [_Conn("zeta"), _Conn("Alpha"), _Conn(""), _Conn("prod")]
        d.connection_manager = type("M", (), {"connections": conns})()
        d.connection = _Conn("prod")  # current is excluded
        assert d._jump_host_candidates() == ["Alpha", "zeta"]


class _FakeRow:
    def __init__(self):
        self.text = None
        self._ac_state = {"busy": False, "prev_len": 0, "active": True}

    def set_text(self, t):
        self.text = t


class TestSetTextWithoutCompletion:
    def test_sets_text_and_resets_completion_state(self):
        d = _dialog()
        row = _FakeRow()
        d._set_text_without_completion(row, "host-a, host-b")
        assert row.text == "host-a, host-b"
        assert row._ac_state["busy"] is False
        assert row._ac_state["prev_len"] == len("host-a, host-b")
        assert row._ac_state["active"] is False

    def test_tolerates_row_without_state(self):
        d = _dialog()
        row = _FakeRow()
        row._ac_state = None
        d._set_text_without_completion(row, "x")
        assert row.text == "x"
