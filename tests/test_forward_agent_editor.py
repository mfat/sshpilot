"""ForwardAgent editor text maps to yes / no / socket / $ENV, not a boolean."""

import types

import pytest

from sshpilot.api.models.connections import (
    EDITABLE_CONFIG_FIELDS,
    validate_config_patch,
)
from sshpilot.connection_dialog import (
    ConnectionDialog,
    forward_agent_fields_from_mode,
    forward_agent_fields_from_text,
    forward_agent_mode_from_connection,
    forward_agent_mode_from_fields,
    forward_agent_text_from_connection,
    forward_agent_text_from_fields,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", {
            "forward_agent": False,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "",
        }),
        ("  ", {
            "forward_agent": False,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "",
        }),
        ("yes", {
            "forward_agent": True,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "",
        }),
        ("YES", {
            "forward_agent": True,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "",
        }),
        ("no", {
            "forward_agent": False,
            "forward_agent_explicit_no": True,
            "forward_agent_target": "",
        }),
        ("/tmp/agent.sock", {
            "forward_agent": True,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "/tmp/agent.sock",
        }),
        ("$SSH_AUTH_SOCK", {
            "forward_agent": True,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "$SSH_AUTH_SOCK",
        }),
    ],
)
def test_forward_agent_fields_from_text(text, expected):
    assert forward_agent_fields_from_text(text) == expected


@pytest.mark.parametrize(
    "fields, text",
    [
        ({}, ""),
        ({"forward_agent": True}, "yes"),
        ({"forward_agent_explicit_no": True}, "no"),
        ({"forward_agent": True, "forward_agent_target": "$SSH_AUTH_SOCK"},
         "$SSH_AUTH_SOCK"),
        ({"forward_agent": True, "forward_agent_target": "/run/user/1000/ssh-agent"},
         "/run/user/1000/ssh-agent"),
    ],
)
def test_forward_agent_text_from_fields(fields, text):
    assert forward_agent_text_from_fields(**fields) == text


def test_forward_agent_text_reads_target_from_connection_data():
    connection = types.SimpleNamespace(
        forward_agent=True,
        forward_agent_explicit_no=False,
        forward_agent_target="",
        data={"forward_agent_target": "$SSH_AUTH_SOCK"},
    )
    assert forward_agent_text_from_connection(connection) == "$SSH_AUTH_SOCK"
    assert forward_agent_mode_from_connection(connection) == ("env", "$SSH_AUTH_SOCK")


@pytest.mark.parametrize(
    "fields, mode, extra",
    [
        ({}, "default", ""),
        ({"forward_agent": True}, "yes", ""),
        ({"forward_agent_explicit_no": True}, "no", ""),
        ({"forward_agent": True, "forward_agent_target": "/tmp/agent.sock"},
         "path", "/tmp/agent.sock"),
        ({"forward_agent": True, "forward_agent_target": "$SSH_AUTH_SOCK"},
         "env", "$SSH_AUTH_SOCK"),
    ],
)
def test_forward_agent_mode_from_fields(fields, mode, extra):
    assert forward_agent_mode_from_fields(**fields) == (mode, extra)


@pytest.mark.parametrize(
    "mode, extra, expected",
    [
        ("default", "", {
            "forward_agent": False,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "",
        }),
        ("yes", "ignored", {
            "forward_agent": True,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "",
        }),
        ("no", "", {
            "forward_agent": False,
            "forward_agent_explicit_no": True,
            "forward_agent_target": "",
        }),
        ("path", "/tmp/agent.sock", {
            "forward_agent": True,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "/tmp/agent.sock",
        }),
        ("path", "", {
            "forward_agent": False,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "",
        }),
        ("env", "$SSH_AUTH_SOCK", {
            "forward_agent": True,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "$SSH_AUTH_SOCK",
        }),
        ("env", "SSH_AUTH_SOCK", {
            "forward_agent": True,
            "forward_agent_explicit_no": False,
            "forward_agent_target": "$SSH_AUTH_SOCK",
        }),
    ],
)
def test_forward_agent_fields_from_mode(mode, extra, expected):
    assert forward_agent_fields_from_mode(mode, extra) == expected


