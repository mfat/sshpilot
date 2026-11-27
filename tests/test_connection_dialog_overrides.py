"""The connection editor marks its fields from OpenSSH's answer for the edit.

One question per edit (``connections.evaluate_connection_edit``): the daemon
writes the Host block as the save would and evaluates the file. Each field
takes from the answer what Advanced Configuration shows for the same line:
"No effect" when an earlier block decides the option first, "Overridden"
when Settings ▸ SSH Options do, "Rejected" when OpenSSH refuses the line,
and the block that decides a value the host does not set.

The answers here are real: the daemon's service on a temporary configuration,
evaluated by the OpenSSH client.
"""

import shutil
import types

import pytest

from sshpilot.api.models.connections import (
    EvaluateConnectionEditRequest,
    UpdateConnectionRequest,
)
from sshpilot.connection_dialog_answer import (
    NO_EFFECT,
    OVERRIDDEN as SETTINGS,
    EditorAnswer,
)
from sshpilot.connection_dialog_draft import _AUTH_KEYWORDS
from sshpilot.connection_dialog_overrides import (
    GLOBAL,
    OVERRIDABLE_ROWS,
    OVERRIDDEN,
    REJECTED,
    ConnectionDialogOverridesMixin as Overrides,
    inherited_note_text,
    row_mark,
)
from sshpilot.connection_dialog_validation import (
    ConnectionDialogValidationMixin as Validation,
)
from sshpilot.core.connections.auth_preferences import AUTH_POLICY_KEYWORDS
from sshpilot.ssh_config_formatter import MANAGED_HOST_OPTIONS

needs_ssh = pytest.mark.skipif(
    shutil.which("ssh") is None, reason="needs the OpenSSH client"
)


def _answer(tmp_path, text, host="h", extra=None, forced=()):
    """OpenSSH's answer for *host* in *text*, with *extra* as the form's
    Extra Options and *forced* as Settings ▸ SSH Options (``-o`` pairs)."""
    from tests.core.test_connection_edit_evaluation import _service

    tmp_path.mkdir(parents=True, exist_ok=True)
    service, repository, _root = _service(tmp_path, text)
    if forced:
        args = []
        for option in forced:
            args += ["-o", option]
        service._forced_ssh_option_args = lambda: tuple(args)
    update = None
    if extra is not None:
        update = UpdateConnectionRequest(
            config_patch={"extra_ssh_config": "\n".join(extra)},
            expected_generation=repository.get_record(host).generation,
        )
    return EditorAnswer(service.evaluate_connection_edit(EvaluateConnectionEditRequest(
        connection_id=host, update=update, without_own=tuple(sorted(_AUTH_KEYWORDS)),
    )))


def _rm(*args, **kwargs):
    """``row_mark`` as ``(kind, tag, note, style)``."""
    kind, verdict = row_mark(*args, **kwargs)
    return kind, verdict.tag, verdict.note, verdict.style


def _v(tag="", note="", style="muted"):
    from sshpilot.ssh_config_answer import LineVerdict

    return LineVerdict(tag=tag, style=style, tooltip=note, note=note)


HOST = "Host h\n    HostName 192.0.2.1\n"


def test_every_marked_row_is_an_option_the_dialog_owns():
    for _row, keyword in OVERRIDABLE_ROWS:
        # The two forwarding switches, connection sharing and the
        # login-policy rows write their option as a plain Host-block line;
        # every other row is a directive the formatter owns.
        assert keyword.lower() in MANAGED_HOST_OPTIONS | AUTH_POLICY_KEYWORDS | {
            "exitonforwardfailure", "sessiontype", "controlmaster",
        }


# -- what the answer says ------------------------------------------------------

@needs_ssh
def test_issue_1406_an_option_from_a_later_host_star_is_listed_as_inherited(tmp_path):
    answer = _answer(tmp_path, HOST + "\nHost *\n    Compression yes\n")
    assert answer.inherited_options(["Compression", "ServerAliveInterval"]) == [
        ("Compression", "yes", "line 5 (Host *)"),
    ]
    # OpenSSH's own defaults are not "inherited from your configuration".
    assert answer.is_default("serveraliveinterval")


@needs_ssh
def test_an_option_the_host_sets_is_not_listed_as_inherited(tmp_path):
    answer = _answer(
        tmp_path, HOST + "\nHost *\n    Compression yes\n", extra=["Compression no"]
    )
    assert answer.inherited_options(["Compression"]) == []
    assert answer.line_state("compression")[0] == "used"


