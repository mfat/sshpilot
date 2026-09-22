"""GTK translates Ásbrú reasons and plural counts only at display time."""

from sshpilot.api.models.connections import (
    AsbruImportMessage,
    AsbruImportMessageCode as Code,
    AsbruImportResult,
)
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.gtk import asbru_import_messages as messages


def test_every_asbru_reason_has_frontend_presentation():
    assert set(messages._TEMPLATES) == set(Code) - {Code.IMPORTED}


def test_gettext_precedes_formatting_and_never_receives_diagnostic(monkeypatch):
    seen = []

    def translate(msgid):
        seen.append(msgid)
        return "Nom {name!r}" if "{name!r}" in msgid else msgid

    monkeypatch.setattr(messages, "_", translate)
    notice = AsbruImportMessage(
        Code.SKIPPED_MISSING_HOST, {"name": "dynamic-host"}, "raw YAML diagnostic"
    )
    rendered = messages.format_asbru_message(notice)
    assert rendered == "Nom 'dynamic-host'\nraw YAML diagnostic"
    assert len(seen) == 1
    assert "dynamic-host" not in seen[0]
    assert "raw YAML diagnostic" not in seen[0]


def test_independent_plural_counts_and_translated_envelope(monkeypatch):
    counts = []
    envelope_inputs = []

    def plural(singular, plural, count):
        counts.append((singular, plural, count))
        return ("ONE " if count == 1 else "MANY ") + (singular if count == 1 else plural)

    def translate(template):
        envelope_inputs.append(template)
        return "FR: " + template

    monkeypatch.setattr(messages, "ngettext", plural)
    monkeypatch.setattr(messages, "_", translate)
    rendered = messages.format_asbru_preview_counts(1, 2, 0)
    assert "FR: Import ONE 1 connection and MANY 2 groups?" in rendered
    assert "MANY 0 existing nicknames" in rendered
    assert [count for _, _, count in counts] == [0, 1, 2]
    assert all("(s)" not in value for value in envelope_inputs)

    counts.clear()
    result = AsbruImportResult(
        ok=True, source="export.yml", connections_added=("one", "two"),
        groups_added=("group",), message=AsbruImportMessage(Code.IMPORTED),
    )
    assert messages.format_asbru_result_message(result) == (
        "FR: Imported MANY 2 connections and ONE 1 group."
    )
    assert [count for _, _, count in counts] == [2, 1]


def test_no_changes_preview_and_unknown_rpc_diagnostic(monkeypatch):
    seen = []
    monkeypatch.setattr(messages, "_", lambda value: seen.append(value) or value)
    body = messages.format_asbru_preview_counts(0, 0, 1)
    assert "1 existing nickname would be skipped" in body
    error = messages.format_asbru_rpc_error(RuntimeError("opaque transport detail"))
    assert error.endswith("opaque transport detail")
    assert all("opaque transport detail" not in item for item in seen)
    known = messages.format_asbru_rpc_error(SshPilotError(
        ErrorCode.UNSUPPORTED_CAPABILITY, "daemon English sentence"
    ))
    assert known == "Ásbrú import is unavailable on this daemon."
    assert "daemon English sentence" not in known
