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
from sshpilot.connection_dialog import SSHConfigAdvancedTab
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


class _Switch:
    def __init__(self, active=False):
        self.active = active

    def get_active(self):
        return self.active

    def set_active(self, active):
        self.active = active


class _AdvancedState:
    def __init__(self, *entries):
        self.entries = list(entries)
        self.set_calls = []
        self.remove_calls = []

    def get_option(self, keyword):
        wanted = keyword.lower()
        for key, value in self.entries:
            if key.lower() == wanted:
                return value
        return None

    def set_option(self, keyword, value):
        self.set_calls.append((keyword, value))
        wanted = keyword.lower()
        self.entries = [(k, v) for k, v in self.entries if k.lower() != wanted]
        self.entries.append((keyword, value))

    def remove_option(self, keyword, value=None):
        self.remove_calls.append((keyword, value))
        wanted = keyword.lower()
        wanted_value = value.lower() if value is not None else None
        self.entries = [
            (k, v)
            for k, v in self.entries
            if not (
                k.lower() == wanted
                and (wanted_value is None or v.lower() == wanted_value)
            )
        ]


def _session_type_dialog(advanced, active=False):
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog._session_type_syncing = False
    dialog.advanced_tab = advanced
    dialog.port_forwarding_only_row = _Switch(active)
    return dialog


def test_session_type_helpers_are_case_insensitive_and_deduplicate():
    advanced = SSHConfigAdvancedTab.__new__(SSHConfigAdvancedTab)
    advanced.entries = [
        ("Compression", "yes"),
        ("sessiontype", "default"),
        ("SessionType", "none"),
    ]
    advanced.get_config_entries = lambda: list(advanced.entries)
    advanced.set_config_entries = lambda entries: setattr(advanced, "entries", list(entries))

    assert advanced.get_option("SESSIONTYPE") == "default"

    advanced.set_option("SessionType", "none")
    assert advanced.entries == [("Compression", "yes"), ("SessionType", "none")]

    advanced.remove_option("sessionTYPE", "NONE")
    assert advanced.entries == [("Compression", "yes")]


def test_existing_session_type_none_turns_forwarding_only_on():
    dialog = _session_type_dialog(_AdvancedState(("sessiontype", "NONE")))

    dialog._sync_session_type_toggle_from_advanced()

    assert dialog.port_forwarding_only_row.get_active() is True


def test_forwarding_only_on_writes_one_session_type_none():
    advanced = _AdvancedState(("Compression", "yes"), ("sessiontype", "default"))
    dialog = _session_type_dialog(advanced, active=True)

    dialog._on_session_type_toggle_changed(dialog.port_forwarding_only_row)

    assert advanced.entries == [("Compression", "yes"), ("SessionType", "none")]
    assert advanced.set_calls == [("SessionType", "none")]


def test_forwarding_only_off_removes_session_type_none():
    advanced = _AdvancedState(("SessionType", "none"))
    dialog = _session_type_dialog(advanced, active=False)

    dialog._on_session_type_toggle_changed(dialog.port_forwarding_only_row)

    assert advanced.entries == []
    assert advanced.remove_calls == [("SessionType", "none")]


def test_manual_advanced_session_type_none_turns_forwarding_only_on():
    advanced = _AdvancedState(("SessionType", "default"))
    dialog = _session_type_dialog(advanced)

    advanced.entries[0] = ("SessionType", "none")
    dialog._sync_session_type_toggle_from_advanced()

    assert dialog.port_forwarding_only_row.get_active() is True


def test_non_none_advanced_session_type_turns_forwarding_only_off_and_survives():
    advanced = _AdvancedState(("SessionType", "subsystem"))
    dialog = _session_type_dialog(advanced, active=True)

    dialog._sync_session_type_toggle_from_advanced()
    dialog._on_session_type_toggle_changed(dialog.port_forwarding_only_row)

    assert dialog.port_forwarding_only_row.get_active() is False
    assert advanced.entries == [("SessionType", "subsystem")]
    assert advanced.remove_calls == []


def test_forwarding_only_round_trip_preserves_session_type_none():
    advanced = _AdvancedState()
    dialog = _session_type_dialog(advanced)

    dialog.port_forwarding_only_row.set_active(True)
    dialog._on_session_type_toggle_changed(dialog.port_forwarding_only_row)
    dialog._sync_session_type_toggle_from_advanced()

    assert advanced.entries == [("SessionType", "none")]
    assert dialog.port_forwarding_only_row.get_active() is True


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
