"""Tests for the ConnectionDialog port-forwarding mixin.

The port-forwarding rule list, rule editor, per-type defaults, and the
listening-ports info dialog were extracted verbatim from connection_dialog.py
into ConnectionDialogPortForwardingMixin
(sshpilot/connection_dialog_port_forwarding.py). The GTK-bound builders aren't
exercised here (the suite stubs gi); the rule-save logic is covered in
tests/test_connection_forwarding.py. These tests guard the extraction contract
(ConnectionDialog still inherits each method, owned by the mixin module) and the
pure per-type default seeding, which routes through fake rows.
"""

from sshpilot.connection_dialog import ConnectionDialog
from sshpilot.connection_dialog_port_forwarding import (
    ConnectionDialogPortForwardingMixin,
)

_MOVED = (
    "load_port_forwarding_rules",
    "on_delete_forwarding_rule_clicked",
    "on_edit_forwarding_rule_clicked",
    "on_add_forwarding_rule_clicked",
    "on_view_port_info_clicked",
    "_open_rule_editor",
    "_save_rule_from_editor",
    "_apply_rule_editor_defaults_for_type",
    "_show_port_info_dialog",
    "_autosave_forwarding_changes",
)


def test_connection_dialog_inherits_the_port_forwarding_mixin():
    assert issubclass(ConnectionDialog, ConnectionDialogPortForwardingMixin)


def test_moved_methods_are_owned_by_the_mixin():
    for name in _MOVED:
        assert (
            getattr(ConnectionDialog, name).__module__
            == "sshpilot.connection_dialog_port_forwarding"
        )


def _dialog():
    return ConnectionDialog.__new__(ConnectionDialog)


class _Row:
    def __init__(self, text=""):
        self._text = text

    def get_text(self):
        return self._text

    def set_text(self, t):
        self._text = t


class TestApplyRuleEditorDefaultsForType:
    def test_local_seeds_blank_fields(self):
        d = _dialog()
        la, lp, rh, rp = _Row(), _Row(), _Row(), _Row()
        d._apply_rule_editor_defaults_for_type(0, la, lp, rh, rp)
        assert la.get_text() == "localhost"
        assert lp.get_text() == "8080"
        assert rh.get_text() == "localhost"
        assert rp.get_text() == "22"

    def test_dynamic_seeds_socks_port(self):
        d = _dialog()
        la, lp, rh, rp = _Row(), _Row(), _Row(), _Row()
        d._apply_rule_editor_defaults_for_type(2, la, lp, rh, rp)
        assert la.get_text() == "localhost"
        assert lp.get_text() == "1080"

    def test_switching_remote_to_local_resets_target_host(self):
        d = _dialog()
        la, lp = _Row("localhost"), _Row("8080")
        rh, rp = _Row("example.com"), _Row("443")
        d._apply_rule_editor_defaults_for_type(0, la, lp, rh, rp, previous_idx=1)
        assert rh.get_text() == "localhost"

    def test_remote_clears_bind_address_on_switch(self):
        d = _dialog()
        la, lp = _Row("localhost"), _Row("8080")
        rh, rp = _Row(), _Row()
        d._apply_rule_editor_defaults_for_type(1, la, lp, rh, rp, previous_idx=0)
        assert la.get_text() == ""

    def test_autosave_is_a_noop(self):
        assert _dialog()._autosave_forwarding_changes() is None
