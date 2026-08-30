"""Frontend-owned presentation of structured backup/import messages."""

from __future__ import annotations

from gettext import gettext as _, ngettext
from typing import Any, Mapping

from ..api.models.secrets import (
    SecretTransferMessage,
    SecretTransferMessageCode,
)
from ..i18n import N_


_MESSAGE_TEMPLATES = {
    SecretTransferMessageCode.BACKUP_ITEMS_REQUIRED: N_(
        "Choose at least one item to include in the backup."
    ),
    SecretTransferMessageCode.NOTHING_SELECTED_TO_EXPORT: N_(
        "Nothing selected to export"
    ),
    SecretTransferMessageCode.BITWARDEN_BACKUP_UNAVAILABLE: N_(
        "Bitwarden is unavailable for backup"
    ),
    SecretTransferMessageCode.BITWARDEN_NOTE_TOO_LARGE: N_(
        "This backup is {length} characters — larger than the Bitwarden note limit ({limit})."
    ),
    SecretTransferMessageCode.BITWARDEN_BACKUP_TOO_LARGE: N_(
        "The backup is too large for a Bitwarden note."
    ),
    SecretTransferMessageCode.BITWARDEN_NOTE_REDUCE: N_(
        "Leaving out the largest part, or exporting fewer connections, may bring it under the limit."
    ),
    SecretTransferMessageCode.EXPORT_SPBK_INSTEAD: N_(
        "Export to a .spbk file instead."
    ),
    SecretTransferMessageCode.BITWARDEN_EXPORT_FAILED: N_(
        "Bitwarden export failed."
    ),
    SecretTransferMessageCode.BACKUP_EXPORT_FAILED: N_(
        "Failed to export backup."
    ),
    SecretTransferMessageCode.SSH_BACKUP_EXPORT_FAILED: N_(
        "SSH export failed."
    ),
    SecretTransferMessageCode.BACKUP_FILE_NOT_FOUND: N_(
        "Backup file not found: {source}"
    ),
    SecretTransferMessageCode.CONFIGURATION_IMPORT_FAILED: N_(
        "Failed to import configuration."
    ),
    SecretTransferMessageCode.CONFIGURATION_IMPORT_FAILED_GENERIC: N_(
        "The configuration could not be imported"
    ),
    SecretTransferMessageCode.ARCHIVE_DECRYPT_OR_READ_FAILED: N_(
        "The archive could not be decrypted or read."
    ),
    SecretTransferMessageCode.WRONG_PASSPHRASE_OR_CORRUPT_BACKUP: N_(
        "Wrong passphrase or corrupt backup"
    ),
    SecretTransferMessageCode.BACKUP_IMPORT_FAILED: N_(
        "Failed to import backup."
    ),
    SecretTransferMessageCode.BACKUP_IMPORT_FAILED_GENERIC: N_(
        "The backup could not be imported"
    ),
    SecretTransferMessageCode.SECRETS_NOT_PERSISTED: N_(
        "The selected secret backend does not persist secrets (agent); no credentials were restored."
    ),
    SecretTransferMessageCode.BITWARDEN_BACKUP_LIST_FAILED: N_(
        "Could not list Bitwarden backups"
    ),
    SecretTransferMessageCode.BITWARDEN_BACKUP_NOT_FOUND: N_(
        "The chosen Bitwarden backup was not found"
    ),
    SecretTransferMessageCode.BITWARDEN_BACKUP_READ_FAILED: N_(
        "The chosen Bitwarden backup could not be read"
    ),
    SecretTransferMessageCode.INVALID_SSHPILOT_BACKUP: N_(
        "The chosen backup is not a valid sshPilot backup"
    ),
    SecretTransferMessageCode.SSH_BACKUP_LIST_FAILED: N_(
        "Could not list backups on the SSH server"
    ),
    SecretTransferMessageCode.SSH_BACKUP_NOT_FOUND: N_(
        "The chosen backup was not found on the SSH server"
    ),
    SecretTransferMessageCode.SSH_BACKUP_READ_FAILED: N_(
        "The chosen backup could not be read from the SSH server"
    ),
    SecretTransferMessageCode.ENCRYPTION_REQUEST_TIMED_OUT: N_(
        "Encryption password request timed out"
    ),
    SecretTransferMessageCode.ENCRYPTION_CANCELLED: N_("Encryption cancelled"),
    SecretTransferMessageCode.DECRYPTION_CANCELLED: N_("Decryption cancelled"),
    SecretTransferMessageCode.BITWARDEN_NOTE_SAVE_FAILED: N_(
        "Bitwarden did not save the backup note (is the vault unlocked?)"
    ),
    SecretTransferMessageCode.SSH_SERVER_CONNECTION_FAILED: N_(
        "Could not connect to the server."
    ),
    SecretTransferMessageCode.SSH_SERVER_DIRECTORY_UNAVAILABLE: N_(
        "Cannot create or write to {directory} on the server."
    ),
    SecretTransferMessageCode.SSH_SERVER_FREE_SPACE_INSUFFICIENT: N_(
        "Not enough free space on the server: need ~{required}, only {available} available in {directory}."
    ),
    SecretTransferMessageCode.SSH_SERVER_WRITE_FAILED: N_(
        "Failed to write the backup to the server."
    ),
    SecretTransferMessageCode.INVALID_JSON_FILE: N_("Invalid JSON file."),
    SecretTransferMessageCode.IMPORT_DATA_NOT_OBJECT: N_(
        "Import data must be a JSON object"
    ),
    SecretTransferMessageCode.IMPORT_VERSION_MISSING: N_(
        "Missing 'version' field in import data"
    ),
    SecretTransferMessageCode.BACKUP_VERSION_UNSUPPORTED: N_(
        "Unsupported backup version: {version}"
    ),
    SecretTransferMessageCode.SCHEMA_VERSION_UNSUPPORTED: N_(
        "Unsupported schema version: {version}"
    ),
    SecretTransferMessageCode.APP_CONFIG_MISSING: N_(
        "Missing 'app_config' field in import data"
    ),
    SecretTransferMessageCode.APP_CONFIG_NOT_OBJECT: N_(
        "'app_config' must be a JSON object"
    ),
    SecretTransferMessageCode.CONNECTIONS_NOT_LIST: N_(
        "connections must be a list"
    ),
    SecretTransferMessageCode.CONNECTION_ENTRY_NOT_OBJECT: N_(
        "Connection entry must be a mapping"
    ),
    SecretTransferMessageCode.CONNECTION_NICKNAME_REQUIRED: N_(
        "Connection nickname is required"
    ),
    SecretTransferMessageCode.CONNECTION_NICKNAME_WHITESPACE: N_(
        "Nickname cannot contain whitespace"
    ),
    SecretTransferMessageCode.CONFIGURATION_REPLACE_FAILED: N_(
        "Failed to replace configuration."
    ),
    SecretTransferMessageCode.CONFIGURATION_MERGE_FAILED: N_(
        "Failed to merge configuration."
    ),
    SecretTransferMessageCode.CONNECTION_STORE_RESTORE_FAILED: N_(
        "Could not restore non-SSH connections, groups, or metadata."
    ),
    SecretTransferMessageCode.CONNECTION_STORE_VERSION_UNSUPPORTED: N_(
        "The connection data in this backup uses an unsupported version and was skipped."
    ),
    SecretTransferMessageCode.CONNECTION_RESTORE_FAILED: N_(
        "Could not restore connection {connection}."
    ),
    SecretTransferMessageCode.CONNECTION_UPDATE_FAILED: N_(
        "Could not update connection {connection}."
    ),
    SecretTransferMessageCode.GROUP_RESTORE_FAILED: N_(
        "Could not restore group {group}."
    ),
    SecretTransferMessageCode.GROUP_UPDATE_FAILED: N_(
        "Could not update group {group}."
    ),
    SecretTransferMessageCode.GROUP_REMOVE_FAILED: N_(
        "Could not remove group {group}."
    ),
    SecretTransferMessageCode.GROUP_ORDER_FAILED: N_(
        "Could not order group {group}."
    ),
    SecretTransferMessageCode.STALE_MEMBERSHIP_REMOVE_FAILED: N_(
        "Could not remove stale group membership for {connection}."
    ),
    SecretTransferMessageCode.RESTORED_GROUP_CONNECTION_MISSING: N_(
        "Connection {connection} referenced by a restored group was not found."
    ),
    SecretTransferMessageCode.BACKUP_ROOT_CONNECTION_MISSING: N_(
        "Root connection {connection} referenced by the backup was not found."
    ),
    SecretTransferMessageCode.UNKNOWN_CONNECTION_METADATA_SKIPPED: N_(
        "Metadata for unknown connection {connection} was skipped."
    ),
    SecretTransferMessageCode.METADATA_RESTORE_FAILED: N_(
        "Could not restore metadata for {connection}."
    ),
    SecretTransferMessageCode.DISPLAY_NAME_RESTORE_FAILED: N_(
        "Could not restore the display name for {connection}."
    ),
    SecretTransferMessageCode.CONNECTION_REMOVE_FAILED: N_(
        "Could not remove connection {connection}."
    ),
}