@needs_ssh
def test_a_line_an_earlier_block_beats_has_no_effect_and_says_what_wins(tmp_path):
    answer = _answer(
        tmp_path, "Host *\n    User shared\n\n" + HOST + "    User mine\n",
    )
    kind, tag, note, style = _rm("username_row", "User", answer)
    assert (kind, tag, style) == (OVERRIDDEN, "No effect", "error")
    assert note == "OpenSSH uses shared from line 2 (Host *)"
    assert answer.unused_count() == (0, 1)


@needs_ssh
def test_settings_override_a_line_and_the_mark_says_so(tmp_path):
    answer = _answer(
        tmp_path, HOST + "    Port 2200\n", forced=("Port=2222",),
    )
    kind, tag, note, style = _rm("port_row", "Port", answer)
    assert (kind, tag, style) == (OVERRIDDEN, "Overridden", "purple")
    assert note == "Settings ▸ SSH Options use 2222"
    assert answer.unused_count() == (1, 0)


@needs_ssh
def test_a_line_openssh_rejects_is_rejected(tmp_path):
    answer = _answer(tmp_path, HOST, extra=["Compression maybe"])
    kind, tag, note, style = _rm("x", "Compression", answer)
    assert (kind, tag, style) == (REJECTED, "Rejected", "error")
    assert "OpenSSH rejects line" in note


@needs_ssh
def test_an_empty_text_field_names_the_block_that_decides_it(tmp_path):
    answer = _answer(tmp_path, HOST + "\nHost *\n    User shared\n")
    kind, tag, note, _style = _rm("username_row", "User", answer)
    assert (kind, tag) == (GLOBAL, "from Host *")
    assert note == "OpenSSH uses shared from line 5 (Host *)"
    assert answer.inherited_note("user") == note


@needs_ssh
def test_a_default_needs_no_mark(tmp_path):
    answer = _answer(tmp_path, HOST)
    assert _rm("port_row", "Port", answer)[:2] == (None, "")
    assert answer.inherited_note("port") == "OpenSSH default: 22"


@needs_ssh
def test_an_emptied_text_field_shows_the_value_it_gets_in_its_tag(tmp_path):
    # Entry rows cannot show a placeholder cleanly, so the tag carries it.
    answer = _answer(tmp_path, HOST + "\nHost *\n    User shared\n")
    kind, tag, note, style = _rm("port_row", "Port", answer, empty=True)
    assert (kind, tag, style) == (GLOBAL, "Not set (22, OpenSSH default)", "muted")
    assert note == "OpenSSH default: 22"
    kind, tag, _note, _style = _rm("username_row", "User", answer, empty=True)
    assert tag == "Not set (shared, from Host *)"
    # Not for switches or selectors, whose state shows a value of its own.
    assert _rm("x11_row", "ForwardX11", answer, empty=True)[0] is None


@needs_ssh
def test_a_switch_that_is_off_is_pointed_out_only_when_the_value_differs(tmp_path):
    on = _answer(tmp_path / "on", HOST + "\nHost *\n    ForwardX11 yes\n")
    assert _rm("x11_row", "ForwardX11", on)[0] == GLOBAL
    off = _answer(tmp_path / "off", HOST + "\nHost *\n    ForwardX11 no\n")
    assert _rm("x11_row", "ForwardX11", off)[0] is None


@needs_ssh
def test_a_selector_on_default_names_the_block_that_decides(tmp_path):
    answer = _answer(tmp_path, HOST + "\nHost *\n    ForwardAgent yes\n")
    kind, tag, _note, _style = _rm("forward_agent_row", "ForwardAgent", answer)
    assert (kind, tag) == (GLOBAL, "from Host *")


@needs_ssh
def test_settings_multiplexing_overrides_a_host_that_turns_sharing_off(tmp_path):
    # Settings ▸ SSH Options win over the block: its Off goes unused.
    answer = _answer(
        tmp_path, HOST, extra=["ControlMaster no", "ControlPath none"],
        forced=("ControlMaster=auto", "ControlPath=/tmp/cm/%C", "ControlPersist=60"),
    )
    kind, tag, _note, style = _rm("multiplex_row", "ControlMaster", answer)
    assert (kind, tag, style) == (OVERRIDDEN, "Overridden", "purple")


@needs_ssh
def test_sharing_left_at_default_names_settings_when_they_force_it(tmp_path):
    answer = _answer(
        tmp_path, HOST,
        forced=("ControlMaster=auto", "ControlPath=/tmp/cm/%C", "ControlPersist=60"),
    )
    kind, _tag, _note, _style = _rm("multiplex_row", "ControlMaster", answer)
    assert kind == GLOBAL
    assert answer.unused_count() == (0, 0)


