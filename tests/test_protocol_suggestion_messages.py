"""Known suggestion failures are translated; tool output and plugin APIs stay opaque."""

import gettext
import io
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sshpilot.gtk import protocol_suggestion_messages as messages
from sshpilot.plugins._command_failure import LocalCommandFailure, LocalCommandFailureReason
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.builtin import docker_protocol, kubernetes_protocol


@pytest.mark.parametrize("reason", list(LocalCommandFailureReason))
def test_each_known_reason_translates_only_its_template(monkeypatch, reason):
    calls = []
    diagnostic = "external: {program}\nsecond line\n"

    def translate(msgid):
        calls.append(msgid)
        return "translated:{status}:{program}"

    monkeypatch.setattr(messages, "_", translate)
    result = messages.format_protocol_suggestion_error(LocalCommandFailure(
        reason, program="kubectl", status=7, diagnostic=diagnostic,
    ))
    assert calls == [messages._REASON_TEMPLATES[reason]]
    assert result == "translated:7:kubectl\n\n" + diagnostic


def test_unknown_plugin_exception_is_opaque(monkeypatch):
    calls = []
    monkeypatch.setattr(messages, "_", lambda text: calls.append(text) or "translated:{error}")
    diagnostic = "plugin detail: Command timed out {braces}\nline 2"
    assert messages.format_protocol_suggestion_error(RuntimeError(diagnostic)) == (
        "translated:" + diagnostic
    )
    assert calls == ["Could not list suggestions: {error}"]


def test_empty_unknown_exception_keeps_technical_type_name(monkeypatch):
    monkeypatch.setattr(messages, "_", lambda _text: "translated:{error}")
    assert messages.format_protocol_suggestion_error(RuntimeError()) == "translated:RuntimeError"


def test_real_catalogue_formats_translated_reason_without_touching_diagnostic(monkeypatch):
    msgid = messages._REASON_TEMPLATES[LocalCommandFailureReason.EXITED]
    header = "Content-Type: text/plain; charset=UTF-8\nLanguage: zz\n"
    po = '\n'.join([
        'msgid ""', 'msgstr ' + json.dumps(header), '', '#, python-brace-format',
        'msgid ' + json.dumps(msgid), 'msgstr "status={status}; program={program}"', '',
    ])
    compiled = subprocess.run(["msgfmt", "--check-format", "-o", "-", "-"],
                              input=po.encode(), capture_output=True, check=True)
    catalogue = gettext.GNUTranslations(io.BytesIO(compiled.stdout))
    monkeypatch.setattr(messages, "_", catalogue.gettext)
    assert messages.format_protocol_suggestion_error(LocalCommandFailure(
        LocalCommandFailureReason.EXITED, program="docker", status=127,
        diagnostic="sh: docker: command not found\n",
    )) == "status=127; program=docker\n\nsh: docker: command not found\n"


@pytest.mark.parametrize("provider,values", [
    (docker_protocol._contexts, {"runtime": "docker"}),
    (kubernetes_protocol._contexts, {}),
])
@pytest.mark.parametrize("failure,reason", [
    ("timeout", LocalCommandFailureReason.TIMED_OUT),
    ("host_executor", LocalCommandFailureReason.HOST_EXECUTOR_UNAVAILABLE),
    ("start", LocalCommandFailureReason.START_FAILED),
])
def test_execution_failures_reach_suggestion_presenter(monkeypatch, provider, values, failure, reason):
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak",
                        lambda: failure == "host_executor")
    monkeypatch.setattr("shutil.which", lambda _name: None)

    def fail(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("tool", 8)
        raise OSError("OS diagnostic")

    monkeypatch.setattr(subprocess, "run", fail)
    ctx = PluginContext.for_editor(plugin_id="test", protocol_registry=None)
    with pytest.raises(LocalCommandFailure) as caught:
        provider(values, ctx)
    assert caught.value.reason is reason
    assert caught.value.diagnostic == ("OS diagnostic" if failure == "start" else "")
    assert "Command timed out" not in messages.format_protocol_suggestion_error(caught.value)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_failed_tool_output_is_preserved_verbatim(monkeypatch, stream):
    diagnostic = "  external {data}\nsecond line\n"
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=7, stdout=diagnostic if stream == "stdout" else "",
        stderr=diagnostic if stream == "stderr" else "",
    ))
    ctx = PluginContext.for_editor(plugin_id="docker", protocol_registry=None)
    with pytest.raises(LocalCommandFailure) as caught:
        docker_protocol._contexts({}, ctx)
    calls = []
    monkeypatch.setattr(messages, "_", lambda text: calls.append(text) or "translated:{program}:{status}")
    assert messages.format_protocol_suggestion_error(caught.value) == "translated:docker:7\n\n" + diagnostic
    assert calls == [messages._REASON_TEMPLATES[LocalCommandFailureReason.EXITED]]


@pytest.mark.parametrize("result,expected", [
    (SimpleNamespace(returncode=127, stdout="", stderr="sh: docker: command not found\n"),
     "translated:docker:127\n\nsh: docker: command not found\n"),
    (subprocess.TimeoutExpired("docker", 8), "translated:timeout"),
    (SimpleNamespace(returncode=0, stdout="prod\n", stderr=""), None),
])
def test_connection_dialog_renders_translated_reason_and_success(monkeypatch, result, expected):
    from sshpilot import connection_dialog as dialog

    displayed = []
    gtk = MagicMock()
    gtk.Label.return_value.set_text.side_effect = displayed.append
    adw = MagicMock()
    monkeypatch.setattr(dialog, "Gtk", gtk)
    monkeypatch.setattr(dialog, "Adw", adw)
    monkeypatch.setattr(dialog, "GLib", SimpleNamespace(idle_add=lambda fn, *args: fn(*args)))
    monkeypatch.setattr(dialog, "threading", SimpleNamespace(
        Thread=lambda *, target, **kwargs: SimpleNamespace(start=target)))
    monkeypatch.setattr(dialog, "protocol_registry", lambda: SimpleNamespace(plugin_id_for=lambda pid: pid))
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)

    def run(*args, **kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(messages, "_", lambda msgid:
                        "translated:timeout" if msgid == "The command timed out."
                        else "translated:{program}:{status}")
    backend = docker_protocol.DockerProtocolBackend()
    spec = next(s for s in backend.connection_fields() if s.key == "docker_context")
    owner = SimpleNamespace(_current_protocol_values=lambda _pid: {})
    dialog.ConnectionDialog._show_field_suggestions(owner, MagicMock(), MagicMock(), spec, backend)
    assert displayed == ([expected] if expected is not None else [])
    if expected is None:
        adw.ActionRow.assert_called_once_with(title="prod")
