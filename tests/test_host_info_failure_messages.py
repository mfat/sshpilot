"""Frontend localization for Host Info failures."""

from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.host_info import HostInfoFailure, HostInfoFailureCode
from sshpilot.gtk import host_info_failure_messages as messages


def _failure(code=HostInfoFailureCode.PROBE_FAILED, *, diagnostic=""):
    return HostInfoFailure(
        code,
        ErrorCode.REMOTE_COMMAND_FAILED,
        diagnostic=diagnostic,
    )


def test_frontend_mapping_is_exhaustive():
    assert set(messages._HOST_INFO_FAILURE_TEMPLATES) == set(HostInfoFailureCode)


def test_gettext_runs_before_parameter_formatting(monkeypatch):
    calls = []

    class Translation(str):
        def format(self, **parameters):
            calls.append(("format", parameters))
            return str(self)

    def translate(msgid):
        calls.append(("gettext", msgid))
        return Translation("Échec traduit")

    monkeypatch.setattr(messages, "_", translate)

    assert messages.format_host_info_failure(_failure()) == "Échec traduit"
    assert calls == [
        ("gettext", "The host information probe failed"),
        ("format", {}),
    ]


def test_diagnostic_is_appended_without_gettext(monkeypatch):
    calls = []

    def translate(msgid):
        calls.append(msgid)
        return "La collecte des informations de l’hôte a échoué"

    monkeypatch.setattr(messages, "_", translate)
    diagnostic = "ssh: connect to host example.test port 22: Connection refused"

    assert messages.format_host_info_failure(
        _failure(diagnostic=diagnostic)
    ) == f"La collecte des informations de l’hôte a échoué\n\n{diagnostic}"
    assert calls == ["The host information probe failed"]


def test_known_rpc_error_uses_its_stable_code_not_its_message(monkeypatch):
    calls = []
    monkeypatch.setattr(messages, "_", lambda msgid: calls.append(msgid) or "Indisponible")
    error = SshPilotError(
        ErrorCode.OPERATION_NOT_FOUND,
        "backend wording that must not select the presentation",
    )

    assert messages.format_host_info_error(error) == "Indisponible"
    assert calls == ["The host information result is no longer available."]


def test_unstructured_rpc_error_remains_an_opaque_diagnostic(monkeypatch):
    calls = []
    monkeypatch.setattr(messages, "_", lambda msgid: calls.append(msgid) or "Échec")
    diagnostic = "socket implementation detail"

    assert messages.format_host_info_error(RuntimeError(diagnostic)) == (
        f"Échec\n\n{diagnostic}"
    )
    assert calls == ["The host information request failed."]


def test_presenter_rejects_a_non_host_info_failure():
    with pytest.raises(ValueError, match="invalid host info failure"):
        messages.format_host_info_failure(object())


class _RefreshButton:
    def __init__(self):
        self.sensitive = False

    def set_sensitive(self, value):
        self.sensitive = value


def _dialog_shell():
    from sshpilot.machine_info_dialog import MachineInfoDialog

    dialog = object.__new__(MachineInfoDialog)
    dialog._closed = False
    dialog._refresh_button = _RefreshButton()
    dialog.statuses = []
    dialog._show_status = dialog.statuses.append
    return dialog


def test_machine_info_dialog_displays_the_frontend_failure_text(monkeypatch):
    import sshpilot.machine_info_dialog as dialog_module

    dialog = _dialog_shell()
    monkeypatch.setattr(dialog_module, "_", lambda value: f"traduit: {value}")
    monkeypatch.setattr(
        dialog_module,
        "format_host_info_failure",
        lambda _value: "raison localisée",
    )
    summary = SimpleNamespace(failure=_failure(), snapshot=None)

    assert dialog._on_snapshot(summary) is False
    assert dialog._refresh_button.sensitive is True
    assert dialog.statuses == [
        "traduit: Could not gather host information.\n\nraison localisée"
    ]


def test_machine_info_dialog_uses_the_rpc_error_formatter(monkeypatch):
    import sshpilot.machine_info_dialog as dialog_module

    dialog = _dialog_shell()
    monkeypatch.setattr(dialog_module, "_", lambda value: value)
    monkeypatch.setattr(
        dialog_module,
        "format_host_info_error",
        lambda _value: "erreur structurée",
    )

    assert dialog._on_error(RuntimeError("must not be rendered directly")) is False
    assert dialog.statuses == [
        "Could not gather host information.\n\nerreur structurée"
    ]
