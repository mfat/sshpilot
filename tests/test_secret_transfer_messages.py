import pytest

from sshpilot.api.models.secrets import (
    SecretTransferMessage,
    SecretTransferMessageCode,
)
from sshpilot.gtk import secret_transfer_messages as messages


def test_every_public_transfer_code_has_a_frontend_presentation():
    plural_codes = {
        SecretTransferMessageCode.BITWARDEN_NOTE_LARGEST_SECTION,
        SecretTransferMessageCode.SSH_CONFIG_FILES_SKIPPED,
        SecretTransferMessageCode.REFERENCED_KEY_FILES_MISSING,
        SecretTransferMessageCode.MERGE_GLOBAL_RULES_SKIPPED,
        SecretTransferMessageCode.MERGE_PARTIAL_NAME_COLLISION,
        SecretTransferMessageCode.MERGE_EXISTING_HOSTS_KEPT,
    }

    assert set(SecretTransferMessageCode) == set(messages._MESSAGE_TEMPLATES) | plural_codes


def test_frontend_translates_before_formatting_and_keeps_source_intact(monkeypatch):
    calls = []

    def translate(msgid):
        calls.append(msgid)
        return "Sauvegarde introuvable : {source}"

    monkeypatch.setattr(messages, "_", translate)
    message = SecretTransferMessage(
        SecretTransferMessageCode.BACKUP_FILE_NOT_FOUND,
        parameters={"source": "/tmp/alice.spbk"},
    )

    assert messages.format_secret_transfer_message(message) == (
        "Sauvegarde introuvable : /tmp/alice.spbk"
    )
    assert calls == ["Backup file not found: {source}"]


def test_frontend_never_translates_external_diagnostic(monkeypatch):
    calls = []

    def translate(msgid):
        calls.append(msgid)
        return "Échec de l'export."

    monkeypatch.setattr(messages, "_", translate)
    message = SecretTransferMessage(
        SecretTransferMessageCode.BACKUP_EXPORT_FAILED,
        diagnostic="OSError: disk quota exceeded",
    )

    assert messages.format_secret_transfer_message(message) == (
        "Échec de l'export.\n\nOSError: disk quota exceeded"
    )
    assert calls == ["Failed to export backup."]


def test_bitwarden_section_code_is_localized_separately(monkeypatch):
    translations = {
        "private keys": "clés privées",
    }
    monkeypatch.setattr(messages, "_", lambda msgid: translations.get(msgid, msgid))
    monkeypatch.setattr(
        messages,
        "ngettext",
        lambda singular, plural, count: plural if count != 1 else singular,
    )
    message = SecretTransferMessage(
        SecretTransferMessageCode.BITWARDEN_NOTE_LARGEST_SECTION,
        parameters={"section": "private_keys", "cost": 42},
    )

    assert messages.format_secret_transfer_message(message) == (
        "Most of it is clés privées (42 characters)."
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    (
        (1, "1 referenced key file was missing and not included: /one"),
        (2, "2 referenced key files were missing and not included: /one, /two"),
    ),
)
def test_counted_warning_uses_gettext_plural(monkeypatch, count, expected):
    seen = []

    def plural(singular, plural, value):
        seen.append((singular, plural, value))
        return singular if value == 1 else plural

    monkeypatch.setattr(messages, "ngettext", plural)
    paths = "/one" if count == 1 else "/one, /two"
    message = SecretTransferMessage(
        SecretTransferMessageCode.REFERENCED_KEY_FILES_MISSING,
        parameters={"count": count, "paths": paths},
    )

    assert messages.format_secret_transfer_message(message) == expected
    assert seen[0][2] == count


@pytest.mark.parametrize(
    ("code", "parameters", "expected"),
    (
        (SecretTransferMessageCode.MERGE_GLOBAL_RULES_SKIPPED, {"count": 1},
         "1 rule for all hosts (Host * or Match) in the backup was not merged, because it "
         "would change every connection. Imported hosts follow this computer's rules "
         "instead."),
        (SecretTransferMessageCode.MERGE_PARTIAL_NAME_COLLISION,
         {"count": 2, "hosts": "a b, c d"},
         "2 host entries in the backup also name hosts you already have; only their new "
         "names were added: a b, c d"),
        (SecretTransferMessageCode.MERGE_EXISTING_HOSTS_KEPT, {"count": 1, "hosts": "web"},
         "1 host in the backup has the same name as one of yours but different settings. "
         "Yours was kept and the backup's was not imported: web"),
        (SecretTransferMessageCode.MERGE_HOST_ROUTED_THROUGH_EXISTING,
         {"host": "db", "via": "web"},
         "The imported host db connects through web, so it now goes through your own web, "
         "not the one in the backup."),
    ),
)
def test_merge_restore_warnings_render(code, parameters, expected):
    message = SecretTransferMessage(code, parameters=parameters)

    assert messages.format_secret_transfer_message(message) == expected


def test_multiple_warnings_keep_wire_order():
    warnings = (
        SecretTransferMessage(SecretTransferMessageCode.EXPORT_SPBK_INSTEAD),
        SecretTransferMessage(SecretTransferMessageCode.BITWARDEN_NOTE_REDUCE),
    )

    assert messages.format_secret_transfer_messages(warnings) == (
        "Export to a .spbk file instead.",
        "Leaving out the largest part, or exporting fewer connections, may bring it under the limit.",
    )


def test_invalid_message_object_is_rejected_strictly():
    with pytest.raises(ValueError, match="invalid secret transfer message"):
        messages.format_secret_transfer_message(object())