@needs_ssh
def test_sharing_without_settings_or_blocks_needs_no_mark(tmp_path):
    answer = _answer(tmp_path, HOST)
    assert _rm("multiplex_row", "ControlMaster", answer)[0] is None
    on = _answer(
        tmp_path / "on", HOST,
        extra=["ControlMaster auto", "ControlPath ~/.ssh/sockets/%C", "ControlPersist 60"],
    )
    assert _rm("multiplex_row", "ControlMaster", on)[0] is None


@needs_ssh
def test_an_unset_authentication_row_needs_no_mark(tmp_path):
    answer = _answer(tmp_path, HOST + "\nHost *\n    PasswordAuthentication no\n")
    assert _rm("auth_password_row", "PasswordAuthentication", answer)[0] is None


@needs_ssh
def test_an_authentication_line_beaten_by_an_earlier_block(tmp_path):
    answer = _answer(
        tmp_path, "Host *\n    PasswordAuthentication no\n\n" + HOST,
        extra=["PasswordAuthentication yes"],
    )
    assert _rm("auth_password_row", "PasswordAuthentication", answer)[0] == OVERRIDDEN
    assert answer.inherited_first()["passwordauthentication"] == "no"


@needs_ssh
def test_local_command_loses_when_an_earlier_block_forbids_local_commands(tmp_path):
    answer = _answer(
        tmp_path, "Host *\n    PermitLocalCommand no\n\n" + HOST
        + "    LocalCommand echo hi\n    PermitLocalCommand yes\n",
    )
    kind, _tag, note, _style = _rm("local_command_row", "LocalCommand", answer)
    assert kind == OVERRIDDEN
    assert note == "PermitLocalCommand: OpenSSH uses no from line 2 (Host *)"


@needs_ssh
def test_an_extra_option_is_judged_by_its_own_line(tmp_path):
    answer = _answer(
        tmp_path, "Host *\n    Compression yes\n\n" + HOST, extra=["Compression no"],
    )
    dialog = types.SimpleNamespace(_editor_answer=answer)
    note, unused = Overrides._advanced_option_note(dialog, "Compression", "no")
    assert unused and note == "OpenSSH uses yes from line 2 (Host *)"
    assert Overrides._advanced_option_note(dialog, "Compression", "other") is None


@needs_ssh
def test_an_option_settings_add_to_is_said_to_be_used_twice(tmp_path):
    answer = _answer(tmp_path, HOST, extra=["SendEnv TERM"], forced=("SendEnv=LANG",))
    dialog = types.SimpleNamespace(_editor_answer=answer)
    note, unused = Overrides._advanced_option_note(dialog, "SendEnv", "TERM")
    assert not unused and "LANG" in note and "Both are used" in note
    assert answer.unused_count() == (0, 0)


def test_no_answer_marks_nothing():
    assert _rm("username_row", "User", None)[:2] == (None, "")
    dialog = types.SimpleNamespace(_editor_answer=None)
    assert Overrides._advanced_option_note(dialog, "Compression", "no") is None
    assert Overrides._overridden_field_counts(dialog) == (0, 0)


# -- the marks on real rows ----------------------------------------------------

def _real_gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        Adw.init()
        return type(Adw.PreferencesGroup()).__name__ == "PreferencesGroup"
    except Exception:
        return False


needs_adw = pytest.mark.skipif(
    not _real_gtk_available(),
    reason="needs real libadwaita (gi is stubbed under the suite)",
)


def _dialog(**attrs):
    dialog = types.SimpleNamespace(_inherited_row_handlers={}, **attrs)
    for name in (
        "_set_row_override_note", "_set_row_inherited_note", "_refresh_row_tooltip",
        "_set_row_override_error", "_set_row_validation_error", "_refresh_row_error_class",
    ):
        setattr(dialog, name, getattr(Validation, name).__get__(dialog))
    dialog._mark_row_override = Overrides._mark_row_override.__get__(dialog)
    dialog._reveal_config_line = lambda target: None
    dialog._inherited_advice = Overrides._inherited_advice.__get__(dialog)
    dialog._mark_unused_password = Overrides._mark_unused_password.__get__(dialog)
    return dialog


@needs_adw
def test_a_value_with_no_effect_is_struck_tinted_and_tagged_and_stays_editable():
    from gi.repository import Adw

    row = Adw.EntryRow(title="Username")
    row.set_text("alice")
    dialog = _dialog(username_row=row)
    dialog._mark_row_override(
        "username_row", row, OVERRIDDEN, _v("No effect", "OpenSSH uses bob", "error")
    )
    assert row._sshpilot_override_pill.get_label() == "No effect"
    assert row.has_css_class("warning") and not row.has_css_class("error")
    assert row.get_delegate()._sshpilot_struck
    assert row.get_text() == "alice" and row.get_sensitive()
    assert "OpenSSH uses bob" in row.get_tooltip_text()

    dialog._mark_row_override("username_row", row, None, _v())
    assert not row._sshpilot_override_pill.get_visible()
    assert not row.has_css_class("warning")
    assert not row.get_delegate()._sshpilot_struck
    assert not row.get_tooltip_text()


