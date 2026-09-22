"""Host Info failure presentation is shared by GTK and WebKit."""

from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.host_info import HostInfoFailure, HostInfoFailureCode
from sshpilot.gtk import host_info_failure_messages as messages
from sshpilot.host_info_tab import HostInfoTab
from sshpilot.machine_info_dialog import MachineInfoDialog


def _failure():
    return HostInfoFailure(
        HostInfoFailureCode.REMOTE_COMMAND_FAILED,
        ErrorCode.REMOTE_COMMAND_FAILED,
        {"exit_code": "127"},
        "opaque ssh: command not found",
    )


def test_every_host_info_failure_code_has_a_frontend_template():
    assert set(messages._FAILURE_TEMPLATES) == set(HostInfoFailureCode)


def test_gettext_runs_before_formatting_and_never_sees_diagnostic(monkeypatch):
    inputs = []

    def translate(msgid):
        inputs.append(msgid)
        return "Sortie {exit_code}" if "{exit_code}" in msgid else msgid

    monkeypatch.setattr(messages, "_", translate)
    result = messages.format_host_info_failure(_failure())
    assert result == "Sortie 127\n\nopaque ssh: command not found"
    assert len(inputs) == 1
    assert "{exit_code}" in inputs[0]
    assert "opaque ssh" not in inputs[0]


def test_rpc_error_uses_stable_code_and_unknown_diagnostic_is_separate(monkeypatch):
    inputs = []
    monkeypatch.setattr(messages, "_", lambda value: inputs.append(value) or "FR: " + value)
    known = messages.format_host_info_error(
        SshPilotError(ErrorCode.OPERATION_NOT_FOUND, "daemon English message")
    )
    assert known.startswith("FR: ")
    assert "daemon English message" not in known
    unknown = messages.format_host_info_error(RuntimeError("external diagnostic"))
    assert unknown == "FR: Could not gather host information.\n\nexternal diagnostic"
    assert all("external diagnostic" not in item for item in inputs)


def test_gtk_and_webkit_show_the_same_structured_failure():
    rendered_gtk = []
    refreshed = []
    gtk = SimpleNamespace(
        _closed=False,
        _refresh_button=SimpleNamespace(set_sensitive=refreshed.append),
        _show_status=rendered_gtk.append,
    )
    rendered_web = []
    web = SimpleNamespace(_closed=False, _push_status=rendered_web.append)
    summary = SimpleNamespace(failure=_failure(), snapshot=None)

    assert MachineInfoDialog._on_snapshot(gtk, summary) is False
    assert HostInfoTab._on_snapshot(web, summary) is False
    assert refreshed == [True]
    assert rendered_gtk == rendered_web
    assert "exited with status 127" in rendered_gtk[0]
    assert rendered_gtk[0].endswith("opaque ssh: command not found")


def test_gtk_and_webkit_show_the_same_localized_rpc_error():
    gtk_status = []
    gtk = SimpleNamespace(
        _closed=False,
        _refresh_button=SimpleNamespace(set_sensitive=lambda _value: None),
        _show_status=gtk_status.append,
    )
    web_status = []
    web = SimpleNamespace(_closed=False, _push_status=web_status.append)
    error = SshPilotError(ErrorCode.OPERATION_NOT_FOUND, "daemon English message")

    assert MachineInfoDialog._on_error(gtk, error) is False
    assert HostInfoTab._on_error(web, error) is False
    assert gtk_status == web_status
    assert "daemon English message" not in gtk_status[0]


def test_failure_model_rejects_wrong_parameter_shape():
    with pytest.raises(ValueError):
        HostInfoFailure(
            HostInfoFailureCode.REMOTE_COMMAND_FAILED,
            ErrorCode.REMOTE_COMMAND_FAILED,
        )