_SECTION_LABELS = {
    "app_settings": N_("app settings"),
    "ssh_config": N_("SSH config"),
    "known_hosts": N_("known hosts"),
    "credentials": N_("credentials"),
    "private_keys": N_("private keys"),
}


def _template_for(message: SecretTransferMessage) -> str:
    code = message.code
    params = message.parameters
    if code is SecretTransferMessageCode.BITWARDEN_NOTE_LARGEST_SECTION:
        return ngettext(
            "Most of it is {label} ({cost} character).",
            "Most of it is {label} ({cost} characters).",
            params["cost"],
        )
    if code is SecretTransferMessageCode.SSH_CONFIG_FILES_SKIPPED:
        return ngettext(
            "{count} SSH config file outside ~/.ssh was not included (system or shared file): {paths}",
            "{count} SSH config files outside ~/.ssh were not included (system or shared files): {paths}",
            params["count"],
        )
    if code is SecretTransferMessageCode.REFERENCED_KEY_FILES_MISSING:
        return ngettext(
            "{count} referenced key file was missing and not included: {paths}",
            "{count} referenced key files were missing and not included: {paths}",
            params["count"],
        )
    try:
        return _(_MESSAGE_TEMPLATES[code])
    except KeyError:
        raise ValueError("transfer message code has no frontend presentation") from None


def format_secret_transfer_message(message: Any) -> str:
    """Translate one structured transfer message and append its raw diagnostic."""

    if type(message) is not SecretTransferMessage:
        raise ValueError("invalid secret transfer message")
    parameters: Mapping[str, object] = message.parameters
    display_parameters = dict(parameters)
    if message.code is SecretTransferMessageCode.BITWARDEN_NOTE_LARGEST_SECTION:
        section = parameters["section"]
        try:
            display_parameters["label"] = _(_SECTION_LABELS[section])
        except KeyError:
            raise ValueError("transfer message section has no frontend presentation") from None
    rendered = _template_for(message).format(**display_parameters)
    return f"{rendered}\n\n{message.diagnostic}" if message.diagnostic else rendered


def format_secret_transfer_messages(messages: Any) -> tuple[str, ...]:
    """Render an ordered sequence of structured transfer warnings."""

    return tuple(format_secret_transfer_message(message) for message in messages)
