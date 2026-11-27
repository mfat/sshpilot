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


class _SilentOptionList:
    """Model SshOptionList.set_options(): load rows without change signals."""

    def __init__(self):
        self.options = []

    def set_options(self, options):
        self.options = list(options)

    def get_options(self):
        return list(self.options)


def _switch_dialog(advanced=None, session_none=False, exit_yes=False):
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog._loading_switch_lines = False
    dialog.advanced_tab = advanced if advanced is not None else _AdvancedState()
    dialog.port_forwarding_only_row = _Switch(session_none)
    dialog.exit_on_forward_failure_row = _Switch(exit_yes)
    dialog._schedule_effective_refresh = lambda *_a: None
    return dialog


# The two Port Forwarding switches own their line of the Host block: taken out
# of the extra lines on load (so Extra Options never lists it), joined back on
# save. A value a switch does not stand for stays on Extra Options.

def test_the_switches_take_only_the_lines_they_stand_for():
    found, rest = ConnectionDialog._split_switch_lines(
        "Compression yes\nsessiontype none\nExitOnForwardFailure=YES\n"
        "SessionType subsystem\nexitonforwardfailure no"
    )
    assert found == {"port_forwarding_only_row", "exit_on_forward_failure_row"}
    assert rest == "Compression yes\nSessionType subsystem\nexitonforwardfailure no"


def test_loading_sets_and_clears_both_switches():
    dialog = _switch_dialog(session_none=True, exit_yes=True)
    dialog._load_switch_lines({"exit_on_forward_failure_row"})
    assert dialog.port_forwarding_only_row.get_active() is False
    assert dialog.exit_on_forward_failure_row.get_active() is True
    dialog._load_switch_lines(set())
    assert dialog.exit_on_forward_failure_row.get_active() is False


def test_a_switch_that_is_on_writes_its_line():
    dialog = _switch_dialog(session_none=True)
    assert dialog._switch_extra_lines() == ["SessionType none"]
    dialog.exit_on_forward_failure_row.set_active(True)
    assert dialog._switch_extra_lines() == [
        "SessionType none", "ExitOnForwardFailure yes",
    ]
    dialog.port_forwarding_only_row.set_active(False)
    assert dialog._switch_extra_lines() == ["ExitOnForwardFailure yes"]


def test_turning_a_switch_on_drops_another_value_from_extra_options():
    # OpenSSH keeps the first value: SessionType subsystem left on the page
    # would make the switch do nothing.
    advanced = _AdvancedState(("Compression", "yes"), ("SessionType", "subsystem"))
    dialog = _switch_dialog(advanced, session_none=True)
    dialog._on_switch_line_toggled(dialog.port_forwarding_only_row, None, "SessionType")
    assert advanced.entries == [("Compression", "yes")]


def test_turning_a_switch_off_leaves_extra_options_alone():
    advanced = _AdvancedState(("ExitOnForwardFailure", "no"))
    dialog = _switch_dialog(advanced)
    dialog._on_switch_line_toggled(
        dialog.exit_on_forward_failure_row, None, "ExitOnForwardFailure"
    )
    assert advanced.entries == [("ExitOnForwardFailure", "no")]
    assert advanced.remove_calls == []


def test_loading_does_not_touch_extra_options():
    advanced = _AdvancedState(("SessionType", "subsystem"))
    dialog = _switch_dialog(advanced)
    dialog._loading_switch_lines = True
    dialog.port_forwarding_only_row.set_active(True)
    dialog._on_switch_line_toggled(dialog.port_forwarding_only_row, None, "SessionType")
    assert advanced.remove_calls == []


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


def test_an_empty_advanced_snapshot_clears_the_options():
    """A loaded empty Host block must not retain options from the prior one."""
    advanced = SSHConfigAdvancedTab.__new__(SSHConfigAdvancedTab)
    advanced.option_list = _SilentOptionList()
    advanced.parent_dialog = None

    advanced.set_extra_ssh_config("Compression yes")
    advanced.set_extra_ssh_config("")

    assert advanced.get_config_entries() == []
    assert advanced.get_extra_ssh_config() == ""


