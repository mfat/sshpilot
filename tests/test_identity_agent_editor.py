"""IdentityAgent editor maps none / SSH_AUTH_SOCK / path / $ENV, not free text."""

from sshpilot.connection_dialog import (
    ConnectionDialog,
    identity_agent_mode_from_value,
    identity_agent_value_from_mode,
)


def test_identity_agent_mode_from_value():
    assert identity_agent_mode_from_value("") == ("default", "")
    assert identity_agent_mode_from_value("none") == ("none", "")
    assert identity_agent_mode_from_value("NONE") == ("none", "")
    assert identity_agent_mode_from_value("SSH_AUTH_SOCK") == ("ssh_auth_sock", "")
    assert identity_agent_mode_from_value("ssh_auth_sock") == ("ssh_auth_sock", "")
    assert identity_agent_mode_from_value("$SSH_AUTH_SOCK") == ("env", "$SSH_AUTH_SOCK")
    assert identity_agent_mode_from_value("~/.ssh/agent.sock") == (
        "path",
        "~/.ssh/agent.sock",
    )


def test_identity_agent_value_from_mode():
    assert identity_agent_value_from_mode("default") == ""
    assert identity_agent_value_from_mode("none") == "none"
    assert identity_agent_value_from_mode("ssh_auth_sock") == "SSH_AUTH_SOCK"
    assert identity_agent_value_from_mode("path", "~/.ssh/agent.sock") == (
        "~/.ssh/agent.sock"
    )
    assert identity_agent_value_from_mode("path", "") == ""
    assert identity_agent_value_from_mode("env", "$SSH_AUTH_SOCK") == "$SSH_AUTH_SOCK"
    assert identity_agent_value_from_mode("env", "SSH_AUTH_SOCK") == "$SSH_AUTH_SOCK"
    assert identity_agent_value_from_mode("env", "") == ""


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
    dialog.identity_agent_row = _DummyCombo(selected)
    dialog.identity_agent_value_row = _DummyEntry(extra)
    dialog._identity_agent_browse_btn = _DummyButton()
    return dialog


def test_path_and_env_reveal_the_value_row():
    dialog = _combo_dialog(selected=0)
    dialog._on_identity_agent_mode_changed()
    assert dialog.identity_agent_value_row.visible is False
    assert dialog._identity_agent_browse_btn.visible is False

    dialog.identity_agent_row.set_selected(3)  # path
    dialog._on_identity_agent_mode_changed()
    assert dialog.identity_agent_value_row.visible is True
    assert dialog._identity_agent_browse_btn.visible is True

    dialog.identity_agent_row.set_selected(4)  # env
    dialog._on_identity_agent_mode_changed()
    assert dialog.identity_agent_value_row.visible is True
    assert dialog._identity_agent_browse_btn.visible is False


def test_combo_save_emits_documented_identity_agent_tokens():
    assert _combo_dialog(selected=1)._selected_identity_agent() == "none"
    assert _combo_dialog(selected=2)._selected_identity_agent() == "SSH_AUTH_SOCK"
    assert _combo_dialog(
        selected=3, extra="~/.ssh/agent.sock"
    )._selected_identity_agent() == "~/.ssh/agent.sock"
    assert _combo_dialog(
        selected=4, extra="SSH_AUTH_SOCK"
    )._selected_identity_agent() == "$SSH_AUTH_SOCK"
    assert _combo_dialog(selected=0, extra="ignored")._selected_identity_agent() == ""