class _DummyCombo:
    def __init__(self, selected=0):
        self._selected = selected

    def get_selected(self):
        return self._selected

    def set_selected(self, value):
        self._selected = value


class _DummyEntry:
    def __init__(self, text=""):
        self._text = text
        self.visible = True

    def get_text(self):
        return self._text

    def set_text(self, text):
        self._text = text

    def set_visible(self, value):
        self.visible = bool(value)

    def set_title(self, *_args, **_kwargs):
        return None

    def set_subtitle(self, *_args, **_kwargs):
        return None


class _DummyButton:
    def __init__(self):
        self.visible = True

    def set_visible(self, value):
        self.visible = bool(value)


def _combo_dialog(selected=0, extra=""):
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.forward_agent_row = _DummyCombo(selected)
    dialog.forward_agent_value_row = _DummyEntry(extra)
    dialog._forward_agent_browse_btn = _DummyButton()
    return dialog


def test_path_and_env_reveal_the_value_row():
    dialog = _combo_dialog(selected=0)
    dialog._on_forward_agent_mode_changed()
    assert dialog.forward_agent_value_row.visible is False
    assert dialog._forward_agent_browse_btn.visible is False

    dialog.forward_agent_row.set_selected(3)  # path
    dialog._on_forward_agent_mode_changed()
    assert dialog.forward_agent_value_row.visible is True
    assert dialog._forward_agent_browse_btn.visible is True

    dialog.forward_agent_row.set_selected(4)  # env
    dialog._on_forward_agent_mode_changed()
    assert dialog.forward_agent_value_row.visible is True
    assert dialog._forward_agent_browse_btn.visible is False


def test_combo_save_emits_socket_and_env_targets():
    path_dialog = _combo_dialog(selected=3, extra="/tmp/agent.sock")
    assert path_dialog._selected_forward_agent_fields() == {
        "forward_agent": True,
        "forward_agent_explicit_no": False,
        "forward_agent_target": "/tmp/agent.sock",
    }

    env_dialog = _combo_dialog(selected=4, extra="SSH_AUTH_SOCK")
    assert env_dialog._selected_forward_agent_fields() == {
        "forward_agent": True,
        "forward_agent_explicit_no": False,
        "forward_agent_target": "$SSH_AUTH_SOCK",
    }

    yes_dialog = _combo_dialog(selected=1, extra="/tmp/should-be-ignored")
    assert yes_dialog._selected_forward_agent_fields() == {
        "forward_agent": True,
        "forward_agent_explicit_no": False,
        "forward_agent_target": "",
    }


def test_config_patch_accepts_forward_agent_socket():
    assert "forward_agent_target" in EDITABLE_CONFIG_FIELDS
    assert "forward_agent_explicit_no" in EDITABLE_CONFIG_FIELDS
    validate_config_patch({
        "forward_agent": True,
        "forward_agent_explicit_no": False,
        "forward_agent_target": "$SSH_AUTH_SOCK",
    })


def test_config_patch_rejects_non_string_forward_agent_target():
    with pytest.raises(ValueError, match="forward_agent_target"):
        validate_config_patch({"forward_agent_target": True})


def test_changing_yes_to_socket_includes_all_forward_agent_fields():
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog._editor_delta_baseline = {
        "hostname": "h.example",
        "forward_agent": True,
        "forward_agent_explicit_no": False,
        "forward_agent_target": "",
    }
    dialog._adopted_inherited_fields = set()
    changed = dialog._changed_editor_fields({
        "hostname": "h.example",
        "forward_agent": True,
        "forward_agent_explicit_no": False,
        "forward_agent_target": "/tmp/agent.sock",
    })
    assert set(changed) == {
        "forward_agent",
        "forward_agent_explicit_no",
        "forward_agent_target",
    }
