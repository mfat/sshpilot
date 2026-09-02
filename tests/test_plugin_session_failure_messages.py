"""Frontend presentation tests for structured built-in plugin failures."""

from __future__ import annotations

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.sessions import (
    PluginSessionFailure,
    PluginSessionFailureCode,
)
from sshpilot.gtk import plugin_session_failure_messages as messages


_CASES = {
    PluginSessionFailureCode.CONTAINER_REQUIRED: ({}, "container"),
    PluginSessionFailureCode.CONTAINER_RUNTIME_UNAVAILABLE: (
        {"runtime": "podman"},
        "podman",
    ),
    PluginSessionFailureCode.POD_REQUIRED: ({}, "pod"),
    PluginSessionFailureCode.KUBECTL_UNAVAILABLE: (
        {"program": "kubectl"},
        "kubectl",
    ),
    PluginSessionFailureCode.MOSH_UNAVAILABLE: (
        {"client_program": "mosh", "server_program": "mosh-server"},
        "mosh-server",
    ),
    PluginSessionFailureCode.HOST_REQUIRED: ({}, "host"),
    PluginSessionFailureCode.MOSH_PREPARATION_FAILED: ({}, "Mosh"),
    PluginSessionFailureCode.ARGUMENTS_INVALID: (
        {"field": "extra_ssh_opts"},
        "fr:Extra SSH options",
    ),
    PluginSessionFailureCode.SERIAL_DEVICE_REQUIRED: ({}, "serial device"),
    PluginSessionFailureCode.SERIAL_SCREEN_HARDWARE_FLOW_UNSUPPORTED: (
        {
            "fallback_program": "screen",
            "preferred_program": "picocom",
            "flow": "RTS/CTS",
        },
        "RTS/CTS",
    ),
    PluginSessionFailureCode.SERIAL_SCREEN_DATABITS_UNSUPPORTED: (
        {
            "fallback_program": "screen",
            "preferred_program": "picocom",
            "databits": "6",
        },
        "6",
    ),
    PluginSessionFailureCode.SERIAL_SCREEN_HARDWARE_FLOW_AND_DATABITS_UNSUPPORTED: (
        {
            "fallback_program": "screen",
            "preferred_program": "picocom",
            "flow": "RTS/CTS",
            "databits": "5",
        },
        "5",
    ),
    PluginSessionFailureCode.SERIAL_PROGRAMS_UNAVAILABLE: (
        {"preferred_program": "picocom", "fallback_program": "screen"},
        "screen",
    ),
}


def test_every_plugin_session_failure_code_has_frontend_presentation():
    assert set(_CASES) == set(PluginSessionFailureCode)
    assert set(messages._PLUGIN_SESSION_FAILURE_TEMPLATES) == set(
        PluginSessionFailureCode
    )


@pytest.mark.parametrize("code", tuple(PluginSessionFailureCode))
def test_plugin_session_failure_is_translated_then_formatted(monkeypatch, code):
    parameters, expected = _CASES[code]
    translated = []

    def translate(msgid):
        translated.append(msgid)
        return f"fr:{msgid}"

    monkeypatch.setattr(messages, "_", translate)
    failure = PluginSessionFailure(
        code=code,
        error_code=ErrorCode.SESSION_STARTUP_FAILED,
        parameters=parameters,
    )

    rendered = messages.format_plugin_session_failure(failure)

    assert rendered.startswith("fr:")
    assert expected in rendered
    assert messages._PLUGIN_SESSION_FAILURE_TEMPLATES[code] in translated


def test_plugin_session_failure_appends_diagnostic_unchanged(monkeypatch):
    diagnostic = "opaque external detail: --bad-value"
    monkeypatch.setattr(messages, "_", lambda msgid: f"translated:{msgid}")
    failure = PluginSessionFailure(
        code=PluginSessionFailureCode.MOSH_PREPARATION_FAILED,
        error_code=ErrorCode.SESSION_STARTUP_FAILED,
        diagnostic=diagnostic,
    )

    rendered = messages.format_plugin_session_failure(failure)

    presentation, separated_diagnostic = rendered.split("\n\n", 1)
    assert presentation.startswith("translated:")
    assert separated_diagnostic == diagnostic


@pytest.mark.parametrize(
    "code,parameters",
    [
        (PluginSessionFailureCode.CONTAINER_REQUIRED, {"field": "container"}),
        (
            PluginSessionFailureCode.CONTAINER_RUNTIME_UNAVAILABLE,
            {"runtime": "nerdctl"},
        ),
        (
            PluginSessionFailureCode.ARGUMENTS_INVALID,
            {"field": "translated English label"},
        ),
        (
            PluginSessionFailureCode.SERIAL_SCREEN_DATABITS_UNSUPPORTED,
            {
                "fallback_program": "screen",
                "preferred_program": "picocom",
                "databits": "8",
            },
        ),
    ],
)
def test_plugin_session_failure_rejects_invalid_parameter_payloads(code, parameters):
    with pytest.raises(ValueError):
        PluginSessionFailure(
            code=code,
            error_code=ErrorCode.SESSION_STARTUP_FAILED,
            parameters=parameters,
        )