@needs_adw
def test_a_rejected_line_is_red_and_not_struck():
    from gi.repository import Adw

    row = Adw.SwitchRow(title="X11")
    dialog = _dialog(x11_row=row)
    dialog._mark_row_override("x11_row", row, REJECTED, _v("Rejected", "bad", "error"))
    assert row.has_css_class("error")
    assert not row.has_css_class("warning")


@needs_adw
def test_a_mark_replaces_the_inherited_advice_and_gives_it_back():
    from gi.repository import Adw

    row = Adw.EntryRow(title="Port")
    dialog = _dialog(port_row=row)
    dialog._inherited_row_handlers["port_row"] = 1
    dialog._set_row_inherited_note(row, inherited_note_text())

    dialog._mark_row_override(
        "port_row", row, OVERRIDDEN,
        _v("Overridden", "Settings ▸ SSH Options use 2222", "purple"),
    )
    assert inherited_note_text() not in row.get_tooltip_text()
    dialog._mark_row_override("port_row", row, None, _v())
    assert row.get_tooltip_text() == inherited_note_text()


def test_states_used_by_the_marks_are_the_answer_s():
    assert NO_EFFECT != SETTINGS


# -- the authentication section and the password, in the window's words -------


@needs_ssh
def test_the_authentication_section_is_told_the_line_that_decides(tmp_path):
    # The issue's case: a global "no" beats this host's "yes".
    answer = _answer(
        tmp_path, "PasswordAuthentication no\n\n" + HOST,
        extra=["PasswordAuthentication yes"],
    )
    note = Overrides._deciding_note(answer, "PasswordAuthentication")
    assert note == "OpenSSH uses no from line 1 (global options)"
    # Inherited, not set here: the same words.
    answer = _answer(tmp_path / "b", HOST + "\nHost *\n    PasswordAuthentication no\n")
    assert Overrides._deciding_note(answer, "PasswordAuthentication") == (
        "OpenSSH uses no from line 5 (Host *)"
    )


def test_a_method_caption_names_what_decides_when_known():
    from sshpilot.connection_dialog_auth import method_caption

    reason = "OpenSSH uses no from line 1 (global options)"
    # The window's sentence alone, either way.
    assert method_caption("", False, True, False, reason) == reason
    assert method_caption("", False, False, True, reason) == reason
    # Agreeing rows say nothing, whatever the reason.
    assert method_caption("", False, None, False, reason) == ""


def test_an_inherited_value_s_advice_says_where_it_comes_from():
    assert inherited_note_text("OpenSSH uses shared from line 5 (Host *)") == (
        "OpenSSH uses shared from line 5 (Host *)\nEdit to set it for this host."
    )


@needs_adw
def test_the_saved_password_has_no_effect_when_no_password_login_is_tried():
    from gi.repository import Adw

    row = Adw.PasswordEntryRow(title="Password")
    row.set_text("secret")
    will_try = ["publickey", "hostbased"]
    section = types.SimpleNamespace(
        policy=types.SimpleNamespace(will_try=lambda: list(will_try))
    )
    dialog = _dialog(password_row=row, auth_policy=section)
    dialog._mark_unused_password()
    assert row._sshpilot_override_state == "unused"
    assert row.has_css_class("warning")
    assert "never asks for this password" in row.get_tooltip_text()
    # Keyboard-interactive asks for it too.
    will_try.append("keyboard-interactive")
    dialog._mark_unused_password()
    assert not row.has_css_class("warning")
    assert not row.get_tooltip_text()
    # Nothing saved, nothing to mark.
    will_try.pop()
    row.set_text("")
    dialog._mark_unused_password()
    assert not row.has_css_class("warning")


def test_refilling_an_inherited_row_keeps_other_edits_in_the_save():
    """Regression: each answer that refilled an inherited row took the whole
    form as loaded, so a key added meanwhile was never saved."""
    from sshpilot.connection_dialog import ConnectionDialog

    form = {"username": "shared", "identity_files": ("~/.ssh/kwp4",)}
    dialog = types.SimpleNamespace(
        _editor_delta_baseline={"username": "", "identity_files": ()},
    )
    dialog._capture_editor_delta_baseline = lambda: setattr(
        dialog, "_editor_delta_baseline", dict(form)
    )
    ConnectionDialog._rebaseline_fields(dialog, {"username"})
    assert dialog._editor_delta_baseline == {
        "username": "shared",         # filled by the dialog: not an edit
        "identity_files": (),         # the user's key: still a change
    }