def test_forwarding_rule_reload_is_private_and_replaces_empty_snapshots():
    """Dialog edits must not change a live Connection until Save succeeds."""
    from types import SimpleNamespace

    original_rule = {
        "type": "local",
        "listen_addr": "localhost",
        "listen_port": 8080,
        "remote_host": "web",
        "remote_port": 80,
    }
    connection = SimpleNamespace(forwarding_rules=[original_rule])
    dialog = _dialog()
    snapshots = []
    dialog.load_port_forwarding_rules = lambda: snapshots.append(
        [dict(rule) for rule in dialog.forwarding_rules]
    )

    dialog._load_forwarding_rule_draft(connection)
    assert dialog.forwarding_rules == connection.forwarding_rules
    assert dialog.forwarding_rules is not connection.forwarding_rules
    assert dialog.forwarding_rules[0] is not original_rule

    dialog.forwarding_rules[0]["remote_host"] = "changed"
    dialog.forwarding_rules.append({"type": "dynamic", "listen_port": 1080})
    assert connection.forwarding_rules == [original_rule]
    assert original_rule["remote_host"] == "web"

    dialog._load_forwarding_rule_draft(SimpleNamespace(forwarding_rules=[]))
    assert dialog.forwarding_rules == []
    assert snapshots[-1] == []


def test_connection_loader_clears_advanced_and_forwarding_on_empty_snapshot():
    """Exercise loader wiring, including its populated-to-empty reload path."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    advanced = SSHConfigAdvancedTab.__new__(SSHConfigAdvancedTab)
    advanced.option_list = _SilentOptionList()
    dialog = _switch_dialog(advanced)
    advanced.parent_dialog = dialog
    dialog.is_editing = True
    dialog._loading_connection_data = False
    dialog.is_daemon_editor = lambda: False

    # Only unrelated controls/services and row rendering are stubbed. Keep
    # load_connection_data, both draft loaders, and switch synchronization real.
    for name in (
        "display_name_row", "nickname_row", "hostname_row", "username_row",
        "port_row", "proxy_jump_row", "forward_agent_row", "auth_policy",
        "key_editor", "cert_editor", "password_row", "key_select_row",
        "key_only_row", "x11_row", "identity_agent_row",
    ):
        setattr(dialog, name, Mock())
    for name in (
        "_load_shared_meta_rows", "_load_password_async",
        "_on_forward_agent_mode_changed", "_on_identity_agent_mode_changed",
        "on_key_select_changed", "_capture_editor_delta_baseline",
    ):
        setattr(dialog, name, Mock())
    dialog.show_error = Mock()
    rendered_rules = []
    dialog.load_port_forwarding_rules = lambda: rendered_rules.append(
        [dict(rule) for rule in dialog.forwarding_rules]
    )
    rule = {
        "type": "local", "listen_addr": "localhost", "listen_port": 8080,
        "remote_host": "web", "remote_port": 80,
    }
    dialog.connection = SimpleNamespace(
        nickname="populated", extra_ssh_config=(
            "SessionType none\nExitOnForwardFailure yes\nCompression yes"
        ), forwarding_rules=[rule],
    )

    dialog.load_connection_data()

    # The switches own their lines: Extra Options keeps only the rest.
    assert advanced.get_config_entries() == [("Compression", "yes")]
    assert dialog.port_forwarding_only_row.get_active() is True
    assert dialog.exit_on_forward_failure_row.get_active() is True
    assert dialog.forwarding_rules == [rule]
    assert rendered_rules == [[rule]]

    dialog.connection = SimpleNamespace(
        nickname="empty", extra_ssh_config="", forwarding_rules=[],
    )
    dialog.load_connection_data()

    assert advanced.get_config_entries() == []
    assert advanced.get_extra_ssh_config() == ""
    assert dialog.port_forwarding_only_row.get_active() is False
    assert dialog.exit_on_forward_failure_row.get_active() is False
    assert dialog.forwarding_rules == []
    assert rendered_rules == [[rule], []]
    dialog.show_error.assert_not_called()
